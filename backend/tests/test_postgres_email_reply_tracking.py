"""Opt-in PostgreSQL coverage for reply uniqueness and mailbox leases.

Set ACCOYA_TEST_DATABASE_URL to an isolated PostgreSQL database whose name
ends in _test. The suite migrates that database and never calls Microsoft Graph.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import bootstrap, database
from app.db.models import (
    EmailReply,
    EmailReplyClassification,
    EmailReplyMatchMethod,
    GraphMailNotification,
    GraphMailboxSyncState,
)
from app.services import email_reply_service


TEST_DATABASE_URL = os.getenv("ACCOYA_TEST_DATABASE_URL")


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "Set ACCOYA_TEST_DATABASE_URL to run isolated PostgreSQL integration tests",
)
class PostgresEmailReplyTrackingTests(unittest.TestCase):
    """Exercise the reply migration's PostgreSQL-only safety guarantees."""

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
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._clean_rows()

    @classmethod
    def _clean_rows(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(GraphMailNotification.__table__.delete())
            connection.execute(EmailReply.__table__.delete())
            connection.execute(GraphMailboxSyncState.__table__.delete())

    def _reply(self, *, graph_id: str, internet_id: str) -> EmailReply:
        return EmailReply(
            id=str(uuid.uuid4()),
            mailbox_email="sender@example.com",
            graph_message_id=graph_id,
            internet_message_id=internet_id,
            reference_message_ids=[],
            sender_email="client@example.com",
            received_at=self.now,
            is_read=False,
            classification=EmailReplyClassification.unmatched,
            match_method=EmailReplyMatchMethod.none,
        )

    def test_migration_matches_metadata_only_contract(self) -> None:
        with self.engine.connect() as connection:
            self.assertEqual(
                set(database.get_current_schema_heads(connection)),
                set(database.get_expected_schema_heads()),
            )
        inspector = inspect(self.engine)
        columns = {
            column["name"]: column
            for column in inspector.get_columns("email_replies")
        }
        self.assertIsInstance(columns["reference_message_ids"]["type"], JSONB)
        self.assertNotIn("subject", columns)
        self.assertNotIn("body", columns)
        self.assertNotIn("preview", columns)
        unique_constraints = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("email_replies")
        }
        self.assertEqual(
            unique_constraints["uq_email_replies_mailbox_graph_message"],
            ["mailbox_email", "graph_message_id"],
        )
        self.assertEqual(
            unique_constraints["uq_email_replies_mailbox_internet_message"],
            ["mailbox_email", "internet_message_id"],
        )

    def test_graph_and_internet_message_ids_are_idempotently_unique(self) -> None:
        with self.session_factory() as db:
            db.add(self._reply(graph_id="immutable-1", internet_id="<reply-1@test>"))
            db.commit()

        for duplicate in (
            self._reply(graph_id="immutable-1", internet_id="<reply-2@test>"),
            self._reply(graph_id="immutable-2", internet_id="<reply-1@test>"),
        ):
            with self.session_factory() as db:
                db.add(duplicate)
                with self.assertRaises(IntegrityError):
                    db.commit()
                db.rollback()

    def test_locked_mailbox_row_is_skipped_and_only_one_lease_wins(self) -> None:
        with self.session_factory() as db:
            email_reply_service.ensure_mailbox_state(
                db,
                mailbox_email="sender@example.com",
                backfill_days=90,
                now=self.now,
            )

        contender_result: list[email_reply_service.MailboxSyncClaim | None] = []
        with self.session_factory() as locking_db:
            locking_db.scalar(
                select(GraphMailboxSyncState)
                .where(
                    GraphMailboxSyncState.mailbox_email == "sender@example.com"
                )
                .with_for_update()
            )

            def contend() -> None:
                with self.session_factory() as contender_db:
                    contender_result.append(
                        email_reply_service.claim_mailbox_sync(
                            contender_db,
                            mailbox_email="sender@example.com",
                            worker_id="contender",
                            now=self.now,
                        )
                    )

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(contender_result, [None])
            locking_db.rollback()

        with self.session_factory() as db:
            winner = email_reply_service.claim_mailbox_sync(
                db,
                mailbox_email="sender@example.com",
                worker_id="winner",
                now=self.now,
            )
            self.assertIsNotNone(winner)
            self.assertIsNone(
                email_reply_service.claim_mailbox_sync(
                    db,
                    mailbox_email="sender@example.com",
                    worker_id="second-worker",
                    now=self.now,
                )
            )


if __name__ == "__main__":
    unittest.main()
