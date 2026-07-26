"""Provider-free tests for persistent agent-run orchestration and APIs."""

from __future__ import annotations

import unittest
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent.models import (
    GenerationResult,
    GenerationStatus,
    GenerationTelemetry,
    TokenUsage,
)
from app.api.routes import agent_runs, emails
from app.db.database import Base, get_db
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    User,
)
from app.services import agent_run_service, email_generator


class FakeAgent:
    """Configurable synchronous fake for the production agent protocol."""

    def __init__(self) -> None:
        self.status = GenerationStatus.GENERATED
        self.subject = "Accoya follow-up"
        self.body = "A provider-free draft."
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
            warnings=["safe test warning"],
            prompt_version="test-prompt-v1",
            telemetry=GenerationTelemetry(
                model_name="fake-gemini",
                prompt_version="test-prompt-v1",
                latency_ms=37,
                token_usage=TokenUsage(
                    input_tokens=11,
                    output_tokens=7,
                    total_tokens=18,
                ),
                model_calls=3,
                retrieval_count=4,
            ),
        )


class AgentRunTests(unittest.TestCase):
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
        self.agent = FakeAgent()
        self.current_user = User(
            id=str(uuid.uuid4()),
            email="reviewer@example.com",
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
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _get_db(self):
        with self.session_factory() as db:
            yield db

    def _seed_lead(self, external_id: str = "run-lead") -> str:
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="Waterfront boardwalk",
                location="Sacramento, CA",
                signal="Decking specification",
                score=92,
                timing="Design development",
                next_step="Offer a technical review",
                summary="An exterior public-realm project.",
                contacts="Alex Rivera <alex@example.test>",
                contact_email="alex@example.test",
                tags=["decking", "public realm"],
                raw_data={"private_feed_field": "never forward"},
                source_feed="test/feed",
            )
            db.add(lead)
            db.commit()
            return str(lead.id)

    def test_running_commit_precedes_provider_and_generated_is_atomic(self):
        lead_id = self._seed_lead()
        self.agent.subject = "Long subject " + ("x" * 700)

        with self.session_factory() as db:
            def observe_running(payload: dict[str, Any]) -> None:
                self.assertFalse(db.in_transaction())
                self.assertNotIn("raw_data", payload)
                self.assertEqual(payload["next_step"], "Offer a technical review")
                with self.session_factory() as observer:
                    persisted = observer.scalar(select(AgentRun))
                    self.assertIsNotNone(persisted)
                    self.assertEqual(persisted.status, AgentRunStatus.running)
                    self.assertIsNone(persisted.completed_at)

            self.agent.observer = observe_running
            run = agent_run_service.execute_agent_run(
                db,
                lead_id=lead_id,
                agent=self.agent,
            )

        self.assertEqual(run.status, AgentRunStatus.generated)
        self.assertEqual(len(run.input_hash), 64)
        self.assertEqual(run.original_subject, self.agent.subject)
        self.assertEqual(run.model_name, "fake-gemini")
        self.assertEqual(run.model_calls, 3)
        self.assertEqual(run.retrieval_count, 4)
        self.assertEqual(run.total_tokens, 18)

        with self.session_factory() as db:
            email = db.scalar(select(Email))
            event = db.scalar(select(EmailStatusEvent))
            self.assertIsNotNone(email)
            self.assertEqual(email.agent_run_id, run.id)
            self.assertEqual(email.subject, self.agent.subject)
            self.assertEqual(email.recipient_email, "alex@example.test")
            self.assertEqual(email.status, EmailStatus.pending_review)
            self.assertIsNotNone(event)
            self.assertIsNone(event.previous_status)
            self.assertEqual(event.new_status, EmailStatus.pending_review)

    def test_expected_and_unexpected_failures_persist_without_email(self):
        lead_id = self._seed_lead()
        expected = (
            (GenerationStatus.INSUFFICIENT_CONTEXT, AgentRunStatus.insufficient_context),
            (GenerationStatus.PROVIDER_ERROR, AgentRunStatus.provider_error),
        )
        with self.session_factory() as db:
            for generation_status, run_status in expected:
                with self.subTest(status=generation_status):
                    self.agent.status = generation_status
                    run = agent_run_service.execute_agent_run(
                        db,
                        lead_id=lead_id,
                        agent=self.agent,
                    )
                    self.assertEqual(run.status, run_status)
                    self.assertEqual(run.error_code, run_status.value)
                    self.assertIsNone(run.original_subject)

            self.agent.error = RuntimeError("provider detail must stay private")
            with self.assertRaises(agent_run_service.AgentRunSystemError) as raised:
                agent_run_service.execute_agent_run(
                    db,
                    lead_id=lead_id,
                    agent=self.agent,
                )

        with self.session_factory() as db:
            system_run = db.get(AgentRun, raised.exception.run_id)
            self.assertIsNotNone(system_run)
            self.assertEqual(system_run.status, AgentRunStatus.system_error)
            self.assertEqual(system_run.error_code, "agent_execution_failed")
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Email)),
                0,
            )

    def test_retry_uses_current_lead_and_keeps_prior_outcome_immutable(self):
        lead_id = self._seed_lead()
        with self.session_factory() as db:
            first = agent_run_service.execute_agent_run(
                db,
                lead_id=lead_id,
                agent=self.agent,
            )
            first_hash = first.input_hash
            first_subject = first.original_subject

            lead = db.get(Lead, lead_id)
            self.assertIsNotNone(lead)
            lead.summary = "Updated current lead context."
            db.commit()

            self.agent.subject = "A different retry draft"
            retry = agent_run_service.retry_agent_run(
                db,
                run_id=first.id,
                agent=self.agent,
            )

        self.assertNotEqual(retry.id, first.id)
        self.assertEqual(retry.retry_of_run_id, first.id)
        self.assertNotEqual(retry.input_hash, first_hash)
        with self.session_factory() as db:
            original = db.get(AgentRun, first.id)
            self.assertEqual(original.input_hash, first_hash)
            self.assertEqual(original.original_subject, first_subject)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Email)),
                2,
            )

    def test_run_read_api_paginates_and_mutation_adapters_queue(self):
        lead_id = self._seed_lead()
        created_ids: list[str] = []
        with self.session_factory() as db:
            for _ in range(3):
                run = agent_run_service.execute_agent_run(
                    db,
                    lead_id=lead_id,
                    agent=self.agent,
                )
                created_ids.append(run.id)

        first_page = self.client.get("/api/agent-runs", params={"limit": 2})
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(len(first_page.json()["items"]), 2)
        cursor = first_page.json()["next_cursor"]
        self.assertIsNotNone(cursor)
        second_page = self.client.get(
            "/api/agent-runs",
            params={"limit": 2, "cursor": cursor, "status": "generated"},
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(len(second_page.json()["items"]), 1)
        self.assertIsNone(second_page.json()["next_cursor"])

        retry = self.client.post(f"/api/agent-runs/{created_ids[0]}/retry")
        self.assertEqual(retry.status_code, 202)
        self.assertEqual(retry.json()["trigger"], "retry")
        self.assertEqual(retry.json()["status"], "queued")
        self.assertEqual(len(self.agent.calls), 3)

        compatibility = self.client.post(
            "/api/agent-runs",
            json={"lead_id": lead_id},
        )
        self.assertEqual(compatibility.status_code, 202)
        self.assertEqual(compatibility.json()["id"], retry.json()["id"])
        self.assertEqual(len(self.agent.calls), 3)

    def test_email_edits_and_status_events_do_not_change_original_draft(self):
        lead_id = self._seed_lead()
        with self.session_factory() as db:
            generated = agent_run_service.execute_agent_run(
                db,
                lead_id=lead_id,
                agent=self.agent,
            )
            run_id = generated.id
        with self.session_factory() as db:
            email = db.scalar(select(Email).where(Email.agent_run_id == run_id))
            email_id = email.id
            original_subject = email.subject
            original_body = email.body

        edited = self.client.patch(
            f"/api/emails/{email_id}",
            json={
                "recipient_email": "reviewer@example.com",
                "subject": "Reviewed subject",
                "body": "Reviewed body",
            },
        )
        self.assertEqual(edited.status_code, 200)
        approved = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(approved.status_code, 200)
        unchanged = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(unchanged.status_code, 200)
        rejected = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "rejected"},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["status"], EmailStatus.rejected.value)

        with self.session_factory() as db:
            run = db.get(AgentRun, run_id)
            email = db.get(Email, email_id)
            events = list(
                db.scalars(
                    select(EmailStatusEvent).where(
                        EmailStatusEvent.email_id == email_id
                    )
                ).all()
            )
            self.assertEqual(run.original_subject, original_subject)
            self.assertEqual(run.original_body, original_body)
            self.assertEqual(email.subject, "Reviewed subject")
            self.assertEqual(email.body, "Reviewed body")
            self.assertEqual(email.status, EmailStatus.rejected)
            self.assertEqual(len(events), 3)
            approved_event = next(
                event for event in events if event.new_status is EmailStatus.approved
            )
            self.assertEqual(
                approved_event.previous_status,
                EmailStatus.pending_review,
            )
            self.assertEqual(approved_event.actor, str(self.current_user.id))
            rejected_event = next(
                event for event in events if event.new_status is EmailStatus.rejected
            )
            self.assertEqual(rejected_event.previous_status, EmailStatus.approved)
            self.assertEqual(rejected_event.actor, str(self.current_user.id))

    def test_email_routes_return_404_for_invalid_identifiers(self):
        cases = ("not-a-uuid", str(uuid.uuid4()))

        for email_id in cases:
            with self.subTest(email_id=email_id, endpoint="edit"):
                response = self.client.patch(
                    f"/api/emails/{email_id}",
                    json={"subject": "Reviewed subject"},
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "Email not found"})

            with self.subTest(email_id=email_id, endpoint="status"):
                response = self.client.post(
                    f"/api/emails/{email_id}/status",
                    json={"status": "approved"},
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"detail": "Email not found"})

    def test_running_run_is_not_retryable(self):
        lead_id = self._seed_lead()
        run_id = str(uuid.uuid4())
        with self.session_factory() as db:
            db.add(
                AgentRun(
                    id=run_id,
                    lead_id=lead_id,
                    status=AgentRunStatus.running,
                    input_hash="0" * 64,
                    warnings=[],
                    prompt_version="test",
                    catalog_version="test",
                    model_name="fake",
                    model_calls=0,
                    retrieval_count=0,
                )
            )
            db.commit()

        response = self.client.post(f"/api/agent-runs/{run_id}/retry")
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
