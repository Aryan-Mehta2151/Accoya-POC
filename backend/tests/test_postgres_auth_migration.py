"""Opt-in PostgreSQL coverage for the 0006-to-0007 auth cutover.

Set ``ACCOYA_TEST_DATABASE_URL`` to an isolated PostgreSQL database whose name
ends in ``_test``. The suite temporarily moves only that test database between
revisions 0006 and 0007 and never calls an external service.
"""

from __future__ import annotations

import os
import unittest
import uuid

from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db import bootstrap, database


TEST_DATABASE_URL = os.getenv("ACCOYA_TEST_DATABASE_URL")
LEGACY_REVISION = "0006_email_delivery_queue"
AUTH_REVISION = "0007_web_auth_security"


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "Set ACCOYA_TEST_DATABASE_URL to run isolated PostgreSQL integration tests",
)
class PostgresAuthMigrationTests(unittest.TestCase):
    """Exercise legacy credential invalidation and atomic preflight failures."""

    @classmethod
    def setUpClass(cls) -> None:
        assert TEST_DATABASE_URL is not None
        parsed = make_url(TEST_DATABASE_URL)
        database_name = parsed.database or ""
        if parsed.get_backend_name() != "postgresql":
            raise RuntimeError("ACCOYA_TEST_DATABASE_URL must use PostgreSQL")
        if not database_name.endswith("_test"):
            raise RuntimeError(
                "Refusing auth migration test: database name must end in _test"
            )

        bootstrap.bootstrap_database(TEST_DATABASE_URL)
        cls.config = database.make_alembic_config(TEST_DATABASE_URL)
        cls.engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._clean_auth_rows()
            command.upgrade(cls.config, AUTH_REVISION)
        finally:
            cls.engine.dispose()

    def setUp(self) -> None:
        command.upgrade(self.config, AUTH_REVISION)
        self._clean_auth_rows()
        command.downgrade(self.config, LEGACY_REVISION)

    def tearDown(self) -> None:
        self._clean_auth_rows()
        command.upgrade(self.config, AUTH_REVISION)

    @classmethod
    def _clean_auth_rows(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("DELETE FROM password_reset_tokens"))
            connection.execute(text("DELETE FROM users"))

    def _insert_legacy_users(self, rows: list[dict[str, object]]) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO users (
                        id, email, name, password_hash, oauth_provider, oauth_id
                    ) VALUES (
                        :id, :email, :name, :password_hash,
                        :oauth_provider, :oauth_id
                    )
                    """
                ),
                rows,
            )

    @staticmethod
    def _legacy_user(
        email: str,
        *,
        oauth_provider: str | None = None,
        oauth_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "id": str(uuid.uuid4()),
            "email": email,
            "name": "Legacy User",
            "password_hash": "$2b$12$legacy-policy-password-hash",
            "oauth_provider": oauth_provider,
            "oauth_id": oauth_id,
        }

    def test_upgrade_invalidates_legacy_credentials_and_normalizes_identity(self):
        user = self._legacy_user(
            "  Mixed.Case@Example.COM ",
            oauth_provider="google",
            oauth_id="verified-google-subject",
        )
        self._insert_legacy_users([user])
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO password_reset_tokens (
                        id, user_id, token, expires_at
                    ) VALUES (
                        :id, :user_id, :token, now() + interval '15 minutes'
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user["id"],
                    "token": "legacy-plaintext-reset-secret",
                },
            )

        command.upgrade(self.config, AUTH_REVISION)

        with self.engine.connect() as connection:
            migrated = connection.execute(
                text(
                    """
                    SELECT email, password_hash, oauth_provider, oauth_id,
                           is_active, auth_version
                    FROM users
                    WHERE id = :id
                    """
                ),
                {"id": user["id"]},
            ).mappings().one()
            reset_count = connection.scalar(
                text("SELECT count(*) FROM password_reset_tokens")
            )
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )

        self.assertEqual(migrated["email"], "mixed.case@example.com")
        self.assertIsNone(migrated["password_hash"])
        self.assertEqual(migrated["oauth_provider"], "google")
        self.assertEqual(migrated["oauth_id"], "verified-google-subject")
        self.assertFalse(migrated["is_active"])
        self.assertEqual(migrated["auth_version"], 0)
        self.assertEqual(reset_count, 0)
        self.assertEqual(revision, AUTH_REVISION)
        self.assertIn(
            "token_hash",
            {column["name"] for column in inspect(self.engine).get_columns(
                "password_reset_tokens"
            )},
        )

    def test_dirty_identity_preflight_failures_are_atomic(self):
        cases = (
            (
                "normalized duplicate email",
                [
                    self._legacy_user(" Mixed@Example.com "),
                    self._legacy_user("mixed@example.com"),
                ],
                "duplicate emails after normalization",
            ),
            (
                "blank email",
                [self._legacy_user("   ")],
                "blank email address",
            ),
            (
                "incomplete OAuth identity",
                [self._legacy_user("one@example.com", oauth_provider="google")],
                "incomplete OAuth identity",
            ),
            (
                "duplicate OAuth identity",
                [
                    self._legacy_user(
                        "one@example.com",
                        oauth_provider="google",
                        oauth_id="same-subject",
                    ),
                    self._legacy_user(
                        "two@example.com",
                        oauth_provider="google",
                        oauth_id="same-subject",
                    ),
                ],
                "duplicate OAuth identity",
            ),
        )

        for index, (label, rows, error_text) in enumerate(cases):
            with self.subTest(case=label):
                if index:
                    self._clean_auth_rows()
                self._insert_legacy_users(rows)

                with self.assertRaisesRegex(Exception, error_text):
                    command.upgrade(self.config, AUTH_REVISION)

                with self.engine.connect() as connection:
                    revision = connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    stored = connection.execute(
                        text("SELECT email, password_hash FROM users ORDER BY email")
                    ).all()

                self.assertEqual(revision, LEGACY_REVISION)
                self.assertEqual(len(stored), len(rows))
                self.assertTrue(all(row.password_hash is not None for row in stored))
                self.assertNotIn(
                    "is_active",
                    {
                        column["name"]
                        for column in inspect(self.engine).get_columns("users")
                    },
                )
