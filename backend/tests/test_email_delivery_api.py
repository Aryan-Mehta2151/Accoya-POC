"""Offline API tests for editing, approval, and delivery enqueueing."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import emails
from app.config import Settings
from app.db.database import Base, get_db
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    LeadReviewStatus,
    User,
)


class EmailDeliveryApiTests(unittest.TestCase):
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
        self.app = FastAPI()
        self.app.include_router(emails.router, prefix="/api")

        def override_db():
            with self.session_factory() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        self.user = User(
            id=str(uuid.uuid4()),
            email="reviewer@example.com",
            name="Offline Reviewer",
        )
        self.settings = Settings(
            jwt_secret_key="offline-jwt-secret",
            microsoft_client_id="client-id",
            microsoft_tenant_id="tenant-id",
            microsoft_client_secret="offline-secret",
            microsoft_sender_email="sender@example.com",
            microsoft_graph_timeout_seconds=10,
        )

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _authorize(self) -> None:
        self.app.dependency_overrides[emails.get_current_user] = lambda: self.user

    def _seed_email(
        self,
        *,
        status: EmailStatus = EmailStatus.pending_review,
        recipient_email: str | None = "architect@example.com",
        external_id: str = "api-delivery-lead",
        state: str | None = None,
        signature: str | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            lead = Lead(
                source_system="earlybid",
                external_id=external_id,
                project="Harbor boardwalk",
                state=state,
                contact_email=recipient_email,
                raw_data={},
            )
            db.add(lead)
            db.flush()
            run = AgentRun(
                lead_id=lead.id,
                status=AgentRunStatus.generated,
                input_hash="0" * 64,
                warnings=[],
                original_subject="Generated subject",
                original_body="Generated body",
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
                subject="Generated subject",
                body="Generated body",
                signature=signature,
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
            return str(email.id)

    def test_edit_recipient_blank_clears_and_approved_edit_requires_review(self):
        self._authorize()
        email_id = self._seed_email(status=EmailStatus.approved)
        response = self.client.patch(
            f"/api/emails/{email_id}",
            json={"recipient_email": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["recipient_email"])
        self.assertEqual(response.json()["status"], "pending_review")
        with self.session_factory() as db:
            event = db.scalar(
                select(EmailStatusEvent)
                .where(
                    EmailStatusEvent.email_id == email_id,
                    EmailStatusEvent.new_status == EmailStatus.pending_review,
                )
                .order_by(EmailStatusEvent.created_at.desc())
                .limit(1)
            )
            self.assertEqual(event.previous_status, EmailStatus.approved)
            self.assertEqual(event.actor, str(self.user.id))

    def test_invalid_recipient_edit_is_rejected(self) -> None:
        self._authorize()
        email_id = self._seed_email()
        response = self.client.patch(
            f"/api/emails/{email_id}",
            json={"recipient_email": "not-an-email"},
        )
        self.assertEqual(response.status_code, 422)

    def test_earlybid_deleted_lead_blocks_email_mutations(self) -> None:
        self._authorize()
        email_id = self._seed_email()
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            email.agent_run.lead.review_status = LeadReviewStatus.deleted
            db.commit()

        edit = self.client.patch(
            f"/api/emails/{email_id}",
            json={"subject": "Should not save"},
        )
        self.assertEqual(edit.status_code, 409)
        self.assertEqual(edit.json()["detail"]["code"], "lead_inactive")

        review = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(review.status_code, 409)
        self.assertEqual(review.json()["detail"]["code"], "lead_inactive")

    def test_approval_requires_valid_recipient_and_nonblank_body(self) -> None:
        self._authorize()
        missing_recipient_id = self._seed_email(recipient_email=None)
        response = self.client.post(
            f"/api/emails/{missing_recipient_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "recipient_invalid")

        email_id = self._seed_email(external_id="blank-body")
        with self.session_factory() as db:
            db.get(Email, email_id).body = "   "
            db.commit()
        response = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "body_invalid")

        blank_subject_id = self._seed_email(external_id="blank-subject")
        with self.session_factory() as db:
            db.get(Email, blank_subject_id).subject = "   "
            db.commit()
        response = self.client.post(
            f"/api/emails/{blank_subject_id}/status",
            json={"status": "approved"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "subject_invalid")

    def test_terminal_and_historical_emails_are_read_only(self) -> None:
        self._authorize()
        rejected_id = self._seed_email(
            status=EmailStatus.rejected,
            external_id="rejected-read-only",
        )
        sent_id = self._seed_email(
            status=EmailStatus.sent,
            external_id="sent-read-only",
        )
        for email_id in (rejected_id, sent_id):
            with self.subTest(email_id=email_id):
                response = self.client.patch(
                    f"/api/emails/{email_id}",
                    json={"body": "A forbidden change"},
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["detail"]["code"],
                    "email_read_only",
                )

        historical_id = self._seed_email(
            status=EmailStatus.approved,
            external_id="historical-read-only",
        )
        with self.session_factory() as db:
            historical = db.get(Email, historical_id)
            now = datetime.now(timezone.utc)
            newer_run = AgentRun(
                lead_id=historical.lead_id,
                status=AgentRunStatus.generated,
                input_hash="1" * 64,
                warnings=[],
                original_subject="New current subject",
                original_body="New current body",
                prompt_version="test",
                catalog_version="test",
                model_name="offline",
                model_calls=0,
                retrieval_count=0,
                started_at=now,
                completed_at=now,
            )
            db.add(newer_run)
            db.flush()
            db.add(
                Email(
                    agent_run_id=newer_run.id,
                    recipient_email="new@example.com",
                    subject="New current subject",
                    body="New current body",
                    status=EmailStatus.pending_review,
                    created_at=now,
                )
            )
            historical.created_at = now - timedelta(days=1)
            db.commit()

        response = self.client.patch(
            f"/api/emails/{historical_id}",
            json={"body": "A historical change"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "email_not_current")

        historical_payload = self.client.get(
            f"/api/emails/{historical_id}"
        ).json()
        with patch.object(emails, "get_settings", return_value=self.settings):
            response = self.client.post(
                f"/api/emails/{historical_id}/send",
                json={
                    "idempotency_key": str(uuid.uuid4()),
                    "expected_content_hash": historical_payload[
                        "delivery_content_hash"
                    ],
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "email_not_current")

    def test_direct_sent_transition_is_forbidden(self) -> None:
        self._authorize()
        for existing_status in (EmailStatus.approved, EmailStatus.sent):
            with self.subTest(existing_status=existing_status):
                email_id = self._seed_email(
                    status=existing_status,
                    external_id=f"direct-sent-{existing_status.value}",
                )
                response = self.client.post(
                    f"/api/emails/{email_id}/status",
                    json={"status": "sent"},
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["detail"]["code"],
                    "sent_requires_delivery",
                )

    def test_review_actor_is_always_the_authenticated_user(self) -> None:
        self._authorize()
        email_id = self._seed_email()
        preview = self.client.get(f"/api/emails/{email_id}").json()

        response = self.client.post(
            f"/api/emails/{email_id}/status",
            json={
                "status": "approved",
                "expected_content_hash": preview["delivery_content_hash"],
                "actor": "spoofed-client",
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.session_factory() as db:
            event = db.scalar(
                select(EmailStatusEvent)
                .where(
                    EmailStatusEvent.email_id == email_id,
                    EmailStatusEvent.new_status == EmailStatus.approved,
                )
                .order_by(EmailStatusEvent.created_at.desc())
                .limit(1)
            )
            self.assertEqual(event.actor, str(self.user.id))

    def test_signature_edit_changes_preview_hash_and_requires_fresh_approval(self) -> None:
        self._authorize()
        email_id = self._seed_email(state="OR", signature="Original signature")
        original = self.client.get(f"/api/emails/{email_id}").json()
        self.assertEqual(
            original["rendered_body"],
            "Generated body\n\nOriginal signature",
        )

        edited = self.client.patch(
            f"/api/emails/{email_id}",
            json={"signature": "Updated signature"},
        )
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.json()["signature"], "Updated signature")
        self.assertEqual(
            edited.json()["rendered_body"],
            "Generated body\n\nUpdated signature",
        )
        self.assertNotEqual(
            edited.json()["delivery_content_hash"],
            original["delivery_content_hash"],
        )

        stale = self.client.post(
            f"/api/emails/{email_id}/status",
            json={
                "status": "approved",
                "expected_content_hash": original["delivery_content_hash"],
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "content_changed")

        approved = self.client.post(
            f"/api/emails/{email_id}/status",
            json={
                "status": "approved",
                "expected_content_hash": edited.json()["delivery_content_hash"],
            },
        )
        self.assertEqual(approved.status_code, 200)

    def test_blank_signature_is_stored_as_null(self) -> None:
        self._authorize()
        email_id = self._seed_email(signature="Original signature")
        response = self.client.patch(
            f"/api/emails/{email_id}",
            json={"signature": "  \n  "},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["signature"])
        self.assertEqual(response.json()["rendered_body"], "Generated body")

    def test_edit_and_review_require_authentication(self) -> None:
        email_id = self._seed_email()

        edited = self.client.patch(
            f"/api/emails/{email_id}",
            json={"body": "Unauthenticated edit"},
        )
        reviewed = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "approved"},
        )

        self.assertEqual(edited.status_code, 401)
        self.assertEqual(reviewed.status_code, 401)
        with self.session_factory() as db:
            email = db.get(Email, email_id)
            self.assertEqual(email.body, "Generated body")
            self.assertEqual(email.status, EmailStatus.pending_review)

    def test_send_requires_authentication_before_queueing(self) -> None:
        email_id = self._seed_email(status=EmailStatus.approved)
        email_response = self.client.get(f"/api/emails/{email_id}")
        with patch.object(emails, "get_settings", return_value=self.settings):
            response = self.client.post(
                f"/api/emails/{email_id}/send",
                json={
                    "idempotency_key": str(uuid.uuid4()),
                    "expected_content_hash": email_response.json()[
                        "delivery_content_hash"
                    ],
                },
            )
        self.assertEqual(response.status_code, 401)
        with self.session_factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(EmailDeliveryJob)),
                0,
            )

    def test_send_returns_durable_job_and_is_idempotent(self) -> None:
        self._authorize()
        email_id = self._seed_email(status=EmailStatus.approved)
        email_response = self.client.get(f"/api/emails/{email_id}")
        content_hash = email_response.json()["delivery_content_hash"]
        key = str(uuid.uuid4())
        request = {
            "idempotency_key": key,
            "expected_content_hash": content_hash,
            "acknowledge_duplicate_risk": False,
        }
        with patch.object(emails, "get_settings", return_value=self.settings):
            first = self.client.post(
                f"/api/emails/{email_id}/send",
                json=request,
            )
            replay = self.client.post(
                f"/api/emails/{email_id}/send",
                json=request,
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["id"], first.json()["id"])
        self.assertEqual(first.json()["status"], "queued")
        self.assertEqual(first.json()["email_id"], email_id)
        self.assertEqual(first.json()["content_hash"], content_hash)
        self.assertEqual(first.json()["requested_by"], str(self.user.id))
        self.assertEqual(first.json()["recipient_email"], "architect@example.com")

        refreshed = self.client.get(f"/api/emails/{email_id}")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(
            refreshed.json()["latest_delivery"]["id"],
            first.json()["id"],
        )
        self.assertEqual(
            refreshed.json()["latest_delivery"]["status"],
            "queued",
        )
        self.assertFalse(refreshed.json()["has_unknown_delivery"])
        self.assertEqual(refreshed.json()["delivery_content_hash"], content_hash)

    def test_send_rejects_unconfigured_or_changed_content(self) -> None:
        self._authorize()
        email_id = self._seed_email(status=EmailStatus.approved)
        email_response = self.client.get(f"/api/emails/{email_id}")
        content_hash = email_response.json()["delivery_content_hash"]
        request = {
            "idempotency_key": str(uuid.uuid4()),
            "expected_content_hash": content_hash,
        }

        unconfigured = Settings(
            jwt_secret_key="offline-jwt-secret",
            microsoft_client_id="",
            microsoft_tenant_id="",
            microsoft_client_secret="",
        )
        with patch.object(emails, "get_settings", return_value=unconfigured):
            response = self.client.post(
                f"/api/emails/{email_id}/send",
                json=request,
            )
        self.assertEqual(response.status_code, 503)

        no_jwt_secret = self.settings.model_copy(
            update={"jwt_secret_key": ""}
        )
        with patch.object(emails, "get_settings", return_value=no_jwt_secret):
            response = self.client.post(
                f"/api/emails/{email_id}/send",
                json=request,
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "jwt_not_configured")

        request["expected_content_hash"] = "f" * 64
        request["idempotency_key"] = str(uuid.uuid4())
        with patch.object(emails, "get_settings", return_value=self.settings):
            response = self.client.post(
                f"/api/emails/{email_id}/send",
                json=request,
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "content_changed")

    def test_active_delivery_freezes_editing_and_review_actions(self) -> None:
        self._authorize()
        email_id = self._seed_email(status=EmailStatus.approved)
        email_response = self.client.get(f"/api/emails/{email_id}")
        with patch.object(emails, "get_settings", return_value=self.settings):
            queued = self.client.post(
                f"/api/emails/{email_id}/send",
                json={
                    "idempotency_key": str(uuid.uuid4()),
                    "expected_content_hash": email_response.json()[
                        "delivery_content_hash"
                    ],
                },
            )
        self.assertEqual(queued.status_code, 202)
        edited = self.client.patch(
            f"/api/emails/{email_id}",
            json={"subject": "Changed after queueing"},
        )
        self.assertEqual(edited.status_code, 409)
        self.assertEqual(edited.json()["detail"]["code"], "delivery_active")
        reviewed = self.client.post(
            f"/api/emails/{email_id}/status",
            json={"status": "rejected"},
        )
        self.assertEqual(reviewed.status_code, 409)
        self.assertEqual(reviewed.json()["detail"]["code"], "delivery_active")


if __name__ == "__main__":
    unittest.main()
