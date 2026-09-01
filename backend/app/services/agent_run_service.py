"""Persistence orchestration for synchronous Accoya email-agent runs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from agent.catalog import CATALOG_VERSION
from agent.models import GenerationResult, GenerationStatus
from agent.prompts import PROMPT_VERSION
from app.config import get_settings
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    LeadReviewStatus,
)
from app.services.email_generator import EmailAgent, build_agent_lead


logger = logging.getLogger(__name__)

SYSTEM_ERROR_CODE = "agent_execution_failed"
INVALID_RESULT_CODE = "invalid_agent_result"
TERMINAL_PERSISTENCE_CODE = "terminal_persistence_failed"

_TERMINAL_STATUSES = {
    AgentRunStatus.generated,
    AgentRunStatus.insufficient_context,
    AgentRunStatus.provider_error,
    AgentRunStatus.system_error,
}


class LeadNotFoundError(LookupError):
    """The requested lead does not exist."""


class AgentRunNotFoundError(LookupError):
    """The requested persisted run does not exist."""


class AgentRunNotRetryableError(ValueError):
    """A running attempt cannot be retried."""


class InvalidRunCursorError(ValueError):
    """A list continuation cursor is malformed."""


class AgentRunPersistenceError(RuntimeError):
    """The database could not durably record an agent-run transition."""


class AgentRunSystemError(RuntimeError):
    """An unexpected agent failure that has already been safely persisted."""

    def __init__(self, run_id: str, code: str = SYSTEM_ERROR_CODE) -> None:
        super().__init__(code)
        self.run_id = run_id
        self.code = code


@dataclass(frozen=True)
class AgentRunPageResult:
    """Internal list result independent of the HTTP schema layer."""

    items: list[AgentRun]
    next_cursor: str | None


def execute_agent_run(
    db: Session,
    *,
    lead_id: str,
    agent: EmailAgent,
    retry_of_run_id: str | None = None,
) -> AgentRun:
    """Create, execute, and terminally persist one independent agent attempt.

    The initial commit is deliberately completed before ``agent.generate`` is
    invoked. No ORM value is accessed between that commit and the provider call,
    which keeps the synchronous external work outside a database transaction.
    """

    try:
        canonical_lead_id = _canonical_uuid(lead_id)
    except ValueError:
        raise LeadNotFoundError(lead_id) from None
    lead = db.get(Lead, canonical_lead_id)
    if lead is None:
        raise LeadNotFoundError(lead_id)
    if lead.review_status is not LeadReviewStatus.active:
        raise LeadNotFoundError(lead_id)

    curated_input = build_agent_lead(lead)
    input_hash = hash_curated_input(curated_input)
    recipient_email = lead.contact_email
    stable_lead_id = str(lead.id)
    run_id = str(uuid.uuid4())
    started_at = _utc_now()
    configured_model = get_settings().gemini_model

    run = AgentRun(
        id=run_id,
        lead_id=stable_lead_id,
        retry_of_run_id=retry_of_run_id,
        input_hash=input_hash,
        status=AgentRunStatus.running,
        warnings=[],
        prompt_version=PROMPT_VERSION,
        catalog_version=CATALOG_VERSION,
        model_name=configured_model,
        model_calls=0,
        retrieval_count=0,
        started_at=started_at,
    )
    try:
        db.add(run)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Agent run could not be initialized",
            extra={"lead_id": stable_lead_id, "agent_run_id": run_id},
        )
        raise AgentRunPersistenceError(
            "Agent run could not be initialized"
        ) from exc

    try:
        result = agent.generate(curated_input)
    except Exception:
        _persist_system_error(db, run_id=run_id, code=SYSTEM_ERROR_CODE)
        logger.error(
            "Accoya agent execution failed",
            extra={
                "lead_id": stable_lead_id,
                "agent_run_id": run_id,
                "agent_status": AgentRunStatus.system_error.value,
            },
        )
        raise AgentRunSystemError(run_id) from None

    try:
        terminal_status = _terminal_status(result)
        _persist_terminal_result(
            db,
            run_id=run_id,
            result=result,
            status=terminal_status,
            recipient_email=recipient_email,
        )
    except AgentRunSystemError:
        raise
    except AgentRunPersistenceError:
        raise
    except Exception:
        db.rollback()
        _persist_system_error(db, run_id=run_id, code=INVALID_RESULT_CODE)
        logger.error(
            "Accoya agent returned an invalid result",
            extra={"lead_id": stable_lead_id, "agent_run_id": run_id},
        )
        raise AgentRunSystemError(run_id, INVALID_RESULT_CODE) from None

    completed_run = db.get(AgentRun, run_id)
    if completed_run is None:
        raise AgentRunPersistenceError("Completed agent run could not be loaded")
    _log_terminal_run(completed_run)
    return completed_run


def retry_agent_run(
    db: Session,
    *,
    run_id: str,
    agent: EmailAgent,
) -> AgentRun:
    """Execute a new linked attempt using the lead's current projection."""

    try:
        canonical_run_id = _canonical_uuid(run_id)
    except ValueError:
        raise AgentRunNotFoundError(run_id) from None
    previous = db.get(AgentRun, canonical_run_id)
    if previous is None:
        raise AgentRunNotFoundError(run_id)
    if previous.status not in _TERMINAL_STATUSES:
        raise AgentRunNotRetryableError(run_id)

    lead_id = previous.lead_id
    return execute_agent_run(
        db,
        lead_id=lead_id,
        agent=agent,
        retry_of_run_id=canonical_run_id,
    )


def get_agent_run(db: Session, run_id: str) -> AgentRun:
    """Load one run or raise the service-layer not-found error."""

    try:
        canonical_run_id = _canonical_uuid(run_id)
    except ValueError:
        raise AgentRunNotFoundError(run_id) from None
    run = db.get(AgentRun, canonical_run_id)
    if run is None:
        raise AgentRunNotFoundError(run_id)
    return run


def list_agent_runs(
    db: Session,
    *,
    lead_id: str | None = None,
    status: AgentRunStatus | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> AgentRunPageResult:
    """List runs newest-first using a stable time-and-ID continuation cursor."""

    statement = select(AgentRun)
    if lead_id is not None:
        try:
            canonical_lead_id = _canonical_uuid(lead_id)
        except ValueError:
            return AgentRunPageResult(items=[], next_cursor=None)
        statement = statement.where(AgentRun.lead_id == canonical_lead_id)
    if status is not None:
        statement = statement.where(AgentRun.status == status)
    if cursor is not None:
        cursor_started_at, cursor_id = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                AgentRun.started_at < cursor_started_at,
                and_(
                    AgentRun.started_at == cursor_started_at,
                    AgentRun.id < cursor_id,
                ),
            )
        )

    rows = list(
        db.scalars(
            statement.order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(
                limit + 1
            )
        ).all()
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1]) if has_more and items else None
    return AgentRunPageResult(items=items, next_cursor=next_cursor)


def hash_curated_input(curated_input: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 digest without persisting the payload."""

    serialized = json.dumps(
        curated_input,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_canonical_json_default,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _terminal_status(result: GenerationResult) -> AgentRunStatus:
    if result.status is GenerationStatus.GENERATED:
        if not result.subject or not result.body:
            raise ValueError("A generated result requires a draft")
        return AgentRunStatus.generated
    if result.status is GenerationStatus.INSUFFICIENT_CONTEXT:
        return AgentRunStatus.insufficient_context
    if result.status is GenerationStatus.PROVIDER_ERROR:
        return AgentRunStatus.provider_error
    raise ValueError("Unsupported agent result status")


def _persist_terminal_result(
    db: Session,
    *,
    run_id: str,
    result: GenerationResult,
    status: AgentRunStatus,
    recipient_email: str | None,
) -> None:
    try:
        run = db.get(AgentRun, run_id)
        if run is None:
            raise AgentRunPersistenceError("Running agent record was not found")
        if run.status is not AgentRunStatus.running:
            raise AgentRunPersistenceError("Agent run is already terminal")

        telemetry = result.telemetry
        token_usage = telemetry.token_usage
        run.status = status
        run.selected_product_family = result.selected_product_family
        run.selected_application = result.selected_application
        run.nurturing_email_number = result.nurturing_email_number
        run.nurturing_email_theme = result.nurturing_email_theme
        run.warnings = list(result.warnings)
        run.error_code = None if status is AgentRunStatus.generated else status.value
        run.original_subject = result.subject
        run.original_body = result.body
        run.prompt_version = result.prompt_version or run.prompt_version
        run.catalog_version = CATALOG_VERSION
        run.model_name = telemetry.model_name or run.model_name
        run.model_calls = telemetry.model_calls
        run.retrieval_count = telemetry.retrieval_count
        run.input_tokens = token_usage.input_tokens
        run.output_tokens = token_usage.output_tokens
        run.total_tokens = token_usage.total_tokens
        run.latency_ms = telemetry.latency_ms
        run.completed_at = _utc_now()

        if status is AgentRunStatus.generated:
            email_id = str(uuid.uuid4())
            email = Email(
                id=email_id,
                agent_run_id=run_id,
                recipient_email=recipient_email,
                subject=result.subject,
                body=result.body,
                status=EmailStatus.pending_review,
            )
            db.add(email)
            db.add(
                EmailStatusEvent(
                    id=str(uuid.uuid4()),
                    email_id=email_id,
                    previous_status=None,
                    new_status=EmailStatus.pending_review,
                    actor=None,
                )
            )
        db.commit()
    except AgentRunPersistenceError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        try:
            _persist_system_error(
                db,
                run_id=run_id,
                code=TERMINAL_PERSISTENCE_CODE,
            )
        except AgentRunPersistenceError as persistence_exc:
            raise AgentRunPersistenceError(
                "Agent run terminal outcome could not be saved"
            ) from persistence_exc
        raise AgentRunSystemError(run_id, TERMINAL_PERSISTENCE_CODE) from exc


def _persist_system_error(db: Session, *, run_id: str, code: str) -> None:
    db.rollback()
    try:
        run = db.get(AgentRun, run_id)
        if run is None:
            raise AgentRunPersistenceError("Running agent record was not found")
        if run.status is not AgentRunStatus.running:
            return
        run.status = AgentRunStatus.system_error
        run.error_code = code
        run.warnings = []
        run.completed_at = _utc_now()
        db.commit()
    except AgentRunPersistenceError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise AgentRunPersistenceError(
            "Agent system failure could not be recorded"
        ) from exc


def _log_terminal_run(run: AgentRun) -> None:
    logger.info(
        "Accoya agent run completed",
        extra={
            "lead_id": run.lead_id,
            "agent_run_id": run.id,
            "agent_status": run.status.value,
            "agent_warning_count": len(run.warnings),
            "agent_model_calls": run.model_calls,
            "agent_retrieval_count": run.retrieval_count,
            "agent_latency_ms": run.latency_ms,
        },
    )


def _encode_cursor(run: AgentRun) -> str:
    payload = json.dumps(
        {"started_at": run.started_at.isoformat(), "id": run.id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        started_at = datetime.fromisoformat(payload["started_at"])
        run_id = payload["id"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError
        return started_at, _canonical_uuid(run_id)
    except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise InvalidRunCursorError(cursor) from exc


def _canonical_json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported curated input value: {type(value).__name__}")


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "AgentRunNotFoundError",
    "AgentRunNotRetryableError",
    "AgentRunPageResult",
    "AgentRunPersistenceError",
    "AgentRunSystemError",
    "InvalidRunCursorError",
    "LeadNotFoundError",
    "execute_agent_run",
    "get_agent_run",
    "hash_curated_input",
    "list_agent_runs",
    "retry_agent_run",
]
