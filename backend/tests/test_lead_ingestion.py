"""Provider-free tests for agent-normalized lead ingestion."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import leads
from app.db.database import Base, get_db
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
    Lead,
)
from app.schemas.lead import LeadRead
from app.services import (
    email_generation_service,
    email_generator,
    lead_feed_service,
)
from app.services.lead_feed_service import (
    LeadFeedValidationError,
    earlybid_identity_scope,
    normalized_row_fields,
    upsert_feed_rows,
)


class LeadRowNormalizationTests(unittest.TestCase):
    def test_agent_normalization_populates_projection_and_keeps_raw_json(self):
        row = {
            "id": "earlybid-42",
            "Project": " Riverside Walkway ",
            "Location": "Portland, Oregon",
            "Score": "1,234.5",
            "Next Step": " Offer a specification discussion ",
            "Contacts": "Taylor Smith, Architect, TAYLOR@EXAMPLE.COM",
            "Meeting Date": "Jul 17, 2026",
            "Tags": "#Decking, Planning; decking",
            "Feed-only Field": "retained only as raw JSON",
        }

        fields = normalized_row_fields(row, source_feed="reseller/client")

        self.assertEqual(fields["source_system"], "earlybid")
        self.assertEqual(fields["external_id"], "earlybid-42")
        self.assertEqual(fields["project"], "Riverside Walkway")
        self.assertEqual(fields["state"], "OR")
        self.assertEqual(fields["score"], 1234.5)
        self.assertEqual(fields["next_step"], "Offer a specification discussion")
        self.assertEqual(fields["contact_email"], "taylor@example.com")
        self.assertEqual(fields["meeting_date"], "Jul 17, 2026")
        self.assertEqual(fields["tags"], ["Decking", "Planning"])
        self.assertEqual(fields["raw_data"], row)
        self.assertEqual(fields["source_feed"], "reseller/client")

    def test_idless_row_uses_source_scope_and_keeps_raw_json_untouched(self):
        row = {
            "Project": "Bridge and Dock Repair",
            "Location": "Mount Pleasant",
            "State": "SC",
            "Score": "10",
            "Next Step": "Monitor the next review",
        }

        fields = normalized_row_fields(
            row,
            source_feed="amped/amped-accoya-materials",
            identity_scope=earlybid_identity_scope(
                "amped",
                "amped-accoya-materials",
            ),
        )

        self.assertTrue(fields["external_id"].startswith("earlybid-natural-v1:"))
        self.assertEqual(fields["raw_data"], row)
        self.assertNotIn("id", fields["raw_data"])
        self.assertNotIn("external_id", fields["raw_data"])

    def test_expanded_feed_fields_are_typed_and_raw_values_are_retained(self):
        row = {
            "id": "expanded-1",
            "Project": "Library Terrace",
            "reported": '{"source":"agenda"}',
            "due_date": "2026-09-10",
            "award_date": "2026-10-01",
            "start_date": "2027-01-15",
            "response_deadline_evidence": '[{"quote":"Due September 10"}]',
            "keywords_matched": "decking;public realm",
            "review_status": "deleted",
            "deleted_by": "operator",
            "deleted_reasons": "duplicate;out_of_scope",
        }

        fields = normalized_row_fields(row, source_feed="reseller/client")

        self.assertEqual(fields["reported"], {"source": "agenda"})
        self.assertEqual(str(fields["due_date"]), "2026-09-10")
        self.assertEqual(str(fields["award_date"]), "2026-10-01")
        self.assertEqual(str(fields["start_date"]), "2027-01-15")
        self.assertEqual(
            fields["response_deadline_evidence"],
            [{"quote": "Due September 10"}],
        )
        self.assertEqual(fields["keywords_matched"], ["decking", "public realm"])
        self.assertEqual(fields["review_status"].value, "deleted")
        self.assertEqual(fields["deleted_by"], "operator")
        self.assertEqual(fields["deleted_reasons"], ["duplicate", "out_of_scope"])
        self.assertEqual(fields["raw_data"], row)


class LeadUpsertTests(unittest.TestCase):
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

    def test_upsert_is_source_scoped_and_updates_agent_fields(self):
        with self.session_factory() as db:
            touched, created, updated = upsert_feed_rows(
                db,
                [
                    {"id": "same-id", "Project": "First", "Tags": "One"},
                ],
                source_feed="first/feed",
            )
            db.commit()
            self.assertEqual((len(touched), created, updated), (1, 1, 0))

            _, created, updated = upsert_feed_rows(
                db,
                [{"id": "same-id", "Project": "Other source"}],
                source_system="manual",
                source_feed="manual-upload",
            )
            db.commit()
            self.assertEqual((created, updated), (1, 0))

            _, created, updated = upsert_feed_rows(
                db,
                [
                    {
                        "id": "same-id",
                        "Project": "Updated",
                        "Next Step": "Review specification",
                        "Tags": "Decking, Public Realm",
                        "Unknown": "raw",
                    }
                ],
                source_feed="second/feed",
            )
            db.commit()
            self.assertEqual((created, updated), (0, 1))

            leads = db.scalars(select(Lead).order_by(Lead.source_system)).all()
            self.assertEqual(len(leads), 2)
            earlybid = next(
                lead for lead in leads if lead.source_system == "earlybid"
            )
            manual = next(lead for lead in leads if lead.source_system == "manual")
            self.assertEqual(earlybid.project, "Updated")
            self.assertEqual(earlybid.next_step, "Review specification")
            self.assertEqual(earlybid.tags, ["Decking", "Public Realm"])
            self.assertEqual(earlybid.raw_data["Unknown"], "raw")
            self.assertEqual(earlybid.source_feed, "second/feed")
            self.assertEqual(manual.project, "Other source")

    def test_duplicate_rows_collapse_to_one_current_projection(self):
        with self.session_factory() as db:
            touched, created, updated = upsert_feed_rows(
                db,
                [
                    {"id": "duplicate", "Project": "Old"},
                    {"id": "duplicate", "Project": "Current"},
                ],
                source_feed="test/feed",
            )
            db.commit()
            self.assertEqual((len(touched), created, updated), (1, 1, 0))
            self.assertEqual(db.scalar(select(Lead)).project, "Current")

    def test_identical_derived_rows_collapse(self):
        row = {
            "Project": "City Hall Terrace",
            "Location": "Portland",
            "State": "OR",
            "Score": "9",
        }
        with self.session_factory() as db:
            touched, created, updated = upsert_feed_rows(
                db,
                [row, dict(row)],
                source_feed="test/feed",
                identity_scope=earlybid_identity_scope("reseller", "client"),
            )
            db.commit()

            self.assertEqual((len(touched), created, updated), (1, 1, 0))
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 1)

    def test_invalid_natural_key_aborts_before_any_row_is_staged(self):
        with self.session_factory() as db:
            with self.assertRaises(LeadFeedValidationError) as raised:
                upsert_feed_rows(
                    db,
                    [
                        {"Project": "Valid", "Location": "Portland", "State": "OR"},
                        {"Project": "Missing location"},
                    ],
                    source_feed="test/feed",
                    identity_scope=earlybid_identity_scope("reseller", "client"),
                )

            self.assertEqual(
                [(issue.row_number, issue.reason_code) for issue in raised.exception.issues],
                [(3, "invalid_natural_identity")],
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 0)

    def test_conflicting_derived_identity_aborts_the_whole_feed(self):
        with self.session_factory() as db:
            with self.assertRaises(LeadFeedValidationError) as raised:
                upsert_feed_rows(
                    db,
                    [
                        {
                            "id": "N/A",
                            "Project": "Same Opportunity",
                            "Location": "Portland",
                            "State": "OR",
                            "Score": "8",
                        },
                        {
                            "id": "N/A",
                            "Project": "Same Opportunity",
                            "Location": "Portland",
                            "State": "OR",
                            "Score": "9",
                        },
                    ],
                    source_feed="test/feed",
                    identity_scope=earlybid_identity_scope("reseller", "client"),
                )

            self.assertEqual(
                [(issue.row_number, issue.reason_code) for issue in raised.exception.issues],
                [
                    (2, "derived_identity_collision"),
                    (3, "derived_identity_collision"),
                ],
            )
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 0)


class LeadIngestionApiTests(unittest.TestCase):
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

    def _lead_count(self) -> int:
        with self.session_factory() as db:
            return db.scalar(select(func.count()).select_from(Lead)) or 0

    def _job_count(self) -> int:
        with self.session_factory() as db:
            return (
                db.scalar(
                    select(func.count()).select_from(EmailGenerationJob)
                )
                or 0
            )

    def test_idless_upload_is_filename_independent_and_updates_mutable_fields(self):
        first_csv = (
            "Project,Location,State,Score,Next Step\n"
            "Stable Opportunity,Portland,OR,8,Make first contact\n"
        )
        first = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("first-name.csv", first_csv, "text/csv")},
        )
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()
        self.assertEqual(len(first_payload["items"]), 1)
        self.assertEqual(first_payload["created"], 1)
        self.assertEqual(first_payload["updated"], 0)
        self.assertEqual(first_payload["total"], 1)
        self.assertEqual(first_payload["generation_queued"], 1)
        lead_uuid = first_payload["items"][0]["id"]
        external_id = first_payload["items"][0]["external_id"]

        second_csv = (
            "Project,Location,State,Score,Next Step\n"
            "Stable Opportunity,Portland,OR,10,Review the specification\n"
        )
        second = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("renamed.csv", second_csv, "text/csv")},
        )

        self.assertEqual(second.status_code, 200)
        second_payload = second.json()
        self.assertEqual(second_payload["items"][0]["id"], lead_uuid)
        self.assertEqual(
            second_payload["items"][0]["external_id"], external_id
        )
        self.assertEqual(second_payload["created"], 0)
        self.assertEqual(second_payload["updated"], 1)
        self.assertEqual(second_payload["generation_queued"], 0)
        self.assertTrue(external_id.startswith("earlybid-natural-v1:"))
        self.assertEqual(self._lead_count(), 1)
        self.assertEqual(self._job_count(), 1)
        with self.session_factory() as db:
            persisted = db.scalar(select(Lead))
            job = db.scalar(select(EmailGenerationJob))
            self.assertEqual(persisted.score, 10)
            self.assertEqual(persisted.next_step, "Review the specification")
            self.assertEqual(persisted.source_feed, "upload:renamed.csv")
            self.assertEqual(persisted.raw_data["Score"], "10")
            self.assertNotIn("id", persisted.raw_data)
            self.assertNotIn("external_id", persisted.raw_data)
            self.assertEqual(job.trigger, EmailGenerationTrigger.csv_upload)
            self.assertEqual(job.idempotency_key, f"initial-v1:{lead_uuid}")

    def test_legacy_17_and_expanded_26_column_feeds_are_compatible(self):
        original_columns = (
            "id,Section,Project,Location,State,Signal,Intelligence,Score,Timing,"
            "Awarded To,Priority Reasons,Summary,Contacts,Meeting Date,Tags,URL,"
            "Next Step"
        )
        legacy_csv = (
            f"{original_columns}\n"
            "legacy-17,Public,Legacy Plaza,Portland,OR,Planning,Details,7,Q4,,"
            "Public project,Summary,Architect,Sep 1 2026,decking,"
            "https://example.test,Call\n"
        )
        legacy = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("legacy.csv", legacy_csv, "text/csv")},
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.json()["items"][0]["review_status"], "active")

        expanded_columns = (
            f"{original_columns},reported,due_date,award_date,start_date,"
            "response_deadline_evidence,keywords_matched,review_status,deleted_by,"
            "deleted_reasons"
        )
        self.assertEqual(len(expanded_columns.split(",")), 26)
        expanded_csv = (
            f"{expanded_columns}\n"
            "expanded-26,Public,Dismissed Plaza,Salem,OR,Planning,Details,8,Q1,,"
            "Public project,Summary,Engineer,Oct 1 2026,cladding,"
            "https://example.test,Review,true,2026-09-10,2026-10-01,2027-01-15,"
            "[],cladding;facade,deleted,operator,out_of_scope\n"
        )
        expanded = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("expanded.csv", expanded_csv, "text/csv")},
        )
        self.assertEqual(expanded.status_code, 200)
        item = expanded.json()["items"][0]
        self.assertEqual(item["review_status"], "deleted")
        self.assertEqual(item["due_date"], "2026-09-10")
        self.assertEqual(item["keywords_matched"], ["cladding", "facade"])
        self.assertEqual(expanded.json()["generation_queued"], 0)

    def test_upload_returns_422_and_stages_nothing_for_an_invalid_row(self):
        csv_text = (
            "Project,Location,State\n"
            "Valid Opportunity,Portland,OR\n"
            "Missing Location,,OR\n"
        )

        response = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("invalid.csv", csv_text, "text/csv")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "invalid_lead_feed",
                "message": "Lead feed validation failed",
                "issues": [{"row": 3, "reason": "invalid_natural_identity"}],
            },
        )
        self.assertEqual(self._lead_count(), 0)
        self.assertEqual(self._job_count(), 0)

    def test_upload_returns_422_for_a_conflicting_derived_identity(self):
        csv_text = (
            "Project,Location,State,Score\n"
            "Same Opportunity,Portland,OR,8\n"
            "Same Opportunity,Portland,OR,9\n"
        )

        response = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("collision.csv", csv_text, "text/csv")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["issues"],
            [
                {"row": 2, "reason": "derived_identity_collision"},
                {"row": 3, "reason": "derived_identity_collision"},
            ],
        )
        self.assertEqual(self._lead_count(), 0)
        self.assertEqual(self._job_count(), 0)

    def test_upload_rolls_back_new_leads_when_queue_staging_fails(self):
        csv_text = (
            "Project,Location,State\n"
            "Atomic Opportunity,Portland,OR\n"
        )
        with patch.object(
            email_generation_service,
            "enqueue_initial_generations",
            side_effect=RuntimeError("queue unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                self.client.post(
                    "/api/leads/upload-csv",
                    files={"file": ("atomic.csv", csv_text, "text/csv")},
                )

        self.assertEqual(self._lead_count(), 0)
        self.assertEqual(self._job_count(), 0)

    def test_sync_maps_feed_validation_to_502(self):
        csv_text = "Project,Location,State\nMissing Location,,OR\n"
        with patch.object(
            lead_feed_service,
            "fetch_latest_csv",
            return_value=csv_text,
        ):
            response = self.client.post(
                "/api/leads/sync",
                params={"reseller": "reseller", "client": "client"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"]["issues"],
            [{"row": 2, "reason": "invalid_natural_identity"}],
        )
        self.assertEqual(self._lead_count(), 0)
        self.assertEqual(self._job_count(), 0)

    def test_invalid_expanded_field_aborts_the_upload(self):
        csv_text = (
            "id,Project,review_status,due_date\n"
            "bad-expanded,Invalid Expanded,unknown,09/10/2026\n"
        )

        response = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("invalid-expanded.csv", csv_text, "text/csv")},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["issues"],
            [{"row": 2, "reason": "invalid_review_status"}],
        )
        self.assertEqual(self._lead_count(), 0)
        self.assertEqual(self._job_count(), 0)

    def test_malformed_expanded_json_and_date_are_rejected_atomically(self):
        cases = (
            ("reported", "{broken", "invalid_reported_json"),
            ("reported", "NaN", "invalid_reported_json"),
            ("due_date", "09/10/2026", "invalid_due_date"),
            ("due_date", "20260910", "invalid_due_date"),
            ("due_date", "2026-W37-4", "invalid_due_date"),
        )
        for column, value, reason in cases:
            with self.subTest(column=column):
                csv_text = (
                    f"id,Project,Location,State,review_status,{column}\n"
                    f"bad-{column},Invalid Expanded,Portland,OR,active,{value}\n"
                )
                response = self.client.post(
                    "/api/leads/upload-csv",
                    files={"file": ("invalid-expanded.csv", csv_text, "text/csv")},
                )

                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json()["detail"]["issues"],
                    [{"row": 2, "reason": reason}],
                )
                self.assertEqual(self._lead_count(), 0)
                self.assertEqual(self._job_count(), 0)

    def test_new_deleted_lead_queues_its_first_draft_only_when_activated(self):
        deleted_csv = (
            "id,Project,Location,State,review_status,deleted_by,deleted_reasons\n"
            "lifecycle-1,Future Active,Portland,OR,deleted,operator,not_ready\n"
        )
        deleted = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("deleted.csv", deleted_csv, "text/csv")},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["generation_queued"], 0)
        self.assertEqual(self.client.get("/api/leads").json(), [])
        self.assertEqual(len(self.client.get("/api/leads?view=dismissed").json()), 1)
        self.assertEqual(self._job_count(), 0)

        active_csv = deleted_csv.replace(
            "deleted,operator,not_ready",
            "active,,",
        )
        active = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("active.csv", active_csv, "text/csv")},
        )
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()["created"], 0)
        self.assertEqual(active.json()["updated"], 1)
        self.assertEqual(active.json()["generation_queued"], 1)
        self.assertEqual(len(self.client.get("/api/leads").json()), 1)
        self.assertEqual(self._job_count(), 1)

    def test_repeated_sync_updates_the_same_idless_lead(self):
        first_csv = (
            "Project,Location,State,Score,Next Step\n"
            "Synced Opportunity,Portland,OR,8,Make first contact\n"
        )
        second_csv = (
            "Project,Location,State,Score,Next Step\n"
            "Synced Opportunity,Portland,OR,10,Review the specification\n"
        )
        with patch.object(
            lead_feed_service,
            "fetch_latest_csv",
            side_effect=[first_csv, second_csv],
        ):
            first = self.client.post(
                "/api/leads/sync",
                params={"reseller": "reseller", "client": "client"},
            )
            with self.session_factory() as db:
                first_uuid = db.scalar(select(Lead.id))

            second = self.client.post(
                "/api/leads/sync",
                params={"reseller": "reseller", "client": "client"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            first.json(),
            {
                "created": 1,
                "updated": 0,
                "total": 1,
                "feed": "reseller/client",
                "generation_queued": 1,
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json(),
            {
                "created": 0,
                "updated": 1,
                "total": 1,
                "feed": "reseller/client",
                "generation_queued": 0,
            },
        )
        with self.session_factory() as db:
            persisted = db.scalar(select(Lead))
            job = db.scalar(select(EmailGenerationJob))
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 1)
            self.assertEqual(
                db.scalar(
                    select(func.count()).select_from(EmailGenerationJob)
                ),
                1,
            )
            self.assertEqual(persisted.id, first_uuid)
            self.assertEqual(persisted.score, 10)
            self.assertEqual(persisted.next_step, "Review the specification")
            self.assertEqual(job.trigger, EmailGenerationTrigger.earlybid_sync)

    def test_sync_queues_without_instantiating_or_calling_the_agent(self):
        csv_text = (
            "Project,Location,State,Score\n"
            "Queued Opportunity,Portland,OR,8\n"
        )
        with (
            patch.object(
                lead_feed_service,
                "fetch_latest_csv",
                return_value=csv_text,
            ),
            patch.object(
                email_generator,
                "get_accoya_email_agent",
            ) as agent_factory,
        ):
            response = self.client.post(
                "/api/leads/sync",
                params={"reseller": "reseller", "client": "client"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generation_queued"], 1)
        agent_factory.assert_not_called()
        self.assertEqual(self._lead_count(), 1)
        self.assertEqual(self._job_count(), 1)

    def test_deleted_and_reopened_lead_preserves_outreach_history(self):
        active_csv = (
            "Project,Location,State,Score,review_status\n"
            "Fresh Again Opportunity,Portland,OR,8,active\n"
        )
        first = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("first.csv", active_csv, "text/csv")},
        )
        self.assertEqual(first.status_code, 200)
        lead_id = first.json()["items"][0]["id"]

        with self.session_factory() as db:
            run = AgentRun(
                lead_id=lead_id,
                status=AgentRunStatus.running,
                input_hash="a" * 64,
                prompt_version="test-v1",
                catalog_version="test-v1",
                model_name="test-model",
            )
            db.add(run)
            db.flush()
            email = Email(
                agent_run_id=run.id,
                recipient_email="approver@example.test",
                subject="Reviewed draft",
                body="Body",
                status=EmailStatus.approved,
            )
            db.add(email)
            db.flush()
            db.add(
                EmailDeliveryJob(
                    email_id=email.id,
                    requested_by="offline-reviewer",
                    idempotency_key="queued-before-source-delete",
                    content_hash="b" * 64,
                    message_id="<queued-before-source-delete@example.test>",
                    sender_email="sender@example.test",
                    recipient_email="approver@example.test",
                    subject="Reviewed draft",
                    body_snapshot="Body",
                )
            )
            db.commit()

        deleted_csv = active_csv.replace(",active\n", ",deleted\n")
        deleted = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("deleted.csv", deleted_csv, "text/csv")},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/api/leads").json(), [])
        dismissed = self.client.get("/api/leads", params={"view": "dismissed"})
        self.assertEqual(dismissed.status_code, 200)
        self.assertEqual(dismissed.json()[0]["id"], lead_id)
        dismissed_workspace = self.client.get(f"/api/leads/{lead_id}/workspace")
        self.assertEqual(dismissed_workspace.status_code, 200)
        self.assertEqual(
            dismissed_workspace.json()["emails"][0]["status"],
            "approved",
        )
        with self.session_factory() as db:
            queued = db.scalar(
                select(EmailGenerationJob).where(
                    EmailGenerationJob.idempotency_key == f"initial-v1:{lead_id}"
                )
            )
            self.assertEqual(queued.status, EmailGenerationJobStatus.system_error)
            self.assertEqual(queued.error_code, "lead_inactive")
            delivery = db.scalar(select(EmailDeliveryJob))
            self.assertEqual(delivery.status, EmailDeliveryJobStatus.failed)
            self.assertEqual(delivery.error_code, "lead_inactive")
            self.assertEqual(delivery.attempt_count, 0)

        restored = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("restored.csv", active_csv, "text/csv")},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["created"], 0)
        self.assertEqual(restored.json()["updated"], 1)
        self.assertEqual(restored.json()["generation_queued"], 0)

        after_restore = self.client.get("/api/leads")
        self.assertEqual(after_restore.status_code, 200)
        self.assertEqual(len(after_restore.json()), 1)
        self.assertEqual(after_restore.json()[0]["id"], lead_id)
        self.assertEqual(
            after_restore.json()[0]["current_email"]["status"],
            "approved",
        )


class LeadWireCompatibilityTests(unittest.TestCase):
    def test_native_uuid_and_json_tags_keep_existing_wire_types(self):
        lead = SimpleNamespace(
            id=uuid4(),
            external_id="wire-1",
            section=None,
            project="Project",
            location=None,
            state=None,
            signal=None,
            intelligence=None,
            score=91.5,
            timing=None,
            awarded_to=None,
            priority_reasons=None,
            summary=None,
            contacts=None,
            contact_email=None,
            meeting_date=None,
            tags=["Decking", "Public Realm"],
            url=None,
            source_feed="test/feed",
            created_at=datetime.now(timezone.utc),
        )

        payload = LeadRead.model_validate(lead).model_dump(mode="json")

        self.assertIsInstance(payload["id"], str)
        self.assertEqual(payload["tags"], "Decking, Public Realm")
        self.assertEqual(payload["score"], 91.5)
        self.assertNotIn("next_step", payload)
        self.assertNotIn("source_system", payload)
