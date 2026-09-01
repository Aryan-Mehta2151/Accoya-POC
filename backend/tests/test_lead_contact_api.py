"""Offline API coverage for editable opportunity contacts."""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import leads
from app.db.database import Base, get_db
from app.db.models import Lead, LeadReviewStatus


class LeadContactApiTests(unittest.TestCase):
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
        self.app.include_router(leads.router, prefix="/api")

        def override_db():
            with self.session_factory() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        with self.session_factory() as db:
            self.lead = Lead(
                source_system="earlybid",
                external_id="editable-contact",
                project="Harbor boardwalk",
                contacts="Original contact",
                contact_email="original@example.com",
                raw_data={},
            )
            db.add(self.lead)
            db.commit()
            self.lead_id = str(self.lead.id)

    def tearDown(self) -> None:
        self.client.close()
        self.app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_update_contact_persists_trimmed_values(self) -> None:
        response = self.client.patch(
            f"/api/leads/{self.lead_id}",
            json={"contacts": "  Jordan Lee  ", "contact_email": " jordan@example.com "},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contacts"], "Jordan Lee")
        self.assertEqual(response.json()["contact_email"], "jordan@example.com")
        with self.session_factory() as db:
            saved = db.get(Lead, self.lead_id)
            self.assertEqual(saved.contacts, "Jordan Lee")
            self.assertEqual(saved.contact_email, "jordan@example.com")

    def test_update_contact_allows_clearing_but_rejects_invalid_email(self) -> None:
        clear_response = self.client.patch(
            f"/api/leads/{self.lead_id}",
            json={"contacts": " ", "contact_email": ""},
        )
        self.assertEqual(clear_response.status_code, 200)
        self.assertIsNone(clear_response.json()["contacts"])
        self.assertIsNone(clear_response.json()["contact_email"])

        invalid_response = self.client.patch(
            f"/api/leads/{self.lead_id}",
            json={"contacts": "Jordan Lee", "contact_email": "invalid-email"},
        )
        self.assertEqual(invalid_response.status_code, 422)

    def test_partial_update_preserves_omitted_contact_field(self) -> None:
        response = self.client.patch(
            f"/api/leads/{self.lead_id}",
            json={"contacts": "Jordan Lee"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contacts"], "Jordan Lee")
        self.assertEqual(response.json()["contact_email"], "original@example.com")

    def test_earlybid_deleted_lead_cannot_be_updated(self) -> None:
        with self.session_factory() as db:
            lead = db.get(Lead, self.lead_id)
            lead.review_status = LeadReviewStatus.deleted
            db.commit()

        response = self.client.patch(
            f"/api/leads/{self.lead_id}",
            json={"contacts": "Jordan Lee"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "lead_inactive")
