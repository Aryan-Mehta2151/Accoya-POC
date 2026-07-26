"""Opt-in PostgreSQL integration tests for the agent-centric database.

Set ``ACCOYA_TEST_DATABASE_URL`` to an isolated database whose name ends in
``_test``. The suite provisions/migrates that database, never invokes an
external provider, and cleans only agent-subsystem rows.
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Text, create_engine, event, func, inspect, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from agent.models import (
    GenerationResult,
    GenerationStatus,
    GenerationTelemetry,
    TokenUsage,
)
from app.api.routes import agent_runs, emails
from app.db import bootstrap, database
from app.db.database import get_db
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    EarlyBidSyncRun,
    EarlyBidSyncRunStatus,
    Email,
    EmailGenerationJob,
    EmailGenerationJobStatus,
    EmailGenerationTrigger,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    User,
)
from app.schemas.email import EmailStatusUpdate
from app.services import (
    agent_run_service,
    earlybid_sync_service,
    email_generation_service,
    email_generator,
    lead_feed_service,
)


TEST_DATABASE_URL = os.getenv("ACCOYA_TEST_DATABASE_URL")


class FakeAgent:
    """Provider-free synchronous agent implementing the production protocol."""

    def __init__(self) -> None:
        self.status = GenerationStatus.GENERATED
        self.subject = "PostgreSQL integration draft"
        self.body = "A provider-free email body."
        self.error: Exception | None = None
        self.observer: Callable[[dict[str, Any]], None] | None = None
        self.calls: list[dict[str, Any]] = []

    def generate(self, complete_lead_record: Mapping[str, Any]) -> GenerationResult:
        payload = deepcopy(dict(complete_lead_record))
        self.calls.append(payload)
        if self.observer is not None:
            self.observer(payload)
        if self.error is not None:
            raise self.error
        generated = self.status is GenerationStatus.GENERATED
        return GenerationResult(
            status=self.status,
            lead_id=str(payload["lead_id"]),
            original_lead=payload,
            subject=self.subject if generated else None,
            body=self.body if generated else None,
            selected_product_family="accoya_solid_wood",
            selected_application="decking",
            nurturing_email_number=3,
            nurturing_email_theme="Technical confidence",
            warnings=["safe integration warning"],
            prompt_version="postgres-test-prompt-v1",
            telemetry=GenerationTelemetry(
                model_name="fake-gemini",
                prompt_version="postgres-test-prompt-v1",
                latency_ms=31,
                token_usage=TokenUsage(
                    input_tokens=12,
                    output_tokens=8,
                    total_tokens=20,
                ),
                model_calls=3,
                retrieval_count=2,
            ),
        )


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "Set ACCOYA_TEST_DATABASE_URL to run isolated PostgreSQL integration tests",
)
class PostgresAgentDatabaseTests(unittest.TestCase):
    """Exercise migrations, ingestion, orchestration, and compatibility APIs."""

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
        cls.second_bootstrap_created = bootstrap.bootstrap_database(TEST_DATABASE_URL)
        cls.engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        cls.session_factory = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._clean_agent_rows()
        cls.engine.dispose()

    def setUp(self) -> None:
        self._clean_agent_rows()
        self.agent = FakeAgent()
        self.current_user = User(
            id=str(uuid.uuid4()),
            email="postgres-reviewer@example.com",
            is_active=True,
        )
        app = FastAPI()
        app.include_router(agent_runs.router, prefix="/api")
        app.include_router(emails.router, prefix="/api")
        app.dependency_overrides[get_db] = self._get_db
        app.dependency_overrides[emails.get_current_user] = (
            lambda: self.current_user
        )
        app.dependency_overrides[
            email_generator.get_accoya_email_agent
        ] = lambda: self.agent
        self.app = app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()
        self._clean_agent_rows()

    @classmethod
    def _clean_agent_rows(cls) -> None:
        """Delete only agent-subsystem tables in dependency order."""
        with cls.engine.begin() as connection:
            connection.execute(EarlyBidSyncRun.__table__.delete())
            connection.execute(EmailStatusEvent.__table__.delete())
            connection.execute(Email.__table__.delete())
            connection.execute(AgentRun.__table__.delete())
            connection.execute(EmailGenerationJob.__table__.delete())
            connection.execute(Lead.__table__.delete())

    def _get_db(self):
        with self.session_factory() as db:
            yield db

    def _seed_normalized_lead(self, external_id: str = "postgres-lead") -> str:
        row = {
            "id": external_id,
            "Project": " Waterfront Boardwalk ",
            "Location": "Sacramento, California",
            "Signal": "Decking specification",
            "Score": "92.5",
            "Timing": "Design development",
            "Next Step": "Offer a technical review",
            "Summary": "An exterior public-realm project.",
            "Contacts": "Alex Rivera, Architect, ALEX@EXAMPLE.TEST",
            "Tags": "#Decking, Public Realm; decking",
            "Private Feed Field": "retained only in JSONB",
        }
        with self.session_factory() as db:
            touched, created, updated = lead_feed_service.upsert_feed_rows(
                db,
                [row],
                source_feed="postgres/test-feed",
            )
            self.assertEqual((created, updated), (1, 0))
            db.commit()
            return str(touched[0].id)

    def test_bootstrap_is_idempotent_and_schema_uses_postgres_types(self):
        self.assertFalse(self.second_bootstrap_created)
        with self.engine.connect() as connection:
            self.assertEqual(
                set(database.get_current_schema_heads(connection)),
                set(database.get_expected_schema_heads()),
            )

        inspector = inspect(self.engine)
        lead_columns = {
            column["name"]: column for column in inspector.get_columns("leads")
        }
        email_columns = {
            column["name"]: column for column in inspector.get_columns("emails")
        }
        self.assertIsInstance(lead_columns["id"]["type"], UUID)
        self.assertIsInstance(lead_columns["tags"]["type"], JSONB)
        self.assertIsInstance(lead_columns["raw_data"]["type"], JSONB)
        self.assertIsInstance(email_columns["subject"]["type"], Text)
        self.assertIsNone(getattr(email_columns["subject"]["type"], "length", None))

        agent_run_columns = {
            column["name"]: column
            for column in inspector.get_columns("agent_runs")
        }
        agent_run_indexes = {
            index["name"]: index for index in inspector.get_indexes("agent_runs")
        }
        job_columns = {
            column["name"]: column
            for column in inspector.get_columns("email_generation_jobs")
        }
        job_indexes = {
            index["name"]: index
            for index in inspector.get_indexes("email_generation_jobs")
        }
        sync_columns = {
            column["name"]: column
            for column in inspector.get_columns("earlybid_sync_runs")
        }
        sync_indexes = {
            index["name"]: index
            for index in inspector.get_indexes("earlybid_sync_runs")
        }
        sync_checks = {
            constraint["name"]: constraint
            for constraint in inspector.get_check_constraints(
                "earlybid_sync_runs"
            )
        }
        sync_uniques = {
            constraint["name"]: constraint
            for constraint in inspector.get_unique_constraints(
                "earlybid_sync_runs"
            )
        }
        self.assertEqual(agent_run_columns["model_calls"]["default"], "0")
        self.assertEqual(agent_run_columns["retrieval_count"]["default"], "0")
        self.assertEqual(
            agent_run_indexes["ix_agent_runs_started_at_id"]["column_names"],
            ["started_at", "id"],
        )
        self.assertEqual(job_columns["attempt_count"]["default"], "0")
        active_index = job_indexes[
            "ix_email_generation_jobs_one_active_per_lead"
        ]
        self.assertTrue(active_index["unique"])
        self.assertEqual(active_index["column_names"], ["lead_id"])
        self.assertIn(
            "status",
            str(active_index.get("dialect_options", {})).casefold(),
        )
        self.assertIn("email_generation_job_id", agent_run_columns)
        self.assertEqual(sync_columns["attempt_count"]["default"], "0")
        self.assertEqual(
            sync_indexes["ix_earlybid_sync_runs_due"]["column_names"],
            ["status", "next_attempt_at", "scheduled_for"],
        )
        self.assertEqual(
            sync_indexes["ix_earlybid_sync_runs_heartbeat"]["column_names"],
            ["status", "heartbeat_at"],
        )
        self.assertEqual(
            sync_uniques["uq_earlybid_sync_runs_feed_schedule_date"][
                "column_names"
            ],
            ["reseller", "client", "schedule_date"],
        )
        result_count_check = sync_checks[
            "ck_earlybid_sync_runs_result_count_bounds"
        ]["sqltext"]
        self.assertIn(
            "created_count + updated_count <= total_count",
            result_count_check.replace("(", "").replace(")", ""),
        )
        self.assertIn(
            "status = 'succeeded'",
            sync_checks["ck_earlybid_sync_runs_terminal_result_shape"][
                "sqltext"
            ],
        )
        sync_lifecycle = sync_checks["ck_earlybid_sync_runs_lifecycle"][
            "sqltext"
        ]
        self.assertIn("status = 'failed'", sync_lifecycle)
        self.assertIn("attempt_count = 0", sync_lifecycle)
        self.assertIn("claimed_by IS NULL", sync_lifecycle)

        command.check(database.make_alembic_config(TEST_DATABASE_URL))

    def test_normalized_ingestion_persists_jsonb_and_composite_identity(self):
        lead_id = self._seed_normalized_lead("shared-external-id")

        with self.session_factory() as db:
            lead = db.get(Lead, lead_id)
            self.assertIsNotNone(lead)
            self.assertEqual(lead.project, "Waterfront Boardwalk")
            self.assertEqual(lead.state, "CA")
            self.assertEqual(lead.score, 92.5)
            self.assertEqual(lead.next_step, "Offer a technical review")
            self.assertEqual(lead.contact_email, "alex@example.test")
            self.assertEqual(lead.tags, ["Decking", "Public Realm"])
            self.assertEqual(
                lead.raw_data["Private Feed Field"],
                "retained only in JSONB",
            )

            _, created, updated = lead_feed_service.upsert_feed_rows(
                db,
                [{"id": "shared-external-id", "Project": "Manual projection"}],
                source_system="manual",
                source_feed="manual/test",
            )
            db.commit()
            self.assertEqual((created, updated), (1, 0))
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Lead)),
                2,
            )

    def test_concurrent_ingestion_queues_one_initial_job(self):
        row = {
            "id": "concurrent-ingestion-lead",
            "Project": "Concurrent Boardwalk",
            "Location": "Sacramento, California",
            "Contacts": "Alex Rivera, alex@example.test",
        }
        barrier = threading.Barrier(2)
        results: list[tuple[int, int, int]] = []
        errors: list[BaseException] = []

        def ingest() -> None:
            try:
                with self.session_factory() as db:
                    created_leads: list[Lead] = []
                    barrier.wait(timeout=5)
                    _, created, updated = lead_feed_service.upsert_feed_rows(
                        db,
                        [row],
                        source_feed="postgres/concurrent-feed",
                        identity_scope="postgres-concurrent-scope",
                        created_leads=created_leads,
                    )
                    db.flush()
                    jobs = email_generation_service.enqueue_initial_generations(
                        db,
                        created_leads,
                        trigger=EmailGenerationTrigger.earlybid_sync,
                    )
                    db.commit()
                    results.append((created, updated, len(jobs)))
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=ingest, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertCountEqual(results, [(1, 0, 1), (0, 1, 0)])
        with self.session_factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 1)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EmailGenerationJob)),
                1,
            )

    def test_concurrent_manual_submissions_return_one_active_job(self):
        lead_id = self._seed_normalized_lead("concurrent-manual-lead")
        key = str(uuid.uuid4())
        barrier = threading.Barrier(2)
        job_ids: list[str] = []
        errors: list[BaseException] = []

        def enqueue() -> None:
            try:
                with self.session_factory() as db:
                    barrier.wait(timeout=5)
                    job = email_generation_service.enqueue_generation(
                        db,
                        lead_id=lead_id,
                        idempotency_key=key,
                    )
                    job_ids.append(job.id)
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=enqueue, daemon=True) for _ in range(2)]
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
                db.scalar(
                    select(func.count())
                    .select_from(EmailGenerationJob)
                    .where(EmailGenerationJob.lead_id == lead_id)
                ),
                1,
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentRun)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Email)), 0)

    def test_run_lifecycle_commits_running_and_persists_all_outcomes(self):
        lead_id = self._seed_normalized_lead()
        observed: dict[str, Any] = {}
        self.agent.subject = "Long PostgreSQL subject " + ("x" * 800)

        with self.session_factory() as db:
            def observe_running(payload: dict[str, Any]) -> None:
                observed["outer_transaction"] = db.in_transaction()
                observed["raw_forwarded"] = "raw_data" in payload
                with self.session_factory() as observer:
                    persisted = observer.scalar(
                        select(AgentRun).where(AgentRun.lead_id == lead_id)
                    )
                    observed["status"] = persisted.status
                    observed["completed_at"] = persisted.completed_at
                    observed["email_count"] = observer.scalar(
                        select(func.count()).select_from(Email)
                    )

            self.agent.observer = observe_running
            generated = agent_run_service.execute_agent_run(
                db,
                lead_id=lead_id,
                agent=self.agent,
            )
            self.agent.observer = None

            for generation_status, persisted_status in (
                (
                    GenerationStatus.INSUFFICIENT_CONTEXT,
                    AgentRunStatus.insufficient_context,
                ),
                (GenerationStatus.PROVIDER_ERROR, AgentRunStatus.provider_error),
            ):
                self.agent.status = generation_status
                outcome = agent_run_service.execute_agent_run(
                    db,
                    lead_id=lead_id,
                    agent=self.agent,
                )
                self.assertEqual(outcome.status, persisted_status)

            self.agent.error = RuntimeError("provider detail must remain private")
            with self.assertRaises(agent_run_service.AgentRunSystemError) as raised:
                agent_run_service.execute_agent_run(
                    db,
                    lead_id=lead_id,
                    agent=self.agent,
                )

        self.assertFalse(observed["outer_transaction"])
        self.assertFalse(observed["raw_forwarded"])
        self.assertEqual(observed["status"], AgentRunStatus.running)
        self.assertIsNone(observed["completed_at"])
        self.assertEqual(observed["email_count"], 0)
        self.assertEqual(generated.status, AgentRunStatus.generated)
        self.assertGreater(len(generated.original_subject), 512)
        self.assertEqual(generated.model_calls, 3)
        self.assertEqual(generated.retrieval_count, 2)

        with self.session_factory() as db:
            system_run = db.get(AgentRun, raised.exception.run_id)
            self.assertEqual(system_run.status, AgentRunStatus.system_error)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Email)),
                1,
            )
            email = db.scalar(
                select(Email).where(Email.agent_run_id == generated.id)
            )
            event = db.scalar(
                select(EmailStatusEvent).where(
                    EmailStatusEvent.email_id == email.id
                )
            )
            self.assertEqual(email.subject, generated.original_subject)
            self.assertEqual(event.new_status, EmailStatus.pending_review)

    def test_queue_retry_hash_audit_and_cursor_pagination(self):
        lead_id = self._seed_normalized_lead()
        self.agent.subject = "Queue subject " + ("z" * 700)
        with self.session_factory() as db:
            first_job = email_generation_service.enqueue_generation(
                db,
                lead_id=lead_id,
                idempotency_key=str(uuid.uuid4()),
            )
            first_job_id = first_job.id
            first_requested_hash = first_job.requested_input_hash
        with self.session_factory() as db:
            first_claim = email_generation_service.claim_next_job(
                db,
                worker_id="postgres-worker-1",
            )
        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=first_claim,
                agent=self.agent,
            )

        with self.session_factory() as db:
            persisted_first_job = db.get(EmailGenerationJob, first_job_id)
            first_run = persisted_first_job.agent_run
            first_run_id = first_run.id
            first_hash = first_run.input_hash
            original_subject = first_run.original_subject
            email_id = first_run.email.id
            lead = db.get(Lead, lead_id)
            lead.summary = "Changed current projection for retry hashing."
            db.commit()

        self.agent.subject = "Retry draft"
        with self.session_factory() as db:
            retry_job = email_generation_service.enqueue_generation(
                db,
                lead_id=lead_id,
                idempotency_key=str(uuid.uuid4()),
            )
            retry_job_id = retry_job.id
            self.assertEqual(retry_job.retry_of_job_id, first_job_id)
            self.assertNotEqual(retry_job.requested_input_hash, first_requested_hash)
        with self.session_factory() as db:
            retry_claim = email_generation_service.claim_next_job(
                db,
                worker_id="postgres-worker-2",
            )
        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=retry_claim,
                agent=self.agent,
            )

        with self.session_factory() as db:
            retry_run = db.get(EmailGenerationJob, retry_job_id).agent_run
            self.assertEqual(retry_run.retry_of_run_id, first_run_id)
            self.assertNotEqual(retry_run.input_hash, first_hash)

            third_job = email_generation_service.enqueue_generation(
                db,
                lead_id=lead_id,
                idempotency_key=str(uuid.uuid4()),
            )
        with self.session_factory() as db:
            third_claim = email_generation_service.claim_next_job(
                db,
                worker_id="postgres-worker-3",
            )
        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=third_claim,
                agent=self.agent,
            )
        with self.session_factory() as db:
            current_email = email_generation_service.current_email_for_lead(
                db,
                lead_id,
            )
            self.assertIsNotNone(current_email)
            current_email_id = current_email.id

        first_page = self.client.get(
            "/api/agent-runs",
            params={"lead_id": lead_id, "status": "generated", "limit": 2},
        )
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(len(first_page.json()["items"]), 2)
        cursor = first_page.json()["next_cursor"]
        self.assertIsNotNone(cursor)
        second_page = self.client.get(
            "/api/agent-runs",
            params={"lead_id": lead_id, "limit": 2, "cursor": cursor},
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(len(second_page.json()["items"]), 1)

        edited = self.client.patch(
            f"/api/emails/{current_email_id}",
            json={
                "recipient_email": "reviewer@example.com",
                "subject": "Reviewed subject",
                "body": "Reviewed body",
            },
        )
        self.assertEqual(edited.status_code, 200)
        approved = self.client.post(
            f"/api/emails/{current_email_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(approved.status_code, 200)

        with self.session_factory() as db:
            original = db.get(AgentRun, first_run_id)
            email = db.get(Email, current_email_id)
            events = list(
                db.scalars(
                    select(EmailStatusEvent).where(
                        EmailStatusEvent.email_id == current_email_id
                    )
                ).all()
            )
            self.assertEqual(original.input_hash, first_hash)
            self.assertEqual(original.original_subject, original_subject)
            self.assertEqual(email.subject, "Reviewed subject")
            self.assertEqual(len(events), 2)
            transition = next(event for event in events if event.previous_status)
            self.assertEqual(transition.new_status, EmailStatus.approved)
            self.assertEqual(transition.actor, str(self.current_user.id))


    def test_claim_skips_a_queue_row_locked_by_another_worker(self):
        first_lead = self._seed_normalized_lead("skip-locked-first")
        second_lead = self._seed_normalized_lead("skip-locked-second")
        for lead_id in (first_lead, second_lead):
            with self.session_factory() as db:
                email_generation_service.enqueue_generation(
                    db,
                    lead_id=lead_id,
                    idempotency_key=str(uuid.uuid4()),
                )

        with self.session_factory() as locking_db:
            first_job_id = locking_db.scalar(
                select(EmailGenerationJob.id)
                .where(
                    EmailGenerationJob.status
                    == EmailGenerationJobStatus.queued
                )
                .order_by(
                    EmailGenerationJob.queued_at,
                    EmailGenerationJob.id,
                )
                .limit(1)
            )
            locked = locking_db.scalar(
                select(EmailGenerationJob)
                .where(EmailGenerationJob.id == first_job_id)
                .with_for_update()
            )
            self.assertIsNotNone(locked)

            with self.session_factory() as competing_db:
                claim = email_generation_service.claim_next_job(
                    competing_db,
                    worker_id="skip-locked-worker",
                )

            self.assertIsNotNone(claim)
            self.assertNotEqual(claim.job_id, first_job_id)
            locking_db.rollback()

        with self.session_factory() as db:
            first_job = db.get(EmailGenerationJob, first_job_id)
            claimed_job = db.get(EmailGenerationJob, claim.job_id)
            self.assertEqual(first_job.status, EmailGenerationJobStatus.queued)
            self.assertEqual(claimed_job.status, EmailGenerationJobStatus.running)

    def test_earlybid_slot_is_unique_and_claim_skips_locked_run(self):
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            first = EarlyBidSyncRun(
                reseller="postgres-reseller",
                client="first-client",
                schedule_date=now.date(),
                scheduled_for=now,
            )
            second = EarlyBidSyncRun(
                reseller="postgres-reseller",
                client="second-client",
                schedule_date=now.date(),
                scheduled_for=now,
            )
            db.add_all([first, second])
            db.commit()
            first_id = first.id
            second_id = second.id

        with self.session_factory() as db:
            db.add(
                EarlyBidSyncRun(
                    reseller="postgres-reseller",
                    client="first-client",
                    schedule_date=now.date(),
                    scheduled_for=now,
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

        with self.session_factory() as locking_db:
            locked = locking_db.scalar(
                select(EarlyBidSyncRun)
                .where(EarlyBidSyncRun.id == first_id)
                .with_for_update()
            )
            self.assertIsNotNone(locked)

            with self.session_factory() as competing_db:
                claim = earlybid_sync_service.claim_next_run(
                    competing_db,
                    worker_id="postgres-sync-worker",
                    now=now,
                )

            self.assertIsNotNone(claim)
            self.assertEqual(claim.run_id, str(second_id))
            locking_db.rollback()

        with self.session_factory() as db:
            first_run = db.get(EarlyBidSyncRun, first_id)
            second_run = db.get(EarlyBidSyncRun, second_id)
            self.assertEqual(first_run.status, EarlyBidSyncRunStatus.queued)
            self.assertEqual(second_run.status, EarlyBidSyncRunStatus.running)
            self.assertEqual(second_run.attempt_count, 1)

    def test_concurrent_status_transitions_keep_a_contiguous_event_chain(self):
        lead_id = self._seed_normalized_lead("concurrent-status-lead")
        with self.session_factory() as db:
            generated = agent_run_service.execute_agent_run(
                db,
                lead_id=lead_id,
                agent=self.agent,
            )
        self.assertEqual(generated.status, AgentRunStatus.generated)

        with self.session_factory() as lookup:
            email_id = lookup.scalar(select(Email.id))
        self.assertIsNotNone(email_id)

        competing_lock_started = threading.Event()
        competing_request_finished = threading.Event()
        competing_result: dict[str, Any] = {}

        def observe_for_update(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if "FOR UPDATE" in statement.upper():
                competing_lock_started.set()

        def reject_in_competing_session() -> None:
            try:
                with self.session_factory() as competing_db:
                    updated = emails.update_status(
                        email_id,
                        EmailStatusUpdate(
                            status=EmailStatus.rejected,
                        ),
                        self.current_user,
                        competing_db,
                    )
                    competing_result["status"] = updated.status
            except BaseException as exc:  # surface worker failures in this test
                competing_result["error"] = exc
            finally:
                competing_request_finished.set()

        worker = threading.Thread(
            target=reject_in_competing_session,
            name="postgres-status-transition",
            daemon=True,
        )

        try:
            with self.session_factory() as locking_db:
                locked_email = locking_db.scalar(
                    select(Email)
                    .where(Email.id == email_id)
                    .with_for_update(of=Email)
                )
                self.assertIsNotNone(locked_email)
                locked_email.status = EmailStatus.approved
                locking_db.add(
                    EmailStatusEvent(
                        email_id=email_id,
                        previous_status=EmailStatus.pending_review,
                        new_status=EmailStatus.approved,
                        actor="first-reviewer",
                    )
                )
                locking_db.flush()

                event.listen(
                    self.engine,
                    "before_cursor_execute",
                    observe_for_update,
                )
                worker.start()
                self.assertTrue(
                    competing_lock_started.wait(timeout=5),
                    "The competing status update did not issue SELECT FOR UPDATE",
                )
                self.assertFalse(
                    competing_request_finished.wait(timeout=0.2),
                    "The competing status update bypassed the held row lock",
                )
                locking_db.commit()
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                observe_for_update,
            )
            worker.join(timeout=5)

        self.assertFalse(
            worker.is_alive(),
            "The competing status update deadlocked",
        )
        if "error" in competing_result:
            raise competing_result["error"]
        self.assertEqual(competing_result.get("status"), EmailStatus.rejected)

        with self.session_factory() as db:
            persisted_email = db.get(Email, email_id)
            transitions = list(
                db.scalars(
                    select(EmailStatusEvent)
                    .where(EmailStatusEvent.email_id == email_id)
                ).all()
            )

        self.assertEqual(persisted_email.status, EmailStatus.rejected)
        self.assertEqual(len(transitions), 3)
        self.assertEqual(
            {
                transition.actor: (
                    transition.previous_status,
                    transition.new_status,
                )
                for transition in transitions
            },
            {
                None: (None, EmailStatus.pending_review),
                "first-reviewer": (
                    EmailStatus.pending_review,
                    EmailStatus.approved,
                ),
                str(self.current_user.id): (
                    EmailStatus.approved,
                    EmailStatus.rejected,
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
