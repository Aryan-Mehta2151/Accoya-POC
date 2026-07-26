"""Opt-in PostgreSQL integration coverage for durable SMTP delivery.

Set ACCOYA_TEST_DATABASE_URL to an isolated PostgreSQL database whose name
ends in _test. These tests migrate and clean only test-database application
rows; SMTP is always replaced with an in-process fake.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from alembic import command
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.routes import emails
from app.config import Settings
from app.db import bootstrap, database
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailGenerationJob,
    EmailGenerationJobStatus,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    User,
)
from app.email_content import email_content_hash
from app.schemas.email import EmailStatusUpdate
from app.services import (
    email_delivery_service,
    email_generation_service,
    email_service,
)


TEST_DATABASE_URL = os.getenv("ACCOYA_TEST_DATABASE_URL")


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "Set ACCOYA_TEST_DATABASE_URL to run isolated PostgreSQL integration tests",
)
class PostgresEmailDeliveryTests(unittest.TestCase):
    """Exercise delivery migration invariants and PostgreSQL locking."""

    @classmethod
    def setUpClass(cls) -> None:
        assert TEST_DATABASE_URL is not None
        parsed = make_url(TEST_DATABASE_URL)
        database_name = parsed.database or ""
        if parsed.get_backend_name() != "postgresql":
            raise RuntimeError("ACCOYA_TEST_DATABASE_URL must use PostgreSQL")
        if not database_name.endswith("_test"):
            raise RuntimeError(
                "Refusing integration cleanup: test database name must end in _test"
            )
        bootstrap.bootstrap_database(TEST_DATABASE_URL)
        cls.engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        cls.session_factory = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._clean_rows()
        cls.engine.dispose()

    def setUp(self) -> None:
        self._clean_rows()
        self.settings = Settings(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_email="sender@example.com",
            smtp_password="offline-secret",
            smtp_timeout_seconds=10,
        )

    def tearDown(self) -> None:
        self._clean_rows()

    @classmethod
    def _clean_rows(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(EmailDeliveryJob.__table__.delete())
            connection.execute(EmailStatusEvent.__table__.delete())
            connection.execute(Email.__table__.delete())
            connection.execute(AgentRun.__table__.delete())
            connection.execute(EmailGenerationJob.__table__.delete())
            connection.execute(Lead.__table__.delete())

    def _seed_email(
        self,
        external_id: str,
        *,
        status: EmailStatus = EmailStatus.approved,
    ) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="PostgreSQL delivery test",
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
                original_subject="PostgreSQL delivery subject",
                original_body="Provider-free PostgreSQL body.",
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
                subject="PostgreSQL delivery subject",
                body="Provider-free PostgreSQL body.",
                status=status,
            )
            db.add(email)
            db.flush()
            db.add(
                EmailStatusEvent(
                    email_id=email.id,
                    previous_status=None,
                    new_status=status,
                )
            )
            db.commit()
            return str(lead.id), str(email.id)

    def _enqueue(
        self,
        email_id: str,
        *,
        key: str | None = None,
    ) -> str:
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            job = email_delivery_service.enqueue_delivery(
                db,
                email_id=email_id,
                idempotency_key=key or str(uuid.uuid4()),
                expected_content_hash=email_content_hash(
                    email.recipient_email,
                    email.subject,
                    email.body,
                ),
                acknowledge_duplicate_risk=False,
                requested_by=str(uuid.uuid4()),
                sender_email=self.settings.smtp_email,
            )
            return str(job.id)

    def test_migration_and_metadata_match_delivery_contract(self) -> None:
        with self.engine.connect() as connection:
            self.assertEqual(
                set(database.get_current_schema_heads(connection)),
                {"0007_web_auth_security"},
            )

        inspector = inspect(self.engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("email_delivery_jobs")
        }
        self.assertEqual(
            set(columns),
            {
                "id",
                "email_id",
                "retry_of_job_id",
                "status",
                "requested_by",
                "idempotency_key",
                "content_hash",
                "message_id",
                "sender_email",
                "recipient_email",
                "subject",
                "body_snapshot",
                "error_code",
                "attempt_count",
                "claimed_by",
                "queued_at",
                "claimed_at",
                "heartbeat_at",
                "send_started_at",
                "accepted_at",
                "completed_at",
            },
        )
        self.assertIsInstance(columns["id"]["type"], UUID)
        self.assertEqual(columns["attempt_count"]["default"], "0")

        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("email_delivery_jobs")
        }
        active_index = indexes["ix_email_delivery_jobs_one_active_per_email"]
        self.assertTrue(active_index["unique"])
        self.assertEqual(active_index["column_names"], ["email_id"])
        dialect_options = str(active_index.get("dialect_options", {})).casefold()
        self.assertIn("queued", dialect_options)
        self.assertIn("running", dialect_options)

        unique_constraints = {
            constraint["name"]: constraint
            for constraint in inspector.get_unique_constraints(
                "email_delivery_jobs"
            )
        }
        self.assertEqual(
            unique_constraints["uq_email_delivery_jobs_idempotency_key"][
                "column_names"
            ],
            ["idempotency_key"],
        )
        self.assertEqual(
            unique_constraints["uq_email_delivery_jobs_message_id"][
                "column_names"
            ],
            ["message_id"],
        )

        checks = {
            constraint["name"]: constraint
            for constraint in inspector.get_check_constraints(
                "email_delivery_jobs"
            )
        }
        lifecycle = checks["ck_email_delivery_jobs_lifecycle"]["sqltext"]
        self.assertIn("delivery_unknown", lifecycle)
        self.assertIn("send_started_at IS NOT NULL", lifecycle)

        enum = next(
            enum
            for enum in inspector.get_enums()
            if enum["name"] == "email_delivery_job_status"
        )
        self.assertEqual(
            enum["labels"],
            [
                "queued",
                "running",
                "succeeded",
                "failed",
                "delivery_unknown",
            ],
        )
        command.check(database.make_alembic_config(TEST_DATABASE_URL))

    def test_partial_unique_index_and_concurrent_enqueue_are_safe(self) -> None:
        _, email_id = self._seed_email("postgres-concurrent-delivery")
        barrier = threading.Barrier(2)
        job_ids: list[str] = []
        errors: list[BaseException] = []

        def enqueue() -> None:
            try:
                with self.session_factory() as db:
                    email = db.get(Email, email_id)
                    barrier.wait(timeout=5)
                    job = email_delivery_service.enqueue_delivery(
                        db,
                        email_id=email_id,
                        idempotency_key=str(uuid.uuid4()),
                        expected_content_hash=email.delivery_content_hash,
                        acknowledge_duplicate_risk=False,
                        requested_by=str(uuid.uuid4()),
                        sender_email=self.settings.smtp_email,
                    )
                    job_ids.append(str(job.id))
            except BaseException as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=enqueue, daemon=True)
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(job_ids), 2)
        self.assertEqual(len(set(job_ids)), 1)

        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EmailDeliveryJob)),
                1,
            )
            existing = db.get(EmailDeliveryJob, job_ids[0])
            duplicate = EmailDeliveryJob(
                email_id=email_id,
                status=EmailDeliveryJobStatus.queued,
                requested_by=str(uuid.uuid4()),
                idempotency_key=str(uuid.uuid4()),
                content_hash=existing.content_hash,
                message_id=f"<{uuid.uuid4()}@accoya-outreach.local>",
                sender_email=existing.sender_email,
                recipient_email=existing.recipient_email,
                subject=existing.subject,
                body_snapshot=existing.body_snapshot,
                attempt_count=0,
            )
            db.add(duplicate)
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

        claim = None
        with self.session_factory() as db:
            claim = email_delivery_service.claim_next_job(
                db,
                worker_id="postgres-failure-worker",
            )
        self.assertIsNotNone(claim)

        def reject(**_: object) -> None:
            raise email_service.EmailDeliveryFailure("smtp_recipient_refused")

        with self.session_factory() as db:
            email_delivery_service.execute_claimed_job(
                db,
                claim=claim,
                settings=self.settings,
                transport=reject,
            )
        retry_id = self._enqueue(email_id)
        self.assertNotEqual(retry_id, job_ids[0])

    def test_claim_skips_delivery_row_locked_by_another_worker(self) -> None:
        _, first_email_id = self._seed_email("postgres-skip-first")
        _, second_email_id = self._seed_email("postgres-skip-second")
        first_job_id = self._enqueue(first_email_id)
        second_job_id = self._enqueue(second_email_id)
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            db.get(EmailDeliveryJob, first_job_id).queued_at = now - timedelta(
                minutes=1
            )
            db.get(EmailDeliveryJob, second_job_id).queued_at = now
            db.commit()

        with self.session_factory() as locking_db:
            locked = locking_db.scalar(
                select(EmailDeliveryJob)
                .where(EmailDeliveryJob.id == first_job_id)
                .with_for_update()
            )
            self.assertIsNotNone(locked)
            with self.session_factory() as claiming_db:
                claim = email_delivery_service.claim_next_job(
                    claiming_db,
                    worker_id="postgres-skip-locked-worker",
                )
            self.assertIsNotNone(claim)
            self.assertEqual(claim.job_id, second_job_id)
            locking_db.rollback()

        with self.session_factory() as db:
            self.assertEqual(
                db.get(EmailDeliveryJob, first_job_id).status,
                EmailDeliveryJobStatus.queued,
            )
            self.assertEqual(
                db.get(EmailDeliveryJob, second_job_id).status,
                EmailDeliveryJobStatus.running,
            )

    def test_send_and_regenerate_race_activates_only_one_workflow(self) -> None:
        lead_id, email_id = self._seed_email("postgres-send-regenerate-race")
        with self.session_factory() as db:
            content_hash = db.get(Email, email_id).delivery_content_hash

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def queue_generation() -> None:
            try:
                barrier.wait(timeout=5)
                with self.session_factory() as db:
                    email_generation_service.enqueue_generation(
                        db,
                        lead_id=lead_id,
                        idempotency_key=str(uuid.uuid4()),
                    )
                outcomes.append("generation")
            except email_generation_service.EmailGenerationConflictError:
                outcomes.append("generation_conflict")
            except BaseException as exc:
                errors.append(exc)

        def queue_delivery() -> None:
            try:
                barrier.wait(timeout=5)
                with self.session_factory() as db:
                    email_delivery_service.enqueue_delivery(
                        db,
                        email_id=email_id,
                        idempotency_key=str(uuid.uuid4()),
                        expected_content_hash=content_hash,
                        acknowledge_duplicate_risk=False,
                        requested_by=str(uuid.uuid4()),
                        sender_email=self.settings.smtp_email,
                    )
                outcomes.append("delivery")
            except email_delivery_service.EmailDeliveryConflictError:
                outcomes.append("delivery_conflict")
            except BaseException as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=queue_generation, daemon=True),
            threading.Thread(target=queue_delivery, daemon=True),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(
            sum(outcome in {"generation", "delivery"} for outcome in outcomes),
            1,
        )
        self.assertEqual(
            sum(outcome.endswith("_conflict") for outcome in outcomes),
            1,
        )

        with self.session_factory() as db:
            active_generations = db.scalar(
                select(func.count())
                .select_from(EmailGenerationJob)
                .where(
                    EmailGenerationJob.status.in_(
                        (
                            EmailGenerationJobStatus.queued,
                            EmailGenerationJobStatus.running,
                        )
                    )
                )
            )
            active_deliveries = db.scalar(
                select(func.count())
                .select_from(EmailDeliveryJob)
                .where(
                    EmailDeliveryJob.status.in_(
                        (
                            EmailDeliveryJobStatus.queued,
                            EmailDeliveryJobStatus.running,
                        )
                    )
                )
            )
        self.assertEqual(active_generations + active_deliveries, 1)

    def test_smtp_success_keeps_status_event_chain_contiguous(self) -> None:
        current_user = User(
            id=str(uuid.uuid4()),
            email="postgres-reviewer@example.com",
            is_active=True,
        )
        _, email_id = self._seed_email(
            "postgres-delivery-events",
            status=EmailStatus.pending_review,
        )
        with self.session_factory() as db:
            updated = emails.update_status(
                email_id,
                EmailStatusUpdate(
                    status=EmailStatus.approved,
                ),
                current_user,
                db,
            )
            self.assertEqual(updated.status, EmailStatus.approved)

        job_id = self._enqueue(email_id)
        with self.session_factory() as db:
            claim = email_delivery_service.claim_next_job(
                db,
                worker_id="postgres-success-worker",
            )
        self.assertIsNotNone(claim)
        with self.session_factory() as db:
            completed = email_delivery_service.execute_claimed_job(
                db,
                claim=claim,
                settings=self.settings,
                transport=lambda **_: None,
            )
            self.assertEqual(completed.id, job_id)
            self.assertEqual(
                completed.status,
                EmailDeliveryJobStatus.succeeded,
            )

        with self.session_factory() as db:
            persisted_email = db.get(Email, email_id)
            events = list(
                db.scalars(
                    select(EmailStatusEvent).where(
                        EmailStatusEvent.email_id == email_id
                    )
                ).all()
            )
        transitions = {
            event.new_status: (event.previous_status, event.actor)
            for event in events
        }
        self.assertEqual(persisted_email.status, EmailStatus.sent)
        self.assertEqual(len(events), 3)
        self.assertEqual(
            transitions[EmailStatus.pending_review],
            (None, None),
        )
        self.assertEqual(
            transitions[EmailStatus.approved],
            (EmailStatus.pending_review, str(current_user.id)),
        )
        self.assertEqual(
            transitions[EmailStatus.sent],
            (EmailStatus.approved, claim.requested_by),
        )


if __name__ == "__main__":
    unittest.main()
