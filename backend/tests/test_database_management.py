"""Provider-free tests for database schema validation and bootstrap helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text

from app.db import bootstrap, database
from app.db.models import AgentRun


class SchemaValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_missing_revision_is_rejected_and_head_is_accepted(self) -> None:
        expected_heads = database.get_expected_schema_heads()
        self.assertEqual(expected_heads, ("0002_agent_run_pagination_index",))

        with patch.object(database, "engine", self.engine):
            with self.assertRaisesRegex(RuntimeError, "not at the required"):
                database.check_database_schema()

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": "0001_agent_centric_baseline"},
            )

        with patch.object(database, "engine", self.engine):
            with self.assertRaisesRegex(RuntimeError, "not at the required"):
                database.check_database_schema()

        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": expected_heads[0]},
            )

        with patch.object(database, "engine", self.engine):
            database.check_database_schema()

    def test_agent_run_metadata_matches_defaults_and_pagination_query(self) -> None:
        model_calls = AgentRun.__table__.c.model_calls
        retrieval_count = AgentRun.__table__.c.retrieval_count

        self.assertEqual(model_calls.default.arg, 0)
        self.assertEqual(retrieval_count.default.arg, 0)
        self.assertEqual(str(model_calls.server_default.arg), "0")
        self.assertEqual(str(retrieval_count.server_default.arg), "0")

        pagination_index = next(
            index
            for index in AgentRun.__table__.indexes
            if index.name == "ix_agent_runs_started_at_id"
        )
        self.assertEqual(
            [column.name for column in pagination_index.columns],
            ["started_at", "id"],
        )


class BootstrapHelperTests(unittest.TestCase):
    def test_database_urls_derive_postgres_maintenance_database(self) -> None:
        target, maintenance, name = bootstrap._database_urls(
            "postgresql+psycopg2://user:secret@localhost:5433/accoya_agent"
        )

        self.assertEqual(name, "accoya_agent")
        self.assertEqual(target.database, "accoya_agent")
        self.assertEqual(maintenance.database, "postgres")
        self.assertEqual(maintenance.port, 5433)

    def test_database_urls_reject_non_postgres_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            bootstrap._database_urls("sqlite+pysqlite:///:memory:")

    def test_existing_database_skips_create_and_disposes_engine(self) -> None:
        connection = MagicMock()
        connection.scalar.return_value = 1
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value = connection

        with patch.object(bootstrap, "create_engine", return_value=engine):
            created = bootstrap.ensure_database_exists(
                "postgresql+psycopg2://user:secret@localhost:5433/accoya_agent"
            )

        self.assertFalse(created)
        connection.exec_driver_sql.assert_not_called()
        engine.dispose.assert_called_once_with()

    def test_bootstrap_upgrades_explicit_target_url(self) -> None:
        target_url = (
            "postgresql+psycopg2://user:secret@localhost:5433/accoya_agent_test"
        )
        with (
            patch.object(bootstrap, "ensure_database_exists", return_value=False),
            patch.object(bootstrap.command, "upgrade") as upgrade,
        ):
            created = bootstrap.bootstrap_database(target_url)

        self.assertFalse(created)
        config, revision = upgrade.call_args.args
        self.assertEqual(revision, "head")
        self.assertEqual(config.get_main_option("sqlalchemy.url"), target_url)


if __name__ == "__main__":
    unittest.main()
