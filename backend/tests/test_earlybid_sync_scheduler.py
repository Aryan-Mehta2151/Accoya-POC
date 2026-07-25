"""Offline tests for durable daily EarlyBid synchronization."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import leads
from app.config import Settings
from app.db.database import Base, get_db
from app.db.models import (
    EarlyBidSyncRun,
    EarlyBidSyncRunStatus,
    EmailGenerationJob,
    Lead,
)
from app.services import earlybid_sync_service, lead_feed_service
from app.workers import earlybid_sync as earlybid_sync_worker


PACIFIC = "America/Los_Angeles"


class EarlyBidSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.now = datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _ensure(
        self,
        *,
        now: datetime | None = None,
        reseller: str = "reseller",
        client: str = "client",
    ) -> EarlyBidSyncRun:
        with self.session_factory() as db:
            run = earlybid_sync_service.ensure_current_daily_run(
                db,
                reseller=reseller,
                client=client,
                timezone_name=PACIFIC,
                now=now or self.now,
            )
            db.refresh(run)
            db.expunge(run)
            return run

    def _claim(
        self,
        *,
        now: datetime | None = None,
        worker_id: str = "offline-sync-worker",
    ):
        with self.session_factory() as db:
            return earlybid_sync_service.claim_next_run(
                db,
                worker_id=worker_id,
                now=now or self.now,
            )

    def test_midnight_conversion_preserves_local_time_across_dst(self) -> None:
        spring_before = earlybid_sync_service.scheduled_midnight_utc(
            date(2026, 3, 8),
            PACIFIC,
        )
        spring_after = earlybid_sync_service.scheduled_midnight_utc(
            date(2026, 3, 9),
            PACIFIC,
        )
        fall_before = earlybid_sync_service.scheduled_midnight_utc(
            date(2026, 11, 1),
            PACIFIC,
        )
        fall_after = earlybid_sync_service.scheduled_midnight_utc(
            date(2026, 11, 2),
            PACIFIC,
        )

        self.assertEqual(spring_before.hour, 8)
        self.assertEqual(spring_after.hour, 7)
        self.assertEqual(spring_after - spring_before, timedelta(hours=23))
        self.assertEqual(fall_before.hour, 7)
        self.assertEqual(fall_after.hour, 8)
        self.assertEqual(fall_after - fall_before, timedelta(hours=25))
        for instant in (spring_before, spring_after, fall_before, fall_after):
            self.assertEqual(instant.tzinfo, timezone.utc)

        with self.assertRaises(ZoneInfoNotFoundError):
            earlybid_sync_service.scheduled_midnight_utc(
                date(2026, 1, 1),
                "Not/A_Timezone",
            )

    def test_scheduler_creates_only_current_day_and_replays_same_slot(self) -> None:
        first = self._ensure()
        replay = self._ensure()
        after_multi_day_downtime = self._ensure(
            now=datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        )
        other_feed = self._ensure(client="other-client")

        self.assertEqual(replay.id, first.id)
        self.assertEqual(first.schedule_date, date(2026, 7, 25))
        self.assertEqual(
            first.scheduled_for.replace(tzinfo=timezone.utc),
            datetime(2026, 7, 25, 7, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(after_multi_day_downtime.schedule_date, date(2026, 7, 28))
        self.assertNotEqual(after_multi_day_downtime.id, first.id)
        self.assertNotEqual(other_feed.id, first.id)
        with self.session_factory() as db:
            dates = list(
                db.scalars(
                    select(EarlyBidSyncRun.schedule_date)
                    .where(EarlyBidSyncRun.client == "client")
                    .order_by(EarlyBidSyncRun.schedule_date)
                ).all()
            )
            self.assertEqual(dates, [date(2026, 7, 25), date(2026, 7, 28)])

    def test_rollover_supersedes_historical_active_work_by_feed(self) -> None:
        running_historical = self._ensure(
            now=datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
        )
        queued_historical = self._ensure(
            now=datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)
        )
        retry_historical = self._ensure(
            now=datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc)
        )
        current = self._ensure(
            now=datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        )
        other_feed = self._ensure(
            now=datetime(2026, 7, 25, 20, 0, tzinfo=timezone.utc),
            client="other-client",
        )
        with self.session_factory() as db:
            running_claim = earlybid_sync_service.claim_next_run(
                db,
                worker_id="historical-worker",
                reseller="reseller",
                client="client",
                schedule_date=running_historical.schedule_date,
                now=running_historical.scheduled_for.replace(tzinfo=timezone.utc),
            )
        self.assertEqual(running_claim.run_id, running_historical.id)
        with self.session_factory() as db:
            retry_claim = earlybid_sync_service.claim_next_run(
                db,
                worker_id="retry-worker",
                reseller="reseller",
                client="client",
                schedule_date=retry_historical.schedule_date,
                now=retry_historical.scheduled_for.replace(tzinfo=timezone.utc),
            )
            retry = earlybid_sync_service.record_attempt_failure(
                db,
                claim=retry_claim,
                error_code=earlybid_sync_service.UPSTREAM_UNAVAILABLE,
                retryable=True,
                now=retry_historical.scheduled_for.replace(tzinfo=timezone.utc),
            )
        self.assertEqual(retry.status, EarlyBidSyncRunStatus.retry_wait)

        current_time = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        with self.session_factory() as db:
            superseded = earlybid_sync_service.supersede_historical_active_runs(
                db,
                reseller="reseller",
                client="client",
                current_schedule_date=current.schedule_date,
                now=current_time,
            )
            claim = earlybid_sync_service.claim_next_run(
                db,
                worker_id="current-worker",
                reseller="reseller",
                client="client",
                schedule_date=current.schedule_date,
                now=current_time,
            )

        self.assertEqual(superseded, 3)
        self.assertEqual(claim.run_id, current.id)
        with self.session_factory() as db:
            with (
                patch.object(lead_feed_service, "stage_feed_sync") as stage,
                self.assertRaises(earlybid_sync_service.EarlyBidSyncLeaseLostError),
            ):
                earlybid_sync_service.finalize_success(
                    db,
                    claim=running_claim,
                    rows=[{"id": "must-not-persist"}],
                    now=current_time,
                )
            stage.assert_not_called()

        with self.session_factory() as db:
            for run_id in (
                running_historical.id,
                queued_historical.id,
                retry_historical.id,
            ):
                persisted = db.get(EarlyBidSyncRun, run_id)
                self.assertEqual(persisted.status, EarlyBidSyncRunStatus.failed)
                self.assertEqual(
                    persisted.error_code,
                    earlybid_sync_service.SUPERSEDED_SCHEDULE,
                )
                self.assertIsNotNone(persisted.completed_at)
                self.assertIsNone(persisted.next_attempt_at)
            never_claimed = db.get(EarlyBidSyncRun, queued_historical.id)
            self.assertEqual(never_claimed.attempt_count, 0)
            self.assertIsNone(never_claimed.claimed_by)
            self.assertEqual(
                db.get(EarlyBidSyncRun, other_feed.id).status,
                EarlyBidSyncRunStatus.queued,
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 0)

    def test_late_result_self_supersedes_when_newer_schedule_exists(self) -> None:
        historical = self._ensure(
            now=datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)
        )
        with self.session_factory() as db:
            claim = earlybid_sync_service.claim_next_run(
                db,
                worker_id="late-worker",
                reseller="reseller",
                client="client",
                schedule_date=historical.schedule_date,
                now=self.now,
            )
        self._ensure(now=datetime(2026, 7, 26, 18, 0, tzinfo=timezone.utc))

        with self.session_factory() as db:
            with (
                patch.object(lead_feed_service, "stage_feed_sync") as stage,
                self.assertRaises(earlybid_sync_service.EarlyBidSyncLeaseLostError),
            ):
                earlybid_sync_service.finalize_success(
                    db,
                    claim=claim,
                    rows=[{"id": "must-not-overwrite"}],
                    now=self.now,
                )
            stage.assert_not_called()

        with self.session_factory() as db:
            persisted = db.get(EarlyBidSyncRun, historical.id)
            self.assertEqual(persisted.status, EarlyBidSyncRunStatus.failed)
            self.assertEqual(
                persisted.error_code,
                earlybid_sync_service.SUPERSEDED_SCHEDULE,
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 0)

    def test_claims_only_due_work_and_heartbeat_requires_same_attempt(self) -> None:
        run = self._ensure()
        before_midnight = datetime(2026, 7, 25, 6, 59, tzinfo=timezone.utc)
        self.assertIsNone(self._claim(now=before_midnight))

        claim = self._claim(now=run.scheduled_for)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.run_id, run.id)
        self.assertEqual(claim.attempt_count, 1)
        self.assertEqual(claim.worker_id, "offline-sync-worker")
        self.assertIsNone(self._claim(now=run.scheduled_for))

        heartbeat_at = run.scheduled_for + timedelta(seconds=10)
        with self.session_factory() as db:
            self.assertTrue(
                earlybid_sync_service.heartbeat_run(
                    db,
                    claim=claim,
                    now=heartbeat_at,
                )
            )
        wrong_claim = earlybid_sync_service.ClaimedEarlyBidSync(
            run_id=claim.run_id,
            reseller=claim.reseller,
            client=claim.client,
            worker_id="other-worker",
            attempt_count=claim.attempt_count,
        )
        with self.session_factory() as db:
            self.assertFalse(
                earlybid_sync_service.heartbeat_run(
                    db,
                    claim=wrong_claim,
                    now=heartbeat_at + timedelta(seconds=1),
                )
            )
            persisted = db.get(EarlyBidSyncRun, run.id)
            self.assertEqual(persisted.status, EarlyBidSyncRunStatus.running)
            self.assertEqual(persisted.heartbeat_at, heartbeat_at)

    def test_retry_delays_are_bounded_and_fourth_failure_is_terminal(self) -> None:
        run = self._ensure()
        attempt_time = run.scheduled_for.replace(tzinfo=timezone.utc)
        expected_delays = (5, 15, 30)

        for attempt, delay_minutes in enumerate(expected_delays, start=1):
            claim = self._claim(now=attempt_time)
            self.assertEqual(claim.attempt_count, attempt)
            failure_time = attempt_time + timedelta(seconds=20)
            with self.session_factory() as db:
                outcome = earlybid_sync_service.record_attempt_failure(
                    db,
                    claim=claim,
                    error_code=earlybid_sync_service.UPSTREAM_UNAVAILABLE,
                    retryable=True,
                    now=failure_time,
                )
            expected_retry = failure_time + timedelta(minutes=delay_minutes)
            self.assertEqual(outcome.status, EarlyBidSyncRunStatus.retry_wait)
            self.assertEqual(
                outcome.next_attempt_at.replace(tzinfo=timezone.utc),
                expected_retry,
            )
            self.assertIsNone(self._claim(now=expected_retry - timedelta(seconds=1)))
            attempt_time = expected_retry

        final_claim = self._claim(now=attempt_time)
        self.assertEqual(final_claim.attempt_count, 4)
        completed_at = attempt_time + timedelta(seconds=10)
        with self.session_factory() as db:
            final = earlybid_sync_service.record_attempt_failure(
                db,
                claim=final_claim,
                error_code=earlybid_sync_service.UPSTREAM_UNAVAILABLE,
                retryable=True,
                now=completed_at,
            )

        self.assertEqual(final.status, EarlyBidSyncRunStatus.failed)
        self.assertEqual(final.attempt_count, 4)
        self.assertIsNone(final.next_attempt_at)
        self.assertEqual(
            final.completed_at.replace(tzinfo=timezone.utc),
            completed_at,
        )
        self.assertIsNone(self._claim(now=completed_at + timedelta(days=1)))

    def test_terminal_error_does_not_consume_retry_budget(self) -> None:
        run = self._ensure()
        claim = self._claim(now=run.scheduled_for.replace(tzinfo=timezone.utc))
        with self.session_factory() as db:
            outcome = earlybid_sync_service.record_attempt_failure(
                db,
                claim=claim,
                error_code=earlybid_sync_service.INVALID_FEED,
                retryable=False,
                now=self.now,
            )

        self.assertEqual(outcome.status, EarlyBidSyncRunStatus.failed)
        self.assertEqual(outcome.attempt_count, 1)
        self.assertEqual(outcome.error_code, earlybid_sync_service.INVALID_FEED)
        self.assertIsNone(outcome.next_attempt_at)

    def test_stale_lease_moves_to_retry_wait_without_duplicate_attempt(self) -> None:
        run = self._ensure()
        claimed_at = run.scheduled_for.replace(tzinfo=timezone.utc)
        claim = self._claim(now=claimed_at)
        recovery_time = claimed_at + timedelta(minutes=6)

        with self.session_factory() as db:
            recovered = earlybid_sync_service.recover_stale_runs(
                db,
                stale_after_seconds=300,
                now=recovery_time,
            )

        self.assertEqual(recovered, 1)
        with self.session_factory() as db:
            persisted = db.get(EarlyBidSyncRun, claim.run_id)
            self.assertEqual(persisted.status, EarlyBidSyncRunStatus.retry_wait)
            self.assertEqual(persisted.attempt_count, 1)
            self.assertEqual(
                persisted.error_code,
                earlybid_sync_service.WORKER_LEASE_EXPIRED,
            )
            retry_at = persisted.next_attempt_at.replace(tzinfo=timezone.utc)
        self.assertEqual(retry_at, recovery_time + timedelta(minutes=5))
        self.assertIsNone(self._claim(now=retry_at - timedelta(seconds=1)))
        retry_claim = self._claim(now=retry_at, worker_id="replacement-worker")
        self.assertEqual(retry_claim.attempt_count, 2)
        self.assertEqual(retry_claim.worker_id, "replacement-worker")

    def test_late_result_from_expired_attempt_cannot_mutate_leads(self) -> None:
        run = self._ensure()
        claimed_at = run.scheduled_for.replace(tzinfo=timezone.utc)
        stale_claim = self._claim(now=claimed_at)
        recovery_time = claimed_at + timedelta(minutes=6)
        with self.session_factory() as db:
            earlybid_sync_service.recover_stale_runs(
                db,
                stale_after_seconds=300,
                now=recovery_time,
            )

        with self.session_factory() as db:
            with self.assertRaises(
                earlybid_sync_service.EarlyBidSyncLeaseLostError
            ):
                earlybid_sync_service.finalize_success(
                    db,
                    claim=stale_claim,
                    rows=[
                        {
                            "id": "late-result",
                            "Project": "Must be ignored",
                        }
                    ],
                    now=recovery_time,
                )

        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 0)
            persisted = db.get(EarlyBidSyncRun, run.id)
            self.assertEqual(persisted.status, EarlyBidSyncRunStatus.retry_wait)
            self.assertEqual(persisted.attempt_count, 1)

    def test_feed_error_classification_is_safe_and_explicit(self) -> None:
        cases = (
            (
                lead_feed_service.LeadFeedError("secret", status_code=429),
                earlybid_sync_service.UPSTREAM_RATE_LIMITED,
                True,
            ),
            (
                lead_feed_service.LeadFeedError("secret", status_code=408),
                earlybid_sync_service.UPSTREAM_UNAVAILABLE,
                True,
            ),
            (
                lead_feed_service.LeadFeedError("secret", status_code=503),
                earlybid_sync_service.UPSTREAM_UNAVAILABLE,
                True,
            ),
            (
                lead_feed_service.LeadFeedError("secret", status_code=401),
                earlybid_sync_service.UPSTREAM_AUTH_ERROR,
                False,
            ),
            (
                lead_feed_service.LeadFeedError("secret", status_code=404),
                earlybid_sync_service.UPSTREAM_REQUEST_ERROR,
                False,
            ),
            (
                lead_feed_service.LeadFeedValidationError(
                    [
                        lead_feed_service.LeadFeedValidationIssue(
                            row_number=2,
                            reason_code="invalid_natural_identity",
                        )
                    ]
                ),
                earlybid_sync_service.INVALID_FEED,
                False,
            ),
        )

        for error, code, retryable in cases:
            with self.subTest(code=code):
                disposition = earlybid_sync_service.classify_feed_error(error)
                self.assertEqual(disposition.error_code, code)
                self.assertEqual(disposition.retryable, retryable)
                self.assertNotIn("secret", disposition.error_code)

    def test_success_atomically_persists_leads_initial_jobs_and_counts(self) -> None:
        with self.session_factory() as db:
            db.add(
                Lead(
                    source_system="earlybid",
                    external_id="existing-1",
                    project="Old projection",
                    raw_data={},
                    source_feed="reseller/client",
                )
            )
            db.commit()
        run = self._ensure()
        claim = self._claim(now=run.scheduled_for.replace(tzinfo=timezone.utc))
        rows = [
            {
                "id": "existing-1",
                "Project": "Updated projection",
                "Location": "Portland",
                "State": "OR",
            },
            {
                "id": "new-2",
                "Project": "New boardwalk",
                "Location": "Seattle",
                "State": "WA",
            },
        ]

        with self.session_factory() as db:
            outcome = earlybid_sync_service.finalize_success(
                db,
                claim=claim,
                rows=rows,
                now=self.now,
            )

        self.assertEqual(outcome.status, EarlyBidSyncRunStatus.succeeded)
        self.assertEqual(outcome.created, 1)
        self.assertEqual(outcome.updated, 1)
        self.assertEqual(outcome.total, 2)
        self.assertEqual(outcome.generation_queued, 1)
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 2)
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(EmailGenerationJob)
                ),
                1,
            )
            existing = db.scalar(
                select(Lead).where(Lead.external_id == "existing-1")
            )
            new_lead = db.scalar(select(Lead).where(Lead.external_id == "new-2"))
            job = db.scalar(select(EmailGenerationJob))
            self.assertEqual(existing.project, "Updated projection")
            self.assertEqual(job.lead_id, new_lead.id)
            self.assertEqual(job.idempotency_key, f"initial-v1:{new_lead.id}")

    def test_persistence_failure_rolls_back_feed_changes_before_retry(self) -> None:
        run = self._ensure()
        claim = self._claim(now=run.scheduled_for.replace(tzinfo=timezone.utc))
        rows = [
            {
                "id": "rolled-back-lead",
                "Project": "Must not persist",
                "Location": "Portland",
                "State": "OR",
            }
        ]
        with patch.object(
            lead_feed_service,
            "enqueue_initial_generations",
            side_effect=RuntimeError("database detail must stay private"),
        ):
            with self.session_factory() as db:
                with self.assertRaisesRegex(RuntimeError, "database detail"):
                    earlybid_sync_service.finalize_success(
                        db,
                        claim=claim,
                        rows=rows,
                        now=self.now,
                    )

        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 0)
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(EmailGenerationJob)
                ),
                0,
            )
            persisted = db.get(EarlyBidSyncRun, run.id)
            self.assertEqual(persisted.status, EarlyBidSyncRunStatus.running)
            retry = earlybid_sync_service.record_attempt_failure(
                db,
                claim=claim,
                error_code=earlybid_sync_service.PERSISTENCE_ERROR,
                retryable=True,
                now=self.now,
            )
            self.assertEqual(retry.status, EarlyBidSyncRunStatus.retry_wait)
            self.assertEqual(retry.error_code, "persistence_error")

    def test_status_is_read_only_and_reports_overdue_then_next_midnight(self) -> None:
        with self.session_factory() as db:
            empty = earlybid_sync_service.get_sync_status(
                db,
                reseller="reseller",
                client="client",
                timezone_name=PACIFIC,
                now=self.now,
            )
        self.assertTrue(empty.overdue)
        self.assertIsNone(empty.latest_run)
        self.assertEqual(
            empty.next_scheduled_at,
            datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc),
        )
        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EarlyBidSyncRun)),
                0,
            )

        run = self._ensure()
        fresh_now = self.now + timedelta(seconds=60)
        with self.session_factory() as db:
            scheduled = earlybid_sync_service.get_sync_status(
                db,
                reseller="reseller",
                client="client",
                timezone_name=PACIFIC,
                now=fresh_now,
            )
        self.assertFalse(scheduled.overdue)
        self.assertEqual(scheduled.timezone, PACIFIC)
        self.assertEqual(scheduled.latest_run.id, run.id)
        self.assertEqual(scheduled.latest_run.status, EarlyBidSyncRunStatus.queued)
        self.assertEqual(
            scheduled.next_scheduled_at,
            datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc),
        )
        self.assertNotIn(
            "claimed_by",
            scheduled.latest_run.model_dump(mode="json"),
        )

        stale_now = fresh_now + timedelta(seconds=300)
        with self.session_factory() as db:
            queued_stale = earlybid_sync_service.get_sync_status(
                db,
                reseller="reseller",
                client="client",
                timezone_name=PACIFIC,
                stale_after_seconds=300,
                now=stale_now,
            )
        self.assertTrue(queued_stale.overdue)

        claim = self._claim(now=self.now)
        with self.session_factory() as db:
            running_stale = earlybid_sync_service.get_sync_status(
                db,
                reseller="reseller",
                client="client",
                timezone_name=PACIFIC,
                stale_after_seconds=300,
                now=stale_now,
            )
        self.assertTrue(running_stale.overdue)

        with self.session_factory() as db:
            retry = earlybid_sync_service.record_attempt_failure(
                db,
                claim=claim,
                error_code=earlybid_sync_service.UPSTREAM_UNAVAILABLE,
                retryable=True,
                now=stale_now,
            )
        retry_overdue_at = retry.next_attempt_at.replace(
            tzinfo=timezone.utc
        ) + timedelta(seconds=301)
        with self.session_factory() as db:
            retry_stale = earlybid_sync_service.get_sync_status(
                db,
                reseller="reseller",
                client="client",
                timezone_name=PACIFIC,
                stale_after_seconds=300,
                now=retry_overdue_at,
            )
        self.assertTrue(retry_stale.overdue)


class EarlyBidSyncWorkerTests(unittest.TestCase):
    def _settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "lead_api_base_url": "https://earlybid.example.test",
            "lead_api_key": "test-key",
            "lead_feed_reseller": "reseller",
            "lead_feed_client": "client",
            "lead_auto_sync_timezone": PACIFIC,
            "lead_auto_sync_poll_seconds": 30,
            "lead_auto_sync_heartbeat_seconds": 15,
            "lead_auto_sync_stale_seconds": 300,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    def test_invalid_configuration_exits_before_schema_database_or_feed(self) -> None:
        variants = (
            {"lead_api_key": ""},
            {"lead_auto_sync_timezone": "Not/A_Timezone"},
            {
                "lead_auto_sync_heartbeat_seconds": 15,
                "lead_auto_sync_stale_seconds": 15,
            },
        )
        for overrides in variants:
            with self.subTest(overrides=overrides):
                with (
                    patch.object(earlybid_sync_worker, "check_database_schema")
                    as schema_check,
                    patch.object(earlybid_sync_worker, "SessionLocal")
                    as session_factory,
                    patch.object(
                        earlybid_sync_worker.lead_feed_service,
                        "fetch_feed_rows",
                    ) as fetch,
                ):
                    exit_code = earlybid_sync_worker.run_worker(
                        settings=self._settings(**overrides)
                    )

                self.assertEqual(exit_code, 2)
                schema_check.assert_not_called()
                session_factory.assert_not_called()
                fetch.assert_not_called()

    def test_worker_fetches_before_opening_finalization_transaction(self) -> None:
        claim = earlybid_sync_service.ClaimedEarlyBidSync(
            run_id=str(uuid4()),
            reseller="reseller",
            client="client",
            worker_id="worker",
            attempt_count=1,
        )
        rows = [{"id": "fetched", "Project": "Fetched outside transaction"}]
        events: list[str] = []
        heartbeat = MagicMock()
        heartbeat.start.side_effect = lambda: events.append("heartbeat-start")
        heartbeat.stop.side_effect = lambda: events.append("heartbeat-stop")
        context = MagicMock()
        db = object()
        context.__enter__.side_effect = lambda: (events.append("session-open"), db)[1]
        context.__exit__.side_effect = (
            lambda *_args: (events.append("session-close"), False)[1]
        )
        session_factory = MagicMock(return_value=context)

        def fetch_rows(reseller: str, client: str):
            self.assertEqual((reseller, client), ("reseller", "client"))
            session_factory.assert_not_called()
            events.append("fetch")
            return rows

        def finalize(finalize_db, *, claim, rows, timezone_name):
            self.assertIs(finalize_db, db)
            self.assertEqual(claim.run_id, claim_id)
            self.assertEqual(rows, [{"id": "fetched", "Project": "Fetched outside transaction"}])
            self.assertEqual(timezone_name, PACIFIC)
            events.append("finalize")

        claim_id = claim.run_id
        with (
            patch.object(earlybid_sync_worker, "_Heartbeat", return_value=heartbeat),
            patch.object(earlybid_sync_worker, "SessionLocal", session_factory),
            patch.object(
                earlybid_sync_worker.lead_feed_service,
                "fetch_feed_rows",
                side_effect=fetch_rows,
            ),
            patch.object(
                earlybid_sync_worker.earlybid_sync_service,
                "finalize_success",
                side_effect=finalize,
            ),
        ):
            earlybid_sync_worker._execute_claim(self._settings(), claim)

        self.assertEqual(
            events,
            [
                "heartbeat-start",
                "fetch",
                "session-open",
                "finalize",
                "session-close",
                "heartbeat-stop",
            ],
        )

    def test_worker_ignores_late_success_without_recording_another_failure(self) -> None:
        claim = earlybid_sync_service.ClaimedEarlyBidSync(
            run_id=str(uuid4()),
            reseller="reseller",
            client="client",
            worker_id="expired-worker",
            attempt_count=1,
        )
        heartbeat = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = object()
        with (
            patch.object(earlybid_sync_worker, "_Heartbeat", return_value=heartbeat),
            patch.object(earlybid_sync_worker, "SessionLocal", return_value=context),
            patch.object(
                earlybid_sync_worker.lead_feed_service,
                "fetch_feed_rows",
                return_value=[],
            ),
            patch.object(
                earlybid_sync_worker.earlybid_sync_service,
                "finalize_success",
                side_effect=earlybid_sync_service.EarlyBidSyncLeaseLostError(
                    claim.run_id
                ),
            ),
            patch.object(earlybid_sync_worker, "_record_failure") as record_failure,
        ):
            earlybid_sync_worker._execute_claim(self._settings(), claim)

        record_failure.assert_not_called()
        heartbeat.stop.assert_called_once_with()


class EarlyBidSyncStatusApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        app = FastAPI()
        app.include_router(leads.router, prefix="/api")
        app.dependency_overrides[get_db] = self._get_db
        self.app = app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _get_db(self):
        with self.session_factory() as db:
            yield db

    def test_status_endpoint_is_read_only_and_never_fetches_feed(self) -> None:
        now = datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)
        with (
            patch.object(leads.settings, "lead_feed_reseller", "status-reseller"),
            patch.object(leads.settings, "lead_feed_client", "status-client"),
            patch.object(leads.settings, "lead_auto_sync_timezone", PACIFIC),
            patch.object(earlybid_sync_service, "_utc_now", return_value=now),
            patch.object(lead_feed_service, "fetch_feed_rows") as fetch,
        ):
            response = self.client.get("/api/leads/sync-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "timezone": PACIFIC,
                "next_scheduled_at": "2026-07-26T07:00:00Z",
                "overdue": True,
                "latest_run": None,
            },
        )
        fetch.assert_not_called()
        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EarlyBidSyncRun)),
                0,
            )


if __name__ == "__main__":
    unittest.main()
