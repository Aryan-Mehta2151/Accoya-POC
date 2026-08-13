"""Provider-free tests for the durable outreach-generation queue."""

from __future__ import annotations

import unittest
import uuid
import threading
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.models import (
    GenerationResult,
    GenerationStatus,
    GenerationTelemetry,
    TokenUsage,
)
from app.api.routes import emails, leads
from app.config import Settings
from app.db.database import Base, get_db
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailGenerationJob,
    EmailGenerationJobStatus,
    EmailGenerationTrigger,
    EmailStatus,
    EmailStatusEvent,
    Lead,
)
from app.email_signature import DEFAULT_US_EMAIL_SIGNATURE
from app.services import email_generation_service
from app.workers import email_generation as email_generation_worker


class FakeAgent:
    """Deterministic agent that never reaches a live provider."""

    def __init__(self) -> None:
        self.status = GenerationStatus.GENERATED
        self.subject = "Queued Accoya follow-up"
        self.body = "A provider-free queued draft."
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    def generate(self, complete_lead_record: Mapping[str, Any]) -> GenerationResult:
        payload = deepcopy(dict(complete_lead_record))
        self.calls.append(payload)
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
            nurturing_email_number=2,
            nurturing_email_theme="Design confidence",
            warnings=["safe offline warning"],
            prompt_version="queue-test-v1",
            telemetry=GenerationTelemetry(
                model_name="fake-gemini",
                prompt_version="queue-test-v1",
                latency_ms=23,
                token_usage=TokenUsage(
                    input_tokens=9,
                    output_tokens=6,
                    total_tokens=15,
                ),
                model_calls=3,
                retrieval_count=2,
            ),
        )


class EmailGenerationQueueTests(unittest.TestCase):
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

    def _seed_lead(
        self,
        external_id: str = "queue-lead",
        *,
        state: str | None = "OR",
    ) -> str:
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="Harbor boardwalk",
                location="Portland, OR",
                state=state,
                signal="Decking specification",
                summary="Exterior public-realm opportunity.",
                next_step="Offer a technical review",
                contact_email="architect@example.test",
                tags=["decking"],
                raw_data={"private": "not forwarded"},
                source_feed="test/feed",
            )
            db.add(lead)
            db.commit()
            return str(lead.id)

    def _enqueue(self, lead_id: str, key: str | None = None) -> str:
        with self.session_factory() as db:
            job = email_generation_service.enqueue_generation(
                db,
                lead_id=lead_id,
                idempotency_key=key or str(uuid.uuid4()),
            )
            return str(job.id)

    def _claim(self, worker_id: str = "offline-worker"):
        with self.session_factory() as db:
            return email_generation_service.claim_next_job(
                db,
                worker_id=worker_id,
            )

    def test_initial_job_is_staged_with_a_deterministic_key(self) -> None:
        lead_id = self._seed_lead()
        with self.session_factory() as db:
            lead = db.get(Lead, lead_id)
            jobs = email_generation_service.enqueue_initial_generations(
                db,
                [lead],
                trigger=EmailGenerationTrigger.csv_upload,
            )
            db.commit()

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].idempotency_key, f"initial-v1:{lead_id}")
            self.assertEqual(jobs[0].trigger, EmailGenerationTrigger.csv_upload)
            self.assertEqual(jobs[0].status, EmailGenerationJobStatus.queued)
            self.assertEqual(jobs[0].attempt_count, 0)
            self.assertEqual(len(jobs[0].requested_input_hash), 64)

    def test_manual_enqueue_is_replay_safe_and_returns_active_work(self) -> None:
        lead_id = self._seed_lead()
        key = str(uuid.uuid4())
        first_id = self._enqueue(lead_id, key)
        replay_id = self._enqueue(lead_id, key)
        active_id = self._enqueue(lead_id, str(uuid.uuid4()))

        self.assertEqual(replay_id, first_id)
        self.assertEqual(active_id, first_id)
        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EmailGenerationJob)),
                1,
            )

        other_lead_id = self._seed_lead("other-queue-lead")
        with self.session_factory() as db:
            with self.assertRaises(
                email_generation_service.IdempotencyKeyConflictError
            ):
                email_generation_service.enqueue_generation(
                    db,
                    lead_id=other_lead_id,
                    idempotency_key=key,
                )

    def test_claim_commits_run_and_captures_current_input_without_provider(self) -> None:
        lead_id = self._seed_lead()
        job_id = self._enqueue(lead_id)
        with self.session_factory() as db:
            requested_hash = db.get(EmailGenerationJob, job_id).requested_input_hash
            lead = db.get(Lead, lead_id)
            lead.summary = "Changed after queueing and before claim."
            db.commit()

        agent = FakeAgent()
        claim = self._claim()

        self.assertIsNotNone(claim)
        self.assertEqual(agent.calls, [])
        self.assertEqual(claim.lead_id, lead_id)
        self.assertEqual(claim.curated_input["summary"], lead.summary)
        self.assertNotIn("raw_data", claim.curated_input)
        with self.session_factory() as db:
            job = db.get(EmailGenerationJob, job_id)
            run = db.get(AgentRun, claim.run_id)
            self.assertEqual(job.status, EmailGenerationJobStatus.running)
            self.assertEqual(job.attempt_count, 1)
            self.assertEqual(job.claimed_by, "offline-worker")
            self.assertIsNotNone(job.heartbeat_at)
            self.assertEqual(run.status, AgentRunStatus.running)
            self.assertEqual(run.email_generation_job_id, job_id)
            self.assertNotEqual(run.input_hash, requested_hash)

    def test_generated_outcome_is_finalized_atomically_after_provider_call(self) -> None:
        lead_id = self._seed_lead()
        job_id = self._enqueue(lead_id)
        claim = self._claim()
        self.assertIsNotNone(claim)
        observed_transactions: list[bool] = []

        with self.session_factory() as db:
            class ObservingAgent(FakeAgent):
                def generate(self, complete_lead_record):
                    observed_transactions.append(db.in_transaction())
                    return super().generate(complete_lead_record)

            agent = ObservingAgent()
            outcome = email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=agent,
            )

        self.assertEqual(observed_transactions, [False])
        self.assertEqual(outcome.status, EmailGenerationJobStatus.generated)
        self.assertIsNotNone(outcome.completed_at)
        with self.session_factory() as db:
            job = db.get(EmailGenerationJob, job_id)
            run = db.get(AgentRun, claim.run_id)
            email = db.scalar(
                select(Email).where(Email.agent_run_id == claim.run_id)
            )
            event = db.scalar(
                select(EmailStatusEvent).where(
                    EmailStatusEvent.email_id == email.id
                )
            )
            self.assertEqual(job.status, EmailGenerationJobStatus.generated)
            self.assertEqual(run.status, AgentRunStatus.generated)
            self.assertEqual(run.original_subject, agent.subject)
            self.assertEqual(run.total_tokens, 15)
            self.assertEqual(email.subject, agent.subject)
            self.assertEqual(email.recipient_email, "architect@example.test")
            self.assertEqual(email.signature, DEFAULT_US_EMAIL_SIGNATURE)
            self.assertEqual(email.status, EmailStatus.pending_review)
            self.assertIsNone(event.previous_status)
            self.assertEqual(event.new_status, EmailStatus.pending_review)

    def test_non_us_generated_draft_starts_without_a_signature(self) -> None:
        lead_id = self._seed_lead("netherlands-signature", state="NL")
        self._enqueue(lead_id)
        claim = self._claim("netherlands-worker")
        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=FakeAgent(),
            )
        with self.session_factory() as db:
            email = db.scalar(
                select(Email).where(Email.agent_run_id == claim.run_id)
            )
            self.assertIsNone(email.signature)

    def test_signature_policy_is_snapshotted_when_generation_is_claimed(self) -> None:
        lead_id = self._seed_lead("signature-state-race", state="OR")
        self._enqueue(lead_id)
        claim = self._claim("signature-state-worker")
        self.assertEqual(claim.signature, DEFAULT_US_EMAIL_SIGNATURE)

        with self.session_factory() as db:
            lead = db.get(Lead, lead_id)
            lead.state = "NL"
            db.commit()

        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=FakeAgent(),
            )

        with self.session_factory() as db:
            email = db.scalar(
                select(Email).where(Email.agent_run_id == claim.run_id)
            )
            self.assertEqual(email.signature, DEFAULT_US_EMAIL_SIGNATURE)

    def test_expected_and_unexpected_worker_failures_are_terminal(self) -> None:
        for index, (agent_status, expected_status) in enumerate(
            (
                (
                    GenerationStatus.INSUFFICIENT_CONTEXT,
                    EmailGenerationJobStatus.insufficient_context,
                ),
                (
                    GenerationStatus.PROVIDER_ERROR,
                    EmailGenerationJobStatus.provider_error,
                ),
            )
        ):
            with self.subTest(status=agent_status):
                lead_id = self._seed_lead(f"terminal-{index}")
                self._enqueue(lead_id)
                claim = self._claim(f"worker-{index}")
                agent = FakeAgent()
                agent.status = agent_status
                with self.session_factory() as db:
                    outcome = email_generation_service.execute_claimed_job(
                        db,
                        claim=claim,
                        agent=agent,
                    )
                self.assertEqual(outcome.status, expected_status)
                self.assertEqual(outcome.error_code, expected_status.value)
                with self.session_factory() as db:
                    email_count = db.scalar(
                        select(func.count())
                        .select_from(Email)
                        .where(Email.agent_run_id == claim.run_id)
                    )
                    if agent_status is GenerationStatus.INSUFFICIENT_CONTEXT:
                        self.assertEqual(email_count, 1)
                        fallback = db.scalar(
                            select(Email).where(Email.agent_run_id == claim.run_id)
                        )
                        self.assertEqual(fallback.status, EmailStatus.pending_review)
                        self.assertIn("Accoya", fallback.subject)
                        self.assertIn("Depending on the application", fallback.body)
                        self.assertNotIn("Best regards", fallback.body)
                        self.assertEqual(
                            fallback.signature,
                            DEFAULT_US_EMAIL_SIGNATURE,
                        )
                    else:
                        self.assertEqual(email_count, 0)

        lead_id = self._seed_lead("unexpected-terminal")
        self._enqueue(lead_id)
        claim = self._claim("unexpected-worker")
        agent = FakeAgent()
        agent.error = RuntimeError("provider secret must not be persisted")
        with self.session_factory() as db:
            outcome = email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=agent,
            )

        self.assertEqual(outcome.status, EmailGenerationJobStatus.system_error)
        self.assertEqual(outcome.error_code, "agent_execution_failed")
        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(Email)
                    .where(Email.agent_run_id == claim.run_id)
                ),
                0,
            )
            run = db.get(AgentRun, claim.run_id)
            self.assertEqual(run.status, AgentRunStatus.system_error)
            self.assertNotIn("provider secret", run.error_code)

    def test_end_to_end_agent_timeout_is_a_terminal_system_error(self) -> None:
        lead_id = self._seed_lead()
        self._enqueue(lead_id)
        claim = self._claim("timeout-worker")
        release = threading.Event()

        class BlockingAgent(FakeAgent):
            def generate(self, complete_lead_record):
                release.wait(timeout=1)
                return super().generate(complete_lead_record)

        try:
            with self.session_factory() as db:
                outcome = email_generation_service.execute_claimed_job(
                    db,
                    claim=claim,
                    agent=BlockingAgent(),
                    timeout_seconds=0.01,
                )
        finally:
            release.set()

        self.assertEqual(outcome.status, EmailGenerationJobStatus.system_error)
        self.assertEqual(outcome.error_code, "provider_timeout")
        with self.session_factory() as db:
            run = db.get(AgentRun, claim.run_id)
            self.assertEqual(run.status, AgentRunStatus.system_error)
            self.assertEqual(run.error_code, "provider_timeout")

    def test_stale_lease_is_failed_without_requeueing(self) -> None:
        lead_id = self._seed_lead()
        job_id = self._enqueue(lead_id)
        claim = self._claim("lease-worker")

        with self.session_factory() as db:
            self.assertFalse(
                email_generation_service.heartbeat_job(
                    db,
                    job_id=job_id,
                    worker_id="other-worker",
                )
            )
        with self.session_factory() as db:
            self.assertTrue(
                email_generation_service.heartbeat_job(
                    db,
                    job_id=job_id,
                    worker_id="lease-worker",
                )
            )
            job = db.get(EmailGenerationJob, job_id)
            job.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()

        with self.session_factory() as db:
            recovered = email_generation_service.recover_stale_jobs(
                db,
                stale_after_seconds=60,
            )

        self.assertEqual(recovered, 1)
        with self.session_factory() as db:
            job = db.get(EmailGenerationJob, job_id)
            run = db.get(AgentRun, claim.run_id)
            self.assertEqual(job.status, EmailGenerationJobStatus.system_error)
            self.assertEqual(job.error_code, "worker_lease_expired")
            self.assertEqual(job.attempt_count, 1)
            self.assertEqual(run.status, AgentRunStatus.system_error)
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(EmailGenerationJob)
                    .where(
                        EmailGenerationJob.status
                        == EmailGenerationJobStatus.queued
                    )
                ),
                0,
            )

    def test_terminal_failure_can_be_explicitly_retried(self) -> None:
        lead_id = self._seed_lead()
        first_id = self._enqueue(lead_id)
        claim = self._claim()
        agent = FakeAgent()
        agent.status = GenerationStatus.PROVIDER_ERROR
        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=agent,
            )

        second_key = str(uuid.uuid4())
        second_id = self._enqueue(lead_id, second_key)
        self.assertNotEqual(second_id, first_id)
        with self.session_factory() as db:
            second = db.get(EmailGenerationJob, second_id)
            self.assertEqual(second.trigger, EmailGenerationTrigger.retry)
            self.assertEqual(second.retry_of_job_id, first_id)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EmailGenerationJob)),
                2,
            )

    def test_generated_email_staleness_tracks_current_lead_input(self) -> None:
        lead_id = self._seed_lead()
        self._enqueue(lead_id)
        claim = self._claim()
        with self.session_factory() as db:
            email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=FakeAgent(),
            )

        with self.session_factory() as db:
            lead = db.get(Lead, lead_id)
            email = email_generation_service.current_email_for_lead(db, lead_id)
            self.assertFalse(
                email_generation_service.current_email_is_stale(lead, email)
            )
            lead.summary = "A material change after the draft was generated."
            db.commit()
            self.assertTrue(
                email_generation_service.current_email_is_stale(lead, email)
            )


class EmailGenerationWorkerConfigurationTests(unittest.TestCase):
    def test_missing_provider_configuration_exits_before_claiming(self) -> None:
        settings = Settings(
            _env_file=None,
            gemini_api_key="",
            gemini_model="fake-model",
        )
        with (
            patch.object(email_generation_worker, "_claim_one") as claim_one,
            patch.object(
                email_generation_worker.email_generator,
                "get_accoya_email_agent",
            ) as agent_factory,
        ):
            exit_code = email_generation_worker.run_worker(settings=settings)

        self.assertEqual(exit_code, 2)
        claim_one.assert_not_called()
        agent_factory.assert_not_called()

    def test_invalid_lease_timing_exits_before_provider_construction(self) -> None:
        settings = Settings(
            _env_file=None,
            gemini_api_key="test-key",
            gemini_model="fake-model",
            email_generation_heartbeat_seconds=30,
            email_generation_stale_seconds=30,
        )
        with patch.object(
            email_generation_worker.email_generator,
            "get_accoya_email_agent",
        ) as agent_factory:
            exit_code = email_generation_worker.run_worker(settings=settings)

        self.assertEqual(exit_code, 2)
        agent_factory.assert_not_called()


class EmailGenerationApiTests(unittest.TestCase):
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
        app.include_router(emails.router, prefix="/api")
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

    def _seed_lead(self, external_id: str = "api-queue-lead") -> str:
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="Civic terrace",
                location="Seattle, WA",
                state="WA",
                summary="An exterior timber opportunity.",
                contact_email="original-recipient@example.test",
                tags=["terrace"],
                raw_data={},
                source_feed="test/feed",
            )
            db.add(lead)
            db.commit()
            return str(lead.id)

    def _request_job(self, lead_id: str, key: str | None = None):
        return self.client.post(
            f"/api/leads/{lead_id}/email-generations",
            json={"idempotency_key": key or str(uuid.uuid4())},
        )

    def _run_next(self, agent: FakeAgent | None = None) -> tuple[str, str]:
        with self.session_factory() as db:
            claim = email_generation_service.claim_next_job(
                db,
                worker_id="api-test-worker",
            )
        self.assertIsNotNone(claim)
        with self.session_factory() as db:
            outcome = email_generation_service.execute_claimed_job(
                db,
                claim=claim,
                agent=agent or FakeAgent(),
            )
        self.assertEqual(outcome.status, EmailGenerationJobStatus.generated)
        with self.session_factory() as db:
            email_id = db.scalar(
                select(Email.id).where(Email.agent_run_id == claim.run_id)
            )
        return str(outcome.id), str(email_id)

    def test_manual_api_is_idempotent_and_reuses_active_job(self) -> None:
        lead_id = self._seed_lead()
        key = str(uuid.uuid4())

        first = self._request_job(lead_id, key)
        replay = self._request_job(lead_id, key)
        active = self._request_job(lead_id)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(active.status_code, 202)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        self.assertEqual(first.json()["id"], active.json()["id"])
        self.assertEqual(first.json()["lead_id"], lead_id)
        self.assertEqual(first.json()["trigger"], "manual")
        self.assertEqual(first.json()["status"], "queued")
        self.assertEqual(first.json()["idempotency_key"], key)
        self.assertEqual(first.json()["attempt_count"], 0)
        self.assertIsNone(first.json()["agent_run_id"])

        missing = self._request_job(str(uuid.uuid4()))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"detail": "Lead not found"})

    def test_idempotency_key_cannot_cross_leads(self) -> None:
        first_lead = self._seed_lead("first-api-lead")
        second_lead = self._seed_lead("second-api-lead")
        key = str(uuid.uuid4())
        self.assertEqual(self._request_job(first_lead, key).status_code, 202)

        conflict = self._request_job(second_lead, key)

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json(),
            {"detail": "Idempotency key is already in use"},
        )

    def test_workspace_and_email_read_expose_history_recipient_and_staleness(self):
        lead_id = self._seed_lead()
        queued = self._request_job(lead_id)
        self.assertEqual(queued.status_code, 202)

        before = self.client.get(f"/api/leads/{lead_id}/workspace")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["emails"], [])
        self.assertIsNone(before.json()["current_email_id"])
        self.assertFalse(before.json()["current_email_is_stale"])
        self.assertEqual(before.json()["latest_generation"]["status"], "queued")

        first_job_id, first_email_id = self._run_next()
        workspace = self.client.get(f"/api/leads/{lead_id}/workspace")
        self.assertEqual(workspace.status_code, 200)
        payload = workspace.json()
        self.assertEqual(payload["current_email_id"], first_email_id)
        self.assertEqual(
            payload["default_email_signature"],
            DEFAULT_US_EMAIL_SIGNATURE,
        )
        self.assertEqual(len(payload["emails"]), 1)
        self.assertEqual(
            payload["emails"][0]["signature"],
            DEFAULT_US_EMAIL_SIGNATURE,
        )
        self.assertEqual(
            payload["emails"][0]["rendered_body"],
            f"{FakeAgent().body}\n\n{DEFAULT_US_EMAIL_SIGNATURE}",
        )
        self.assertEqual(
            payload["emails"][0]["recipient_email"],
            "original-recipient@example.test",
        )
        self.assertEqual(payload["latest_generation"]["id"], first_job_id)
        self.assertEqual(payload["latest_generation"]["status"], "generated")
        self.assertFalse(payload["current_email_is_stale"])

        direct = self.client.get(f"/api/emails/{first_email_id}")
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(direct.json()["lead_id"], lead_id)
        self.assertEqual(
            direct.json()["recipient_email"],
            "original-recipient@example.test",
        )

        with self.session_factory() as db:
            lead = db.get(Lead, lead_id)
            lead.summary = "The opportunity changed after generation."
            lead.contact_email = "new-recipient@example.test"
            db.commit()

        changed = self.client.get(f"/api/leads/{lead_id}/workspace")
        self.assertTrue(changed.json()["current_email_is_stale"])
        self.assertEqual(
            changed.json()["emails"][0]["recipient_email"],
            "original-recipient@example.test",
        )

        with self.session_factory() as db:
            first_email = db.get(Email, first_email_id)
            first_email.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            first_job = db.get(EmailGenerationJob, first_job_id)
            first_job.queued_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            db.commit()
        regenerated = self._request_job(lead_id)
        self.assertEqual(regenerated.status_code, 202)
        second_job_id, second_email_id = self._run_next()

        history = self.client.get(f"/api/leads/{lead_id}/workspace").json()
        self.assertEqual(history["current_email_id"], second_email_id)
        self.assertEqual(
            [email["id"] for email in history["emails"]],
            [second_email_id, first_email_id],
        )
        self.assertEqual(
            history["emails"][0]["recipient_email"],
            "new-recipient@example.test",
        )
        self.assertFalse(history["current_email_is_stale"])
        self.assertEqual(history["latest_generation"]["id"], second_job_id)

        lead_list = self.client.get("/api/leads")
        self.assertEqual(lead_list.status_code, 200)
        self.assertEqual(
            lead_list.json()[0]["current_email"]["id"],
            second_email_id,
        )
        self.assertEqual(
            lead_list.json()[0]["latest_generation"]["id"],
            second_job_id,
        )

    def test_workspace_and_email_read_return_stable_404s(self) -> None:
        missing_workspace = self.client.get(
            f"/api/leads/{uuid.uuid4()}/workspace"
        )
        malformed_workspace = self.client.get("/api/leads/not-a-uuid/workspace")
        missing_email = self.client.get(f"/api/emails/{uuid.uuid4()}")
        malformed_email = self.client.get("/api/emails/not-a-uuid")

        self.assertEqual(missing_workspace.status_code, 404)
        self.assertEqual(malformed_workspace.status_code, 404)
        self.assertEqual(missing_email.status_code, 404)
        self.assertEqual(malformed_email.status_code, 404)
        self.assertEqual(missing_workspace.json(), {"detail": "Lead not found"})
        self.assertEqual(missing_email.json(), {"detail": "Email not found"})

    def test_workspace_backfills_fallback_for_legacy_low_context_run(self) -> None:
        lead_id = self._seed_lead("legacy-low-context")
        queued = self._request_job(lead_id)
        self.assertEqual(queued.status_code, 202)

        with self.session_factory() as db:
            claim = email_generation_service.claim_next_job(
                db,
                worker_id="legacy-backfill-worker",
            )
            self.assertIsNotNone(claim)
            job = db.get(EmailGenerationJob, claim.job_id)
            run = db.get(AgentRun, claim.run_id)
            terminal = datetime.now(timezone.utc)
            job.status = EmailGenerationJobStatus.insufficient_context
            job.error_code = EmailGenerationJobStatus.insufficient_context.value
            job.completed_at = terminal
            run.status = AgentRunStatus.insufficient_context
            run.error_code = EmailGenerationJobStatus.insufficient_context.value
            run.completed_at = terminal
            run.original_subject = None
            run.original_body = None
            db.commit()

            self.assertIsNone(
                db.scalar(select(Email.id).where(Email.agent_run_id == run.id))
            )

        workspace = self.client.get(f"/api/leads/{lead_id}/workspace")
        self.assertEqual(workspace.status_code, 200)
        payload = workspace.json()
        self.assertEqual(payload["latest_generation"]["status"], "insufficient_context")
        self.assertIsNotNone(payload["current_email_id"])
        self.assertEqual(len(payload["emails"]), 1)
        self.assertEqual(payload["emails"][0]["status"], "pending_review")
        self.assertIn("Accoya", payload["emails"][0]["subject"])
        self.assertIn("Depending on the application", payload["emails"][0]["body"])

    def test_archive_hides_lead_and_blocks_workspace_and_generation(self) -> None:
        archived_lead_id = self._seed_lead("archive-target")
        remaining_lead_id = self._seed_lead("archive-other")

        archive = self.client.delete(f"/api/leads/{archived_lead_id}")
        self.assertEqual(archive.status_code, 200)
        self.assertEqual(
            archive.json(),
            {"id": archived_lead_id, "archived": True},
        )

        list_response = self.client.get("/api/leads")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()), 1)
        self.assertEqual(list_response.json()[0]["id"], remaining_lead_id)

        workspace = self.client.get(f"/api/leads/{archived_lead_id}/workspace")
        self.assertEqual(workspace.status_code, 404)
        self.assertEqual(workspace.json(), {"detail": "Lead not found"})

        generation = self._request_job(archived_lead_id)
        self.assertEqual(generation.status_code, 404)
        self.assertEqual(generation.json(), {"detail": "Lead not found"})

        archive_again = self.client.delete(f"/api/leads/{archived_lead_id}")
        self.assertEqual(archive_again.status_code, 404)
        self.assertEqual(archive_again.json(), {"detail": "Lead not found"})

        with self.session_factory() as db:
            archived = db.get(Lead, archived_lead_id)
            self.assertIsNotNone(archived.archived_at)


if __name__ == "__main__":
    unittest.main()
