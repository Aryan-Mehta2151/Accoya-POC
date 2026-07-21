"""Provision the configured PostgreSQL database and migrate it to head."""

from __future__ import annotations

from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.database import make_alembic_config


def _database_urls(database_url: str) -> tuple[URL, URL, str]:
    """Return target and maintenance URLs plus the validated target name."""

    target_url = make_url(database_url)
    if target_url.get_backend_name() != "postgresql":
        raise ValueError("Database bootstrap supports PostgreSQL URLs only")
    if not target_url.database:
        raise ValueError("DATABASE_URL must include a target database name")
    maintenance_url = target_url.set(database="postgres")
    return target_url, maintenance_url, target_url.database


def _database_exists(connection, database_name: str) -> bool:
    return bool(
        connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
            {"database_name": database_name},
        )
    )


def ensure_database_exists(database_url: str) -> bool:
    """Create the configured target database if needed; return whether created."""

    _, maintenance_url, database_name = _database_urls(database_url)
    maintenance_engine: Engine = create_engine(
        maintenance_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        with maintenance_engine.connect() as connection:
            if _database_exists(connection, database_name):
                return False

            quoted_name = connection.dialect.identifier_preparer.quote_identifier(
                database_name
            )
            try:
                connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
            except SQLAlchemyError:
                # A concurrent bootstrap may have won the create race.
                if not _database_exists(connection, database_name):
                    raise
            return True
    finally:
        maintenance_engine.dispose()


def bootstrap_database(database_url: str | None = None) -> bool:
    """Idempotently provision the configured database and apply all migrations."""

    configured_url = database_url or get_settings().database_url
    target_url, _, _ = _database_urls(configured_url)
    created = ensure_database_exists(configured_url)
    command.upgrade(
        make_alembic_config(target_url.render_as_string(hide_password=False)),
        "head",
    )
    return created


def main() -> None:
    """CLI entry point for ``python -m app.db.bootstrap``."""

    target_url, _, database_name = _database_urls(get_settings().database_url)
    created = bootstrap_database()
    action = "created" if created else "already existed"
    host = target_url.host or "localhost"
    port = target_url.port or 5432
    print(
        f"Database {database_name!r} {action} at {host}:{port}; "
        "Alembic migrations are at head."
    )


if __name__ == "__main__":
    main()
