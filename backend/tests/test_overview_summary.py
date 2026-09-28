"""Offline coverage for date-filtered Overview dashboard metrics."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import overview
from app.config import Settings
from app.db.database import Base, get_db
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailReply,
    EmailReplyClassification,
    EmailReplyMatchMethod,
    EmailStatus,
    EmailStatusEvent,
    GraphMailboxSyncState,
    GraphMailboxSyncStatus,
    Lead,
    LeadReviewStatus,
)
from app.services import overview_service


UTC = timezone.utc


class OverviewSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.now = datetime(2026, 7, 20, 12, tzinfo=UTC)
        self.settings = Settings(
            email_reply_tracking_enabled=True,
            microsoft_sender_email="sender@example.com",
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _lead(
        self,
        db: Session,
        name: str,
        created_at: datetime,
        *,
        active: bool = True,
    ) -> Lead:
        lead = Lead(
            source_system="earlybid",
            external_id=name,
            project=name,
            raw_data={},
            review_status=(
                LeadReviewStatus.active if active else LeadReviewStatus.deleted
            ),
            created_at=created_at,
        )
        db.add(lead)
        db.flush()
        return lead

    def _email(
        self,
        db: Session,
        lead: Lead,
        created_at: datetime,
        status: EmailStatus,
    ) -> Email:
        run = AgentRun(
            lead_id=lead.id,
            status=AgentRunStatus.generated,
            input_hash="0" * 64,
            warnings=[],
            original_subject="Subject",
            original_body="Body",
            prompt_version="test",
            catalog_version="test",
            model_name="offline",
            model_calls=0,
            retrieval_count=0,
            started_at=created_at,
            completed_at=created_at,
        )
        db.add(run)
        db.flush()
        email = Email(
            agent_run_id=run.id,
            recipient_email="client@example.com",
            subject="Subject",
            body="Body",
            status=status,
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(email)
        db.flush()
        return email

    def _pending_event(
        self,
        db: Session,
        email: Email,
        created_at: datetime,
        previous_status: EmailStatus | None = None,
    ) -> None:
        db.add(
            EmailStatusEvent(
                email_id=email.id,
                previous_status=previous_status,
                new_status=EmailStatus.pending_review,
                created_at=created_at,
            )
        )

    def _accepted_delivery(
        self,
        db: Session,
        email: Email,
        accepted_at: datetime,
    ) -> None:
        identifier = str(uuid.uuid4())
        db.add(
            EmailDeliveryJob(
                email_id=email.id,
                status=EmailDeliveryJobStatus.succeeded,
                requested_by=str(uuid.uuid4()),
                idempotency_key=identifier,
                content_hash="a" * 64,
                message_id=f"<{identifier}@example.com>",
                sender_email="sender@example.com",
                recipient_email="client@example.com",
                subject="Subject",
                body_snapshot="Body",
                attempt_count=1,
                claimed_by="offline-worker",
                queued_at=accepted_at - timedelta(minutes=2),
                claimed_at=accepted_at - timedelta(minutes=1),
                heartbeat_at=accepted_at - timedelta(seconds=30),
                send_started_at=accepted_at - timedelta(seconds=30),
                accepted_at=accepted_at,
                completed_at=accepted_at,
            )
        )

    def _reply(
        self,
        db: Session,
        lead: Lead,
        received_at: datetime,
        *,
        classification: EmailReplyClassification = EmailReplyClassification.human,
        is_read: bool = False,
        removed: bool = False,
    ) -> None:
        identifier = str(uuid.uuid4())
        db.add(
            EmailReply(
                mailbox_email="sender@example.com",
                graph_message_id=f"graph-{identifier}",
                internet_message_id=f"<{identifier}@example.com>",
                reference_message_ids=[],
                lead_id=lead.id,
                sender_email="client@example.com",
                received_at=received_at,
                is_read=is_read,
                classification=classification,
                match_method=EmailReplyMatchMethod.references,
                removed_at=received_at if removed else None,
            )
        )

    def _seed_metrics(self) -> None:
        with self.sessions() as db:
            pending_lead = self._lead(
                db,
                "pending-current",
                datetime(2026, 7, 1, tzinfo=UTC),
            )
            historical_sent = self._email(
                db,
                pending_lead,
                datetime(2026, 7, 2, tzinfo=UTC),
                EmailStatus.sent,
            )
            self._accepted_delivery(
                db,
                historical_sent,
                datetime(2026, 7, 12, tzinfo=UTC),
            )
            current_pending = self._email(
                db,
                pending_lead,
                datetime(2026, 7, 14, tzinfo=UTC),
                EmailStatus.pending_review,
            )
            self._pending_event(
                db,
                current_pending,
                datetime(2026, 7, 14, tzinfo=UTC),
            )
            self._pending_event(
                db,
                current_pending,
                datetime(2026, 7, 15, tzinfo=UTC),
                previous_status=EmailStatus.approved,
            )

            sent_lead = self._lead(
                db,
                "sent-current",
                datetime(2026, 7, 10, tzinfo=UTC),
            )
            current_sent = self._email(
                db,
                sent_lead,
                datetime(2026, 7, 11, tzinfo=UTC),
                EmailStatus.sent,
            )
            self._accepted_delivery(
                db,
                current_sent,
                datetime(2026, 7, 15, 23, 59, tzinfo=UTC),
            )

            legacy_lead = self._lead(
                db,
                "legacy-pending",
                datetime(2026, 7, 15, tzinfo=UTC),
            )
            self._email(
                db,
                legacy_lead,
                datetime(2026, 7, 15, 8, tzinfo=UTC),
                EmailStatus.pending_review,
            )

            end_boundary_lead = self._lead(
                db,
                "end-boundary",
                datetime(2026, 7, 16, tzinfo=UTC),
            )
            deleted_lead = self._lead(
                db,
                "deleted",
                datetime(2026, 7, 12, tzinfo=UTC),
                active=False,
            )
            deleted_email = self._email(
                db,
                deleted_lead,
                datetime(2026, 7, 12, tzinfo=UTC),
                EmailStatus.pending_review,
            )
            self._pending_event(
                db,
                deleted_email,
                datetime(2026, 7, 12, tzinfo=UTC),
            )

            self._reply(db, sent_lead, datetime(2026, 7, 10, tzinfo=UTC))
            self._reply(
                db,
                sent_lead,
                datetime(2026, 7, 15, tzinfo=UTC),
                is_read=True,
            )
            self._reply(db, end_boundary_lead, datetime(2026, 7, 16, tzinfo=UTC))
            self._reply(
                db,
                sent_lead,
                datetime(2026, 7, 13, tzinfo=UTC),
                classification=EmailReplyClassification.automatic,
            )
            self._reply(
                db,
                sent_lead,
                datetime(2026, 7, 13, tzinfo=UTC),
                removed=True,
            )
            self._reply(db, deleted_lead, datetime(2026, 7, 13, tzinfo=UTC))
            db.add(
                GraphMailboxSyncState(
                    mailbox_email="sender@example.com",
                    status=GraphMailboxSyncStatus.idle,
                    subscription_id="subscription-1",
                    subscription_expires_at=self.now + timedelta(days=5),
                    backfill_cutoff_at=self.now - timedelta(days=90),
                    initial_backfill_completed_at=self.now,
                    force_resync=False,
                    next_sync_at=self.now + timedelta(minutes=1),
                    last_succeeded_at=self.now,
                )
            )
            db.commit()

    def test_all_time_uses_current_email_state_and_counts_all_human_replies(self) -> None:
        self._seed_metrics()
        with self.sessions() as db:
            summary = overview_service.overview_summary(
                db,
                settings=self.settings,
                now=self.now,
            )

        self.assertEqual(summary["opportunities"], 4)
        self.assertEqual(summary["needs_review"], 2)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["replies"], 3)
        self.assertEqual(summary["reply_sync_status"], "healthy")

    def test_range_uses_each_metric_activity_and_excludes_end_boundary(self) -> None:
        self._seed_metrics()
        with self.sessions() as db:
            summary = overview_service.overview_summary(
                db,
                settings=self.settings,
                start_at=datetime(2026, 7, 10, tzinfo=UTC),
                end_before=datetime(2026, 7, 16, tzinfo=UTC),
                now=self.now,
            )

        self.assertEqual(summary["opportunities"], 2)
        self.assertEqual(summary["needs_review"], 2)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["replies"], 2)


class OverviewSummaryApiValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        app = FastAPI()
        app.include_router(overview.router, prefix="/api")

        def override_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_requires_a_complete_ordered_offset_aware_range(self) -> None:
        incomplete = self.client.get(
            "/api/overview/summary",
            params={"start_at": "2026-07-01T00:00:00Z"},
        )
        reversed_range = self.client.get(
            "/api/overview/summary",
            params={
                "start_at": "2026-07-02T00:00:00Z",
                "end_before": "2026-07-01T00:00:00Z",
            },
        )
        naive = self.client.get(
            "/api/overview/summary",
            params={
                "start_at": "2026-07-01T00:00:00",
                "end_before": "2026-07-02T00:00:00",
            },
        )
        malformed = self.client.get(
            "/api/overview/summary",
            params={
                "start_at": "not-a-date",
                "end_before": "2026-07-02T00:00:00Z",
            },
        )

        self.assertEqual(incomplete.status_code, 422)
        self.assertEqual(reversed_range.status_code, 422)
        self.assertEqual(naive.status_code, 422)
        self.assertEqual(malformed.status_code, 422)


if __name__ == "__main__":
    unittest.main()
