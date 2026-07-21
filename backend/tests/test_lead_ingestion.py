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
from app.db.models import Lead
from app.schemas.lead import LeadRead
from app.services import lead_feed_service
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
        self.assertEqual(len(first_payload), 1)
        lead_uuid = first_payload[0]["id"]
        external_id = first_payload[0]["external_id"]

        second_csv = (
            "Project,Location,State,Score,Next Step\n"
            "Stable Opportunity,Portland,OR,10,Review the specification\n"
        )
        second = self.client.post(
            "/api/leads/upload-csv",
            files={"file": ("renamed.csv", second_csv, "text/csv")},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()[0]["id"], lead_uuid)
        self.assertEqual(second.json()[0]["external_id"], external_id)
        self.assertTrue(external_id.startswith("earlybid-natural-v1:"))
        self.assertEqual(self._lead_count(), 1)
        with self.session_factory() as db:
            persisted = db.scalar(select(Lead))
            self.assertEqual(persisted.score, 10)
            self.assertEqual(persisted.next_step, "Review the specification")
            self.assertEqual(persisted.source_feed, "upload:renamed.csv")
            self.assertEqual(persisted.raw_data["Score"], "10")
            self.assertNotIn("id", persisted.raw_data)
            self.assertNotIn("external_id", persisted.raw_data)

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
            },
        )
        with self.session_factory() as db:
            persisted = db.scalar(select(Lead))
            self.assertEqual(db.scalar(select(func.count()).select_from(Lead)), 1)
            self.assertEqual(persisted.id, first_uuid)
            self.assertEqual(persisted.score, 10)
            self.assertEqual(persisted.next_step, "Review the specification")


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
