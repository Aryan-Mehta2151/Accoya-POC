"""Offline coverage for regeneration guards tied to delivery state."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailStatus,
    Lead,
)
from app.services import email_generation_service


class EmailDeliveryGenerationBlockTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _seed(self, status: EmailDeliveryJobStatus) -> str:
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=f"delivery-generation-{status.value}",
                project="Boardwalk",
                contact_email="architect@example.com",
                raw_data={},
            )
            db.add(lead)
            db.flush()
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
                started_at=now,
                completed_at=now,
            )
            db.add(run)
            db.flush()
            email = Email(
                agent_run_id=run.id,
                recipient_email="architect@example.com",
                subject="Subject",
                body="Body",
                status=EmailStatus.approved,
            )
            db.add(email)
            db.flush()
            terminal = status in (
                EmailDeliveryJobStatus.failed,
                EmailDeliveryJobStatus.delivery_unknown,
            )
            active = status is EmailDeliveryJobStatus.running
            db.add(
                EmailDeliveryJob(
                    email_id=email.id,
                    status=status,
                    requested_by=str(uuid.uuid4()),
                    idempotency_key=str(uuid.uuid4()),
                    content_hash=email.delivery_content_hash,
                    message_id=f"<{uuid.uuid4()}@accoya-outreach.local>",
                    sender_email="sender@example.com",
                    recipient_email="architect@example.com",
                    subject="Subject",
                    body_snapshot="Body",
                    attempt_count=1 if terminal or active else 0,
                    claimed_by="offline-worker" if terminal or active else None,
                    claimed_at=now if terminal or active else None,
                    heartbeat_at=now if terminal or active else None,
                    send_started_at=now if terminal or active else None,
                    error_code="offline_outcome" if terminal else None,
                    completed_at=now if terminal else None,
                )
            )
            db.commit()
            return str(lead.id)

    def test_active_and_unknown_delivery_block_regeneration(self) -> None:
        for status in (
            EmailDeliveryJobStatus.queued,
            EmailDeliveryJobStatus.running,
            EmailDeliveryJobStatus.delivery_unknown,
        ):
            with self.subTest(status=status):
                lead_id = self._seed(status)
                with self.session_factory() as db:
                    with self.assertRaises(
                        email_generation_service.EmailGenerationConflictError
                    ) as raised:
                        email_generation_service.enqueue_generation(
                            db,
                            lead_id=lead_id,
                            idempotency_key=str(uuid.uuid4()),
                        )
                    self.assertEqual(
                        raised.exception.code,
                        "delivery_blocks_generation",
                    )

    def test_definite_failure_does_not_block_regeneration(self) -> None:
        lead_id = self._seed(EmailDeliveryJobStatus.failed)
        with self.session_factory() as db:
            job = email_generation_service.enqueue_generation(
                db,
                lead_id=lead_id,
                idempotency_key=str(uuid.uuid4()),
            )
            self.assertEqual(job.lead_id, lead_id)


if __name__ == "__main__":
    unittest.main()
