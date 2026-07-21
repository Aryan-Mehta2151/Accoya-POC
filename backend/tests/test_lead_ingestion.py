"""Provider-free tests for agent-normalized lead ingestion."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.models import Lead
from app.schemas.lead import LeadRead
from app.services.lead_feed_service import normalized_row_fields, upsert_feed_rows


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
                    {"Project": "missing stable id"},
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
