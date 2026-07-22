"""Offline integration tests for agent-backed email generation."""

from __future__ import annotations

import importlib
import os
import unittest
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Text, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent.models import GenerationResult, GenerationStatus
from app.api.routes import emails
from app.config import get_settings
from app.db.database import Base, get_db
from app.db.models import AgentRun, AgentRunStatus, Email, EmailStatus, Lead
from app.services import email_generator


class FakeAgent:
    """Deterministic fake implementing the production agent interface."""

    def __init__(
        self,
        *,
        status: GenerationStatus = GenerationStatus.GENERATED,
        subject: str = "An Accoya opportunity",
        body: str = "A concise outreach email.",
        warnings: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.subject = subject
        self.body = body
        self.warnings = list(warnings or [])
        self.error = error
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
            warnings=self.warnings,
            prompt_version="test",
        )


class CommitFailingSession(Session):
    """Session used to assert that persistence failures are rolled back."""

    rollback_called = False

    def commit(self) -> None:
        raise RuntimeError("database unavailable")

    def rollback(self) -> None:
        self.rollback_called = True
        super().rollback()


class EmailAgentIntegrationTests(unittest.TestCase):
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
        app = FastAPI()
        app.include_router(emails.router, prefix="/api")
        app.dependency_overrides[get_db] = self._get_db
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

    def _seed_lead(self, **updates: Any) -> Lead:
        values: dict[str, Any] = {
            "source_system": "earlybid",
            "external_id": "earlybid-100",
            "section": "Exterior",
            "project": "Riverside Walkway",
            "location": "Sacramento",
            "state": "CA",
            "signal": "Decking material review",
            "intelligence": "Architect reviewing options",
            "score": 91,
            "timing": "Design development",
            "next_step": "Offer a specification discussion",
            "awarded_to": None,
            "priority_reasons": "Exterior wood opportunity",
            "summary": "A public riverside walkway.",
            "contacts": "Jordan Lee, Architect",
            "contact_email": "jordan@example.com",
            "meeting_date": "2026-08-01",
            "tags": ["decking", "public realm"],
            "url": "https://example.test/lead/100",
            "raw_data": {
                "Next Step": "stale raw value",
                "Secret Internal Field": "must not reach the agent",
            },
            "source_feed": "test-feed",
        }
        values.update(updates)
        with self.session_factory() as db:
            lead = Lead(**values)
            db.add(lead)
            db.commit()
            db.refresh(lead)
            db.expunge(lead)
            return lead

    def _email_count(self) -> int:
        with self.session_factory() as db:
            return db.scalar(select(func.count()).select_from(Email)) or 0

    def test_success_maps_only_allowlisted_fields_and_persists_long_subject(self):
        lead = self._seed_lead()
        long_subject = "Accoya " + ("x" * 700)
        self.agent.subject = long_subject

        response = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["lead_id"], lead.id)
        self.assertEqual(payload["subject"], long_subject)
        self.assertEqual(payload["body"], self.agent.body)
        self.assertEqual(payload["status"], EmailStatus.pending_review.value)

        self.assertEqual(len(self.agent.calls), 1)
        agent_lead = self.agent.calls[0]
        self.assertEqual(agent_lead["lead_id"], lead.id)
        self.assertEqual(agent_lead["external_id"], lead.external_id)
        self.assertEqual(
            agent_lead["next_step"], "Offer a specification discussion"
        )
        self.assertNotIn("id", agent_lead)
        self.assertNotIn("raw_data", agent_lead)
        self.assertNotIn("Secret Internal Field", agent_lead)
        self.assertEqual(
            set(agent_lead),
            {
                "lead_id",
                "source_system",
                "external_id",
                "section",
                "project",
                "location",
                "state",
                "signal",
                "intelligence",
                "score",
                "timing",
                "next_step",
                "awarded_to",
                "priority_reasons",
                "summary",
                "contacts",
                "contact_email",
                "meeting_date",
                "tags",
                "url",
            },
        )

        with self.session_factory() as db:
            saved = db.scalar(select(Email))
            self.assertIsNotNone(saved)
            self.assertEqual(saved.subject, long_subject)
            self.assertEqual(saved.status, EmailStatus.pending_review)
        self.assertIsInstance(Email.__table__.c.subject.type, Text)

    def test_raw_payload_is_ignored_in_favor_of_first_class_next_step(self):
        lead = self._seed_lead(
            external_id="earlybid-raw-payload",
            next_step="Use the normalized next step",
            raw_data={"Next Step": "Do not use raw input", "secret": "hidden"},
        )

        response = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.agent.calls[0]["next_step"],
            "Use the normalized next step",
        )
        self.assertNotIn("raw_data", self.agent.calls[0])

    def test_missing_lead_returns_404_without_invoking_agent(self):
        response = self.client.post("/api/emails/generate/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Lead not found"})
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(self._email_count(), 0)

    def test_non_generated_statuses_return_structured_errors_without_rows(self):
        lead = self._seed_lead()
        cases = (
            (
                GenerationStatus.INSUFFICIENT_CONTEXT,
                422,
                "The lead does not contain enough context to generate an email.",
            ),
            (
                GenerationStatus.PROVIDER_ERROR,
                502,
                "The email generation provider could not produce a draft.",
            ),
        )
        for status, expected_code, expected_message in cases:
            with self.subTest(status=status):
                self.agent.status = status
                self.agent.warnings = ["safe warning"]

                response = self.client.post(f"/api/emails/generate/{lead.id}")

                self.assertEqual(response.status_code, expected_code)
                self.assertEqual(
                    response.json(),
                    {
                        "code": status.value,
                        "message": expected_message,
                        "warnings": ["safe warning"],
                    },
                )
                self.assertEqual(self._email_count(), 0)

    def test_repeated_generation_creates_separate_review_rows(self):
        lead = self._seed_lead()

        first = self.client.post(f"/api/emails/generate/{lead.id}")
        second = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self._email_count(), 2)

    def test_generated_and_edited_bodies_normalize_escaped_newlines(self):
        lead = self._seed_lead()
        self.agent.body = "Opening paragraph.\\n\\nSecond paragraph."

        generated = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(generated.status_code, 200)
        self.assertEqual(
            generated.json()["body"],
            "Opening paragraph.\n\nSecond paragraph.",
        )
        email_id = generated.json()["id"]

        edited = self.client.patch(
            f"/api/emails/{email_id}",
            json={"body": "First line.\\r\\n\\r\\nFinal line."},
        )

        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["body"], "First line.\n\nFinal line.")
        with self.session_factory() as db:
            saved = db.get(Email, email_id)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.body, "First line.\n\nFinal line.")

    def test_missing_contact_email_does_not_block_generation(self):
        lead = self._seed_lead(
            external_id="earlybid-no-recipient",
            contact_email=None,
            contacts=None,
        )

        response = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.agent.calls[0]["contact_email"])
        self.assertEqual(self._email_count(), 1)

    def test_email_edit_rejects_a_blank_subject_without_a_length_cap(self):
        lead = self._seed_lead()
        generated = self.client.post(f"/api/emails/generate/{lead.id}")
        email_id = generated.json()["id"]

        blank = self.client.patch(
            f"/api/emails/{email_id}",
            json={"subject": "   "},
        )
        long_subject = "y" * 900
        long = self.client.patch(
            f"/api/emails/{email_id}",
            json={"subject": long_subject},
        )

        self.assertEqual(blank.status_code, 422)
        self.assertEqual(long.status_code, 200)
        self.assertEqual(long.json()["subject"], long_subject)

    def test_unexpected_agent_exception_is_safe_and_persists_system_run(self):
        lead = self._seed_lead()
        self.agent.error = RuntimeError("provider secret should not be returned")

        response = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Email generation failed"})
        self.assertNotIn("provider secret", response.text)
        self.assertEqual(self._email_count(), 0)
        with self.session_factory() as db:
            run = db.scalar(select(AgentRun))
            self.assertIsNotNone(run)
            self.assertEqual(run.status, AgentRunStatus.system_error)
            self.assertEqual(run.error_code, "agent_execution_failed")

    def test_commit_failure_rolls_back_and_returns_safe_500(self):
        lead = self._seed_lead()
        failing_sessions: list[CommitFailingSession] = []

        def failing_db():
            db = CommitFailingSession(bind=self.engine, autoflush=False)
            failing_sessions.append(db)
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = failing_db

        response = self.client.post(f"/api/emails/generate/{lead.id}")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(), {"detail": "Generated email could not be saved"}
        )
        self.assertEqual(len(failing_sessions), 1)
        self.assertTrue(failing_sessions[0].rollback_called)
        self.assertEqual(self._email_count(), 0)

    def test_agent_provider_is_cached(self):
        sentinel = object()
        email_generator.get_accoya_email_agent.cache_clear()
        try:
            with patch.object(
                email_generator.AccoyaEmailAgent,
                "from_settings",
                return_value=sentinel,
            ) as from_settings:
                first = email_generator.get_accoya_email_agent()
                second = email_generator.get_accoya_email_agent()

            self.assertIs(first, sentinel)
            self.assertIs(second, sentinel)
            from_settings.assert_called_once_with()
        finally:
            email_generator.get_accoya_email_agent.cache_clear()


class AgentRouteRegistrationTests(unittest.TestCase):
    def test_agent_routes_are_only_registered_in_development(self):
        import app.main as main

        try:
            with patch.dict(os.environ, {"APP_ENV": "production"}):
                get_settings.cache_clear()
                production_main = importlib.reload(main)
                production_paths = set(production_main.app.openapi()["paths"])

            with patch.dict(os.environ, {"APP_ENV": "development"}):
                get_settings.cache_clear()
                development_main = importlib.reload(main)
                development_paths = set(development_main.app.openapi()["paths"])

            self.assertFalse(
                any(path.startswith("/api/agent/") for path in production_paths)
            )
            self.assertTrue(
                any(path.startswith("/api/agent/") for path in development_paths)
            )
            self.assertTrue(
                any(path.startswith("/api/agent-runs") for path in production_paths)
            )
        finally:
            get_settings.cache_clear()
            importlib.reload(main)


if __name__ == "__main__":
    unittest.main()
