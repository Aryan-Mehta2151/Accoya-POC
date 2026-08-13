"""Provider-free tests for database schema validation and bootstrap helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text

from app.db import bootstrap, database
from app.db.models import AgentRun, EarlyBidSyncRun, EmailGenerationJob, User


class SchemaValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_missing_revision_is_rejected_and_head_is_accepted(self) -> None:
        expected_heads = database.get_expected_schema_heads()
        self.assertEqual(expected_heads, ("0009_email_signatures",))

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

    def test_user_metadata_enforces_normalized_identity_invariants(self) -> None:
        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in User.__table__.constraints
            if constraint.name and hasattr(constraint, "sqltext")
        }
        self.assertIn(
            "email = lower(trim(email))",
            checks["ck_users_email_normalized"],
        )
        self.assertIn(
            "length(trim(oauth_id)) > 0",
            checks["ck_users_oauth_identity_complete"],
        )

    def test_email_generation_job_metadata_enforces_queue_invariants(self) -> None:
        table = EmailGenerationJob.__table__

        self.assertEqual(table.c.status.default.arg.value, "queued")
        self.assertEqual(str(table.c.status.server_default.arg), "queued")
        self.assertEqual(table.c.attempt_count.default.arg, 0)
        self.assertEqual(str(table.c.attempt_count.server_default.arg), "0")
        self.assertTrue(table.c.idempotency_key.unique is None)

        idempotency_constraint = next(
            constraint
            for constraint in table.constraints
            if constraint.name == "uq_email_generation_jobs_idempotency_key"
        )
        self.assertEqual(
            [column.name for column in idempotency_constraint.columns],
            ["idempotency_key"],
        )

        active_index = next(
            index
            for index in table.indexes
            if index.name == "ix_email_generation_jobs_one_active_per_lead"
        )
        self.assertTrue(active_index.unique)
        self.assertEqual(
            [column.name for column in active_index.columns],
            ["lead_id"],
        )
        self.assertIn(
            "status IN ('queued', 'running')",
            str(active_index.dialect_options["postgresql"]["where"]),
        )

        job_link = AgentRun.__table__.c.email_generation_job_id
        self.assertTrue(job_link.nullable)
        self.assertTrue(
            any(
                constraint.name == "uq_agent_runs_email_generation_job_id"
                for constraint in AgentRun.__table__.constraints
            )
        )

    def test_earlybid_sync_metadata_enforces_schedule_and_attempt_invariants(self):
        table = EarlyBidSyncRun.__table__

        self.assertEqual(table.c.status.default.arg.value, "queued")
        self.assertEqual(str(table.c.status.server_default.arg), "queued")
        for name in (
            "attempt_count",
            "created_count",
            "updated_count",
            "total_count",
            "generation_queued_count",
        ):
            self.assertEqual(table.c[name].default.arg, 0)
            self.assertEqual(str(table.c[name].server_default.arg), "0")

        schedule_constraint = next(
            constraint
            for constraint in table.constraints
            if constraint.name
            == "uq_earlybid_sync_runs_feed_schedule_date"
        )
        self.assertEqual(
            [column.name for column in schedule_constraint.columns],
            ["reseller", "client", "schedule_date"],
        )
        due_index = next(
            index
            for index in table.indexes
            if index.name == "ix_earlybid_sync_runs_due"
        )
        self.assertEqual(
            [column.name for column in due_index.columns],
            ["status", "next_attempt_at", "scheduled_for"],
        )
        heartbeat_index = next(
            index
            for index in table.indexes
            if index.name == "ix_earlybid_sync_runs_heartbeat"
        )
        self.assertEqual(
            [column.name for column in heartbeat_index.columns],
            ["status", "heartbeat_at"],
        )
        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if constraint.name and hasattr(constraint, "sqltext")
        }
        self.assertIn(
            "created_count + updated_count <= total_count",
            checks["ck_earlybid_sync_runs_result_count_bounds"],
        )
        self.assertIn(
            "status = 'succeeded'",
            checks["ck_earlybid_sync_runs_terminal_result_shape"],
        )
        lifecycle = checks["ck_earlybid_sync_runs_lifecycle"]
        self.assertIn("status = 'failed'", lifecycle)
        self.assertIn("attempt_count = 0", lifecycle)
        self.assertIn("claimed_by IS NULL", lifecycle)


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
