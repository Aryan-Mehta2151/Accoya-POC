"""Database engine, sessions, and Alembic schema validation."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"
_ALEMBIC_DIR = _BACKEND_ROOT / "alembic"


class Base(DeclarativeBase):
    pass


def make_alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic config independent of the process working directory."""

    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    if database_url is not None:
        # Alembic's ConfigParser treats percent characters as interpolation.
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def get_expected_schema_heads() -> tuple[str, ...]:
    """Return all revision heads shipped with this backend."""

    script = ScriptDirectory.from_config(make_alembic_config())
    return tuple(script.get_heads())


def get_current_schema_heads(connection: Connection) -> tuple[str, ...]:
    """Return the migration revisions recorded by a database connection."""

    return tuple(MigrationContext.configure(connection).get_current_heads())


def check_database_schema() -> None:
    """Fail startup when the database is unreachable, missing, or behind."""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current_heads = set(get_current_schema_heads(connection))
    except SQLAlchemyError as exc:
        raise RuntimeError(
            "Database connectivity check failed. Verify DATABASE_URL and that "
            "PostgreSQL is running."
        ) from exc

    expected_heads = set(get_expected_schema_heads())
    if current_heads != expected_heads:
        current = ", ".join(sorted(current_heads)) or "none"
        expected = ", ".join(sorted(expected_heads)) or "none"
        raise RuntimeError(
            "Database schema is not at the required Alembic head "
            f"(current: {current}; expected: {expected}). Run "
            "`python -m app.db.bootstrap` from the backend directory."
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
