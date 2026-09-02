"""Provider-free tests for durable SMTP outreach delivery."""

from __future__ import annotations

import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.database import Base
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailGenerationJob,
    EmailGenerationJobStatus,
    EmailGenerationTrigger,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    LeadReviewStatus,
)
from app.services import email_delivery_service, email_service
from app.workers import email_delivery as email_delivery_worker


class EmailDeliveryQueueTests(unittest.TestCase):
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
        self.settings = Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_password="offline-secret",
            smtp_timeout_seconds=10,
        )
        self.requester_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _seed_email(
        self,
        *,
        external_id: str = "delivery-lead",
        state: str | None = None,
        status: EmailStatus = EmailStatus.approved,
        recipient_email: str | None = "architect@example.com",
        subject: str = "Accoya technical review",
        body: str = "Would a short technical review be useful?",
        signature: str | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="Harbor boardwalk",
                location="Portland, OR",
                state=state,
                contact_email=recipient_email,
                raw_data={},
                source_feed="test/feed",
            )
            db.add(lead)
            db.flush()
            run = AgentRun(
                lead_id=lead.id,
                status=AgentRunStatus.generated,
                input_hash="0" * 64,
                warnings=[],
                original_subject=subject,
                original_body=body,
                prompt_version="test",
                catalog_version="test",
                model_name="offline",
                model_calls=0,
                retrieval_count=0,
                started_at=now,
                completed_at=now,
            )
            db.add(run)
            db.flush()
            email = Email(
                agent_run_id=run.id,
                recipient_email=recipient_email,
                subject=subject,
                body=body,
                signature=signature,
                status=status,
            )
            db.add(email)
            db.flush()
            db.add(
                EmailStatusEvent(
                    email_id=email.id,
                    previous_status=EmailStatus.pending_review,
                    new_status=status,
                    actor="offline-reviewer",
                )
            )
            db.commit()
            return str(email.id)

    def _enqueue(
        self,
        email_id: str,
        *,
        key: str | None = None,
        expected_hash: str | None = None,
        acknowledge_duplicate_risk: bool = False,
    ) -> str:
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            job = email_delivery_service.enqueue_delivery(
                db,
                email_id=email_id,
                idempotency_key=key or str(uuid.uuid4()),
                expected_content_hash=expected_hash
                or email.delivery_content_hash,
                acknowledge_duplicate_risk=acknowledge_duplicate_risk,
                requested_by=self.requester_id,
                sender_email=self.settings.smtp_email,
            )
            return str(job.id)

    def _claim(
        self,
        worker_id: str = "offline-delivery-worker",
    ) -> email_delivery_service.ClaimedEmailDelivery:
        with self.session_factory() as db:
            claim = email_delivery_service.claim_next_job(
                db,
                worker_id=worker_id,
            )
        self.assertIsNotNone(claim)
        return claim

    def test_enqueue_is_idempotent_and_snapshots_exact_content(self) -> None:
        email_id = self._seed_email()
        key = str(uuid.uuid4())
        first_id = self._enqueue(email_id, key=key)
        replay_id = self._enqueue(email_id, key=key)
        active_id = self._enqueue(email_id)

        self.assertEqual(replay_id, first_id)
        self.assertEqual(active_id, first_id)
        with self.session_factory() as db:
            job = db.get(EmailDeliveryJob, first_id)
            self.assertEqual(job.status, EmailDeliveryJobStatus.queued)
            self.assertEqual(job.recipient_email, "architect@example.com")
            self.assertEqual(job.subject, "Accoya technical review")
            self.assertEqual(
                job.body_snapshot,
                "Would a short technical review be useful?",
            )
            self.assertEqual(job.requested_by, self.requester_id)
            self.assertEqual(job.attempt_count, 0)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EmailDeliveryJob)),
                1,
            )

    def test_inactive_lead_blocks_enqueue_and_preclaim_delivery(self) -> None:
        blocked_email_id = self._seed_email(external_id="inactive-enqueue")
        with self.session_factory() as db:
            email = db.get(Email, blocked_email_id)
            email.agent_run.lead.review_status = LeadReviewStatus.deleted
            db.commit()
        with self.assertRaises(email_delivery_service.EmailDeliveryConflictError) as raised:
            self._enqueue(blocked_email_id)
        self.assertEqual(raised.exception.code, "lead_inactive")

        queued_email_id = self._seed_email(external_id="inactive-claim")
        job_id = self._enqueue(queued_email_id)
        with self.session_factory() as db:
            email = db.get(Email, queued_email_id)
            email.agent_run.lead.review_status = LeadReviewStatus.deleted
            db.commit()
        with self.session_factory() as db:
            claim = email_delivery_service.claim_next_job(
                db,
                worker_id="inactive-worker",
            )
            self.assertIsNone(claim)
            job = db.get(EmailDeliveryJob, job_id)
            self.assertEqual(job.status, EmailDeliveryJobStatus.failed)
            self.assertEqual(job.error_code, "lead_inactive")
            self.assertEqual(job.attempt_count, 0)

    def test_idempotency_key_cannot_be_reused_for_another_email(self) -> None:
        first_email_id = self._seed_email(external_id="idempotency-first")
        second_email_id = self._seed_email(external_id="idempotency-second")
        key = str(uuid.uuid4())
        self._enqueue(first_email_id, key=key)

        with self.assertRaises(
            email_delivery_service.IdempotencyKeyConflictError
        ):
            self._enqueue(second_email_id, key=key)

    def test_signature_is_hashed_and_snapshotted_into_the_delivery_body(self) -> None:
        email_id = self._seed_email(
            state="OR",
            signature="Doug Gillikin\nAccsys",
        )
        job_id = self._enqueue(email_id)
        with self.session_factory() as db:
            job = db.get(EmailDeliveryJob, job_id)
            self.assertEqual(
                job.body_snapshot,
                "Would a short technical review be useful?\n\n"
                "Doug Gillikin\nAccsys",
            )
            db.get(Email, email_id).signature = "Changed later"
            db.commit()
        with self.session_factory() as db:
            self.assertEqual(
                db.get(EmailDeliveryJob, job_id).body_snapshot,
                "Would a short technical review be useful?\n\n"
                "Doug Gillikin\nAccsys",
            )

    def test_unsigned_delivery_preserves_the_stored_body_exactly(self) -> None:
        body = "  Would a short technical review be useful?\n"
        email_id = self._seed_email(
            external_id="unsigned-body-compatibility",
            body=body,
        )
        job_id = self._enqueue(email_id)

        with self.session_factory() as db:
            job = db.get(EmailDeliveryJob, job_id)
            self.assertEqual(job.body_snapshot, body)

    def test_us_delivery_auto_appends_default_signature_when_missing(self) -> None:
        email_id = self._seed_email(
            external_id="us-auto-signature",
            state="OR",
            signature=None,
        )
        job_id = self._enqueue(email_id)

        with self.session_factory() as db:
            job = db.get(EmailDeliveryJob, job_id)
            self.assertEqual(
                job.body_snapshot,
                "Would a short technical review be useful?\n\n"
                "ARTURO LUGO\n"
                "NORTH AMERICA ARCHITECTURE SEGMENT MANAGER\n"
                "Accsys Technologies, Building 470, 200 S Wilcox Dr, "
                "Kingsport, TN 37660-5147",
            )

    def test_enqueue_requires_current_approved_unchanged_valid_content(self) -> None:
        email_id = self._seed_email(status=EmailStatus.pending_review)
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            with self.assertRaises(
                email_delivery_service.EmailDeliveryConflictError
            ) as raised:
                email_delivery_service.enqueue_delivery(
                    db,
                    email_id=email_id,
                    idempotency_key=str(uuid.uuid4()),
                    expected_content_hash=email.delivery_content_hash,
                    acknowledge_duplicate_risk=False,
                    requested_by=self.requester_id,
                    sender_email=self.settings.smtp_email,
                )
            self.assertEqual(raised.exception.code, "email_not_approved")

        approved_id = self._seed_email(external_id="changed-content")
        with self.session_factory() as db:
            with self.assertRaises(
                email_delivery_service.EmailDeliveryConflictError
            ) as raised:
                email_delivery_service.enqueue_delivery(
                    db,
                    email_id=approved_id,
                    idempotency_key=str(uuid.uuid4()),
                    expected_content_hash="f" * 64,
                    acknowledge_duplicate_risk=False,
                    requested_by=self.requester_id,
                    sender_email=self.settings.smtp_email,
                )
            self.assertEqual(raised.exception.code, "content_changed")

        missing_recipient_id = self._seed_email(
            external_id="missing-recipient",
            recipient_email=None,
        )
        with self.session_factory() as db:
            email = db.get(Email, missing_recipient_id)
            with self.assertRaises(
                email_delivery_service.EmailDeliveryConflictError
            ) as raised:
                email_delivery_service.enqueue_delivery(
                    db,
                    email_id=missing_recipient_id,
                    idempotency_key=str(uuid.uuid4()),
                    expected_content_hash=email.delivery_content_hash,
                    acknowledge_duplicate_risk=False,
                    requested_by=self.requester_id,
                    sender_email=self.settings.smtp_email,
                )
            self.assertEqual(raised.exception.code, "recipient_invalid")

    def test_enqueue_rejects_while_replacement_generation_is_active(self) -> None:
        email_id = self._seed_email(external_id="active-generation")
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            db.add(
                EmailGenerationJob(
                    lead_id=email.lead_id,
                    trigger=EmailGenerationTrigger.manual,
                    status=EmailGenerationJobStatus.queued,
                    requested_input_hash="0" * 64,
                    idempotency_key=str(uuid.uuid4()),
                    attempt_count=0,
                )
            )
            db.commit()

        with self.session_factory() as db:
            email = db.get(Email, email_id)
            with self.assertRaises(
                email_delivery_service.EmailDeliveryConflictError
            ) as raised:
                email_delivery_service.enqueue_delivery(
                    db,
                    email_id=email_id,
                    idempotency_key=str(uuid.uuid4()),
                    expected_content_hash=email.delivery_content_hash,
                    acknowledge_duplicate_risk=False,
                    requested_by=self.requester_id,
                    sender_email=self.settings.smtp_email,
                )
            self.assertEqual(raised.exception.code, "generation_active")

    def test_success_marks_sent_and_appends_one_status_event(self) -> None:
        email_id = self._seed_email()
        job_id = self._enqueue(email_id)
        claim = self._claim()
        observed: dict[str, object] = {}

        def accepted_transport(**kwargs: object) -> None:
            observed.update(kwargs)
            self.assertFalse(db.in_transaction())

        with self.session_factory() as db:
            completed = email_delivery_service.execute_claimed_job(
                db,
                claim=claim,
                settings=self.settings,
                transport=accepted_transport,
            )
            self.assertEqual(completed.id, job_id)
            self.assertEqual(completed.status, EmailDeliveryJobStatus.succeeded)

        self.assertEqual(observed["recipient_email"], "architect@example.com")
        self.assertEqual(observed["message_id"], claim.message_id)
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            job = db.get(EmailDeliveryJob, job_id)
            events = list(
                db.scalars(
                    select(EmailStatusEvent)
                    .where(EmailStatusEvent.email_id == email_id)
                    .order_by(EmailStatusEvent.created_at, EmailStatusEvent.id)
                ).all()
            )
            self.assertEqual(email.status, EmailStatus.sent)
            self.assertIsNotNone(job.accepted_at)
            self.assertIsNotNone(job.completed_at)
            sent_event = next(
                event for event in events
                if event.new_status is EmailStatus.sent
            )
            self.assertEqual(sent_event.previous_status, EmailStatus.approved)
            self.assertEqual(sent_event.actor, self.requester_id)

    def test_acceptance_with_initial_persistence_failure_becomes_unknown(self) -> None:
        email_id = self._seed_email(external_id="accepted-persistence-failure")
        job_id = self._enqueue(email_id)
        claim = self._claim()

        with self.session_factory() as db:
            real_commit = db.commit
            commit_calls = 0

            def fail_first_commit() -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 1:
                    raise RuntimeError("offline persistence failure")
                real_commit()

            with patch.object(db, "commit", side_effect=fail_first_commit):
                completed = email_delivery_service.execute_claimed_job(
                    db,
                    claim=claim,
                    settings=self.settings,
                    transport=lambda **_: None,
                )

        self.assertEqual(
            completed.status,
            EmailDeliveryJobStatus.delivery_unknown,
        )
        self.assertEqual(
            completed.error_code,
            email_delivery_service.DELIVERY_FINALIZATION_UNKNOWN,
        )
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            job = db.get(EmailDeliveryJob, job_id)
            sent_events = db.scalar(
                select(func.count())
                .select_from(EmailStatusEvent)
                .where(
                    EmailStatusEvent.email_id == email_id,
                    EmailStatusEvent.new_status == EmailStatus.sent,
                )
            )
            self.assertEqual(email.status, EmailStatus.approved)
            self.assertIsNone(job.accepted_at)
            self.assertEqual(sent_events, 0)

    def test_definite_failure_stays_approved_and_can_be_retried(self) -> None:
        email_id = self._seed_email()
        first_id = self._enqueue(email_id)
        claim = self._claim()

        def rejected_transport(**_: object) -> None:
            raise email_service.EmailDeliveryFailure("smtp_recipient_refused")

        with self.session_factory() as db:
            failed = email_delivery_service.execute_claimed_job(
                db,
                claim=claim,
                settings=self.settings,
                transport=rejected_transport,
            )
            self.assertEqual(failed.status, EmailDeliveryJobStatus.failed)
            self.assertEqual(failed.error_code, "smtp_recipient_refused")
        with self.session_factory() as db:
            self.assertEqual(db.get(Email, email_id).status, EmailStatus.approved)

        retry_id = self._enqueue(email_id)
        self.assertNotEqual(retry_id, first_id)
        with self.session_factory() as db:
            retry = db.get(EmailDeliveryJob, retry_id)
            self.assertEqual(retry.retry_of_job_id, first_id)

    def test_unknown_requires_acknowledgement_on_every_later_retry(self) -> None:
        email_id = self._seed_email()
        first_id = self._enqueue(email_id)
        claim = self._claim()

        def uncertain_transport(**_: object) -> None:
            raise email_service.EmailDeliveryUnknown(
                "smtp_submission_interrupted"
            )

        with self.session_factory() as db:
            unknown = email_delivery_service.execute_claimed_job(
                db,
                claim=claim,
                settings=self.settings,
                transport=uncertain_transport,
            )
            self.assertEqual(
                unknown.status,
                EmailDeliveryJobStatus.delivery_unknown,
            )

        with self.session_factory() as db:
            email = db.get(Email, email_id)
            self.assertTrue(email.has_unknown_delivery)
            self.assertEqual(
                email.latest_delivery.status,
                EmailDeliveryJobStatus.delivery_unknown,
            )
            with self.assertRaises(
                email_delivery_service.EmailDeliveryConflictError
            ) as raised:
                email_delivery_service.enqueue_delivery(
                    db,
                    email_id=email_id,
                    idempotency_key=str(uuid.uuid4()),
                    expected_content_hash=email.delivery_content_hash,
                    acknowledge_duplicate_risk=False,
                    requested_by=self.requester_id,
                    sender_email=self.settings.smtp_email,
                )
            self.assertEqual(
                raised.exception.code,
                "duplicate_risk_acknowledgement_required",
            )

        retry_id = self._enqueue(
            email_id,
            acknowledge_duplicate_risk=True,
        )
        self.assertNotEqual(retry_id, first_id)
        retry_claim = self._claim()
        with self.session_factory() as db:
            email_delivery_service.execute_claimed_job(
                db,
                claim=retry_claim,
                settings=self.settings,
                transport=lambda **_: (_ for _ in ()).throw(
                    email_service.EmailDeliveryFailure("smtp_data_rejected")
                ),
            )
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            with self.assertRaises(
                email_delivery_service.EmailDeliveryConflictError
            ) as raised:
                email_delivery_service.enqueue_delivery(
                    db,
                    email_id=email_id,
                    idempotency_key=str(uuid.uuid4()),
                    expected_content_hash=email.delivery_content_hash,
                    acknowledge_duplicate_risk=False,
                    requested_by=self.requester_id,
                    sender_email=self.settings.smtp_email,
                )
            self.assertEqual(
                raised.exception.code,
                "duplicate_risk_acknowledgement_required",
            )

    def test_stale_running_job_becomes_unknown_without_requeue(self) -> None:
        email_id = self._seed_email()
        job_id = self._enqueue(email_id)
        self._claim()
        with self.session_factory() as db:
            job = db.get(EmailDeliveryJob, job_id)
            job.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()
        with self.session_factory() as db:
            recovered = email_delivery_service.recover_stale_jobs(
                db,
                stale_after_seconds=60,
            )
            self.assertEqual(recovered, 1)
        with self.session_factory() as db:
            job = db.get(EmailDeliveryJob, job_id)
            self.assertEqual(
                job.status,
                EmailDeliveryJobStatus.delivery_unknown,
            )
            self.assertEqual(
                job.error_code,
                email_delivery_service.WORKER_LEASE_EXPIRED,
            )
            self.assertIsNone(
                db.scalar(
                    select(EmailDeliveryJob).where(
                        EmailDeliveryJob.status
                        == EmailDeliveryJobStatus.queued
                    )
                )
            )

    def test_heartbeat_requires_the_claiming_worker(self) -> None:
        email_id = self._seed_email(external_id="heartbeat")
        job_id = self._enqueue(email_id)
        self._claim(worker_id="worker-one")

        with self.session_factory() as db:
            self.assertFalse(
                email_delivery_service.heartbeat_job(
                    db,
                    job_id=job_id,
                    worker_id="worker-two",
                )
            )
        with self.session_factory() as db:
            before = db.get(EmailDeliveryJob, job_id).heartbeat_at
        with self.session_factory() as db:
            self.assertTrue(
                email_delivery_service.heartbeat_job(
                    db,
                    job_id=job_id,
                    worker_id="worker-one",
                )
            )
        with self.session_factory() as db:
            after = db.get(EmailDeliveryJob, job_id).heartbeat_at
            self.assertGreaterEqual(after, before)


class EmailDeliveryWorkerConfigurationTests(unittest.TestCase):
    def test_invalid_smtp_exits_before_claiming(self) -> None:
        settings = Settings(
            smtp_host="",
            smtp_email="",
            smtp_password="",
        )
        with patch.object(email_delivery_worker, "_claim_one") as claim_one:
            exit_code = email_delivery_worker.run_worker(settings=settings)
        self.assertEqual(exit_code, 2)
        claim_one.assert_not_called()

    def test_stale_threshold_must_exceed_heartbeat(self) -> None:
        settings = Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_password="offline-secret",
            smtp_timeout_seconds=10,
            email_delivery_heartbeat_seconds=30,
            email_delivery_stale_seconds=30,
        )
        with patch.object(email_delivery_worker, "_claim_one") as claim_one:
            exit_code = email_delivery_worker.run_worker(settings=settings)
        self.assertEqual(exit_code, 2)
        claim_one.assert_not_called()

    def test_pre_stopped_worker_does_not_touch_queue(self) -> None:
        settings = Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_password="offline-secret",
            smtp_timeout_seconds=10,
        )
        stopped = threading.Event()
        stopped.set()
        with patch.object(email_delivery_worker, "_claim_one") as claim_one:
            exit_code = email_delivery_worker.run_worker(
                settings=settings,
                stop_event=stopped,
            )
        self.assertEqual(exit_code, 0)
        claim_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
