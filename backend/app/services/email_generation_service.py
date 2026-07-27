"""Durable queue orchestration for asynchronous outreach generation."""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from agent.catalog import CATALOG_VERSION
from agent.models import GenerationResult, GenerationStatus
from agent.workflow import LOW_CONTEXT_WARNING_CODE
from agent.prompts import PROMPT_VERSION
from app.config import get_settings
from app.db.models import (
    AgentRun,
    AgentRunStatus,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailGenerationJob,
    EmailGenerationJobStatus,
    EmailGenerationTrigger,
    EmailStatus,
    EmailStatusEvent,
    Lead,
)
from app.services.agent_run_service import hash_curated_input
from app.services.email_generator import EmailAgent, build_agent_lead


logger = logging.getLogger(__name__)

ACTIVE_JOB_STATUSES = (
    EmailGenerationJobStatus.queued,
    EmailGenerationJobStatus.running,
)
FAILED_JOB_STATUSES = (
    EmailGenerationJobStatus.insufficient_context,
    EmailGenerationJobStatus.provider_error,
    EmailGenerationJobStatus.system_error,
)
WORKER_LEASE_EXPIRED = "worker_lease_expired"
PROVIDER_TIMEOUT = "provider_timeout"
AGENT_EXECUTION_FAILED = "agent_execution_failed"
INVALID_AGENT_RESULT = "invalid_agent_result"
TERMINAL_PERSISTENCE_FAILED = "terminal_persistence_failed"


class LeadNotFoundError(LookupError):
    """The requested lead does not exist."""


class EmailGenerationJobNotFoundError(LookupError):
    """The requested queue job does not exist."""


class IdempotencyKeyConflictError(ValueError):
    """An idempotency key already belongs to another lead."""


class EmailGenerationConflictError(ValueError):
    '''Generation is blocked by an unresolved outbound delivery.'''

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EmailGenerationPersistenceError(RuntimeError):
    """The queue request could not be durably recorded."""


class EmailGenerationJobNotRunningError(RuntimeError):
    """A provider result arrived after the job stopped being active."""


class AgentCallTimeoutError(TimeoutError):
    """The configured end-to-end provider-call timeout elapsed."""


@dataclass(frozen=True)
class ClaimedEmailGeneration:
    """Detached provider input returned after the claim transaction commits."""

    job_id: str
    run_id: str
    lead_id: str
    curated_input: dict[str, Any]
    recipient_email: str | None


def enqueue_initial_generations(
    db: Session,
    leads: list[Lead],
    *,
    trigger: EmailGenerationTrigger,
) -> list[EmailGenerationJob]:
    """Stage first-draft jobs in the caller's lead-ingestion transaction."""

    jobs: list[EmailGenerationJob] = []
    for lead in leads:
        if lead.id is None:
            raise ValueError("Lead must be flushed before generation is queued")
        job = EmailGenerationJob(
            lead_id=str(lead.id),
            trigger=trigger,
            status=EmailGenerationJobStatus.queued,
            requested_input_hash=hash_curated_input(build_agent_lead(lead)),
            idempotency_key=f"initial-v1:{lead.id}",
            attempt_count=0,
        )
        db.add(job)
        jobs.append(job)
    return jobs


def enqueue_generation(
    db: Session,
    *,
    lead_id: str,
    idempotency_key: str,
    trigger: EmailGenerationTrigger = EmailGenerationTrigger.manual,
    retry_of_job_id: str | None = None,
) -> EmailGenerationJob:
    """Idempotently commit one manual job, or return an already-active job."""

    try:
        canonical_lead_id = _canonical_uuid(lead_id)
    except ValueError:
        raise LeadNotFoundError(lead_id) from None

    key = idempotency_key.strip()
    if not key:
        raise ValueError("idempotency_key must not be blank")

    existing = db.scalar(
        select(EmailGenerationJob).where(
            EmailGenerationJob.idempotency_key == key
        )
    )
    if existing is not None:
        if existing.lead_id != canonical_lead_id:
            raise IdempotencyKeyConflictError(key)
        return existing

    lead = db.scalar(
        select(Lead)
        .where(
            Lead.id == canonical_lead_id,
            Lead.archived_at.is_(None),
        )
        .with_for_update()
    )
    if lead is None:
        raise LeadNotFoundError(lead_id)

    active = _latest_job(db, canonical_lead_id, statuses=ACTIVE_JOB_STATUSES)
    if active is not None:
        return active

    current_email = current_email_for_lead(db, canonical_lead_id)
    if current_email is not None:
        blocking_delivery = db.scalar(
            select(EmailDeliveryJob.id)
            .where(
                EmailDeliveryJob.email_id == current_email.id,
                EmailDeliveryJob.status.in_(
                    (
                        EmailDeliveryJobStatus.queued,
                        EmailDeliveryJobStatus.running,
                        EmailDeliveryJobStatus.delivery_unknown,
                    )
                ),
            )
            .limit(1)
        )
        if blocking_delivery is not None:
            raise EmailGenerationConflictError(
                'delivery_blocks_generation',
                'Resolve the current email delivery before generating another draft',
            )

    previous = _latest_job(db, canonical_lead_id)
    linked_job_id = retry_of_job_id or (previous.id if previous else None)
    effective_trigger = trigger
    if trigger is EmailGenerationTrigger.manual and previous is not None:
        if previous.status in FAILED_JOB_STATUSES:
            effective_trigger = EmailGenerationTrigger.retry

    job = EmailGenerationJob(
        lead_id=canonical_lead_id,
        retry_of_job_id=linked_job_id,
        trigger=effective_trigger,
        status=EmailGenerationJobStatus.queued,
        requested_input_hash=hash_curated_input(build_agent_lead(lead)),
        idempotency_key=key,
        attempt_count=0,
    )
    db.add(job)
    try:
        db.commit()
        db.refresh(job)
        return job
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(
            select(EmailGenerationJob).where(
                EmailGenerationJob.idempotency_key == key
            )
        )
        if replay is not None and replay.lead_id == canonical_lead_id:
            return replay
        active = _latest_job(db, canonical_lead_id, statuses=ACTIVE_JOB_STATUSES)
        if active is not None:
            return active
        raise EmailGenerationPersistenceError(
            "Email generation request could not be queued"
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.error(
            "Email generation request could not be queued",
            extra={"lead_id": canonical_lead_id},
        )
        raise EmailGenerationPersistenceError(
            "Email generation request could not be queued"
        ) from exc


def get_generation_job(db: Session, job_id: str) -> EmailGenerationJob:
    """Load one queue job by canonical UUID."""

    try:
        canonical_job_id = _canonical_uuid(job_id)
    except ValueError:
        raise EmailGenerationJobNotFoundError(job_id) from None
    job = db.get(EmailGenerationJob, canonical_job_id)
    if job is None:
        raise EmailGenerationJobNotFoundError(job_id)
    return job


def latest_generation_job(
    db: Session, lead_id: str
) -> EmailGenerationJob | None:
    """Return a lead's newest job using a stable timestamp-and-ID order."""

    return _latest_job(db, lead_id)


def emails_for_lead(db: Session, lead_id: str) -> list[Email]:
    """Return immutable-history order with the current email first."""

    return list(
        db.scalars(
            select(Email)
            .join(AgentRun, Email.agent_run_id == AgentRun.id)
            .where(AgentRun.lead_id == lead_id)
            .options(selectinload(Email.delivery_jobs))
            .order_by(Email.created_at.desc(), Email.id.desc())
        ).all()
    )


def current_email_for_lead(db: Session, lead_id: str) -> Email | None:
    """Return the newest generated email for one lead."""

    return db.scalar(
        select(Email)
        .join(AgentRun, Email.agent_run_id == AgentRun.id)
        .where(AgentRun.lead_id == lead_id)
        .order_by(Email.created_at.desc(), Email.id.desc())
        .limit(1)
    )


def ensure_low_context_fallback_email(db: Session, lead_id: str) -> bool:
    """Backfill one fallback draft for legacy insufficient-context runs.

    Older runs may have terminal insufficient-context status without a persisted
    draft. This helper creates a single pending-review fallback email so the UI
    can always open a draft workspace.
    """

    latest = _latest_job(db, lead_id)
    if latest is None or latest.status is not EmailGenerationJobStatus.insufficient_context:
        return False
    if latest.agent_run is None:
        return False

    existing_email = db.scalar(
        select(Email.id)
        .where(Email.agent_run_id == latest.agent_run.id)
        .limit(1)
    )
    if existing_email is not None:
        return False

    lead = db.get(Lead, lead_id)
    subject, body = _fallback_draft_for_low_context(lead)
    email_id = str(uuid.uuid4())
    db.add(
        Email(
            id=email_id,
            agent_run_id=latest.agent_run.id,
            recipient_email=lead.contact_email if lead is not None else None,
            subject=subject,
            body=body,
            status=EmailStatus.pending_review,
        )
    )
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
    return True


def current_email_is_stale(lead: Lead, email: Email | None) -> bool:
    """Compare a draft's captured agent input with the current projection."""

    if email is None:
        return False
    return email.agent_run.input_hash != hash_curated_input(build_agent_lead(lead))


def claim_next_job(
    db: Session,
    *,
    worker_id: str,
) -> ClaimedEmailGeneration | None:
    """Claim the oldest job with SKIP LOCKED and durably initialize its run."""

    worker_id = worker_id.strip()
    if not worker_id:
        raise ValueError("worker_id must not be blank")

    job = db.scalar(
        select(EmailGenerationJob)
        .where(EmailGenerationJob.status == EmailGenerationJobStatus.queued)
        .order_by(EmailGenerationJob.queued_at, EmailGenerationJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None

    lead = db.get(Lead, job.lead_id)
    if lead is None:
        # The cascading foreign key should make this unreachable.
        db.rollback()
        raise LeadNotFoundError(job.lead_id)

    curated_input = build_agent_lead(lead)
    actual_input_hash = hash_curated_input(curated_input)
    recipient_email = lead.contact_email
    stable_job_id = str(job.id)
    stable_lead_id = str(lead.id)
    now = _utc_now()
    run_id = str(uuid.uuid4())
    retry_of_run_id = None
    if job.retry_of is not None and job.retry_of.agent_run is not None:
        retry_of_run_id = job.retry_of.agent_run.id

    job.status = EmailGenerationJobStatus.running
    job.claimed_by = worker_id
    job.claimed_at = now
    job.heartbeat_at = now
    job.attempt_count += 1
    run = AgentRun(
        id=run_id,
        lead_id=job.lead_id,
        retry_of_run_id=retry_of_run_id,
        email_generation_job_id=job.id,
        status=AgentRunStatus.running,
        input_hash=actual_input_hash,
        warnings=[],
        prompt_version=PROMPT_VERSION,
        catalog_version=CATALOG_VERSION,
        model_name=get_settings().gemini_model,
        model_calls=0,
        retrieval_count=0,
        started_at=now,
    )
    db.add(run)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error(
            "Email generation job claim failed",
            extra={
                "email_generation_job_id": stable_job_id,
                "worker_id": worker_id,
            },
        )
        raise

    return ClaimedEmailGeneration(
        job_id=stable_job_id,
        run_id=run_id,
        lead_id=stable_lead_id,
        curated_input=curated_input,
        recipient_email=recipient_email,
    )


def heartbeat_job(db: Session, *, job_id: str, worker_id: str) -> bool:
    """Renew a running job's lease only for the worker that claimed it."""

    job = db.scalar(
        select(EmailGenerationJob)
        .where(EmailGenerationJob.id == job_id)
        .with_for_update()
    )
    if (
        job is None
        or job.status is not EmailGenerationJobStatus.running
        or job.claimed_by != worker_id
    ):
        db.rollback()
        return False
    job.heartbeat_at = _utc_now()
    db.commit()
    return True


def recover_stale_jobs(db: Session, *, stale_after_seconds: float) -> int:
    """Terminally fail expired leases; provider calls are never replayed."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    cutoff = _utc_now() - timedelta(seconds=stale_after_seconds)
    jobs = list(
        db.scalars(
            select(EmailGenerationJob)
            .where(
                EmailGenerationJob.status == EmailGenerationJobStatus.running,
                EmailGenerationJob.heartbeat_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        ).all()
    )
    now = _utc_now()
    for job in jobs:
        job.status = EmailGenerationJobStatus.system_error
        job.error_code = WORKER_LEASE_EXPIRED
        job.completed_at = now
        run = job.agent_run
        if run is not None and run.status is AgentRunStatus.running:
            _set_run_system_error(run, code=WORKER_LEASE_EXPIRED, now=now)
    if jobs:
        db.commit()
        logger.warning(
            "Expired email generation leases were terminally failed",
            extra={"email_generation_stale_count": len(jobs)},
        )
    else:
        db.rollback()
    return len(jobs)


def execute_claimed_job(
    db: Session,
    *,
    claim: ClaimedEmailGeneration,
    agent: EmailAgent,
    timeout_seconds: float | None = None,
) -> EmailGenerationJob:
    """Call the provider outside a transaction, then atomically finalize state."""

    try:
        result = _invoke_agent(
            agent,
            claim.curated_input,
            timeout_seconds=timeout_seconds,
        )
    except AgentCallTimeoutError:
        logger.error(
            "Email generation provider call timed out",
            extra={
                "lead_id": claim.lead_id,
                "email_generation_job_id": claim.job_id,
                "agent_run_id": claim.run_id,
            },
        )
        return _finalize_system_error(
            db,
            claim=claim,
            error_code=PROVIDER_TIMEOUT,
        )
    except Exception:
        logger.error(
            "Email generation provider call failed unexpectedly",
            extra={
                "lead_id": claim.lead_id,
                "email_generation_job_id": claim.job_id,
                "agent_run_id": claim.run_id,
            },
        )
        return _finalize_system_error(
            db,
            claim=claim,
            error_code=AGENT_EXECUTION_FAILED,
        )

    try:
        result_status = _result_status(result)
    except Exception:
        db.rollback()
        logger.error(
            "Email generation result was invalid",
            extra={
                "lead_id": claim.lead_id,
                "email_generation_job_id": claim.job_id,
                "agent_run_id": claim.run_id,
            },
        )
        return _finalize_system_error(
            db,
            claim=claim,
            error_code=INVALID_AGENT_RESULT,
        )
    try:
        return _finalize_result(
            db,
            claim=claim,
            result=result,
            status=result_status,
        )
    except (EmailGenerationJobNotRunningError, EmailGenerationJobNotFoundError):
        raise
    except Exception:
        db.rollback()
        logger.error(
            "Email generation terminal outcome could not be persisted",
            extra={
                "lead_id": claim.lead_id,
                "email_generation_job_id": claim.job_id,
                "agent_run_id": claim.run_id,
            },
        )
        return _finalize_system_error(
            db,
            claim=claim,
            error_code=TERMINAL_PERSISTENCE_FAILED,
        )


def _finalize_result(
    db: Session,
    *,
    claim: ClaimedEmailGeneration,
    result: GenerationResult,
    status: EmailGenerationJobStatus,
) -> EmailGenerationJob:
    job, run = _load_running_job_and_run(db, claim)
    now = _utc_now()
    telemetry = result.telemetry
    token_usage = telemetry.token_usage
    draft_subject: str | None = None
    draft_body: str | None = None

    if status is EmailGenerationJobStatus.generated:
        draft_subject = result.subject
        draft_body = result.body
    elif status is EmailGenerationJobStatus.insufficient_context:
        lead = run.lead or db.get(Lead, claim.lead_id)
        draft_subject, draft_body = _fallback_draft_for_low_context(lead)

    low_context_best_effort = any(
        isinstance(warning, str) and warning.startswith(LOW_CONTEXT_WARNING_CODE)
        for warning in result.warnings
    )

    run.status = AgentRunStatus(status.value)
    run.selected_product_family = result.selected_product_family
    run.selected_application = result.selected_application
    run.nurturing_email_number = result.nurturing_email_number
    run.nurturing_email_theme = result.nurturing_email_theme
    run.warnings = list(result.warnings)
    run.error_code = (
        LOW_CONTEXT_WARNING_CODE
        if status is EmailGenerationJobStatus.generated and low_context_best_effort
        else (None if status is EmailGenerationJobStatus.generated else status.value)
    )
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
    run.completed_at = now

    job.status = status
    job.error_code = (
        LOW_CONTEXT_WARNING_CODE
        if status is EmailGenerationJobStatus.generated and low_context_best_effort
        else (None if status is EmailGenerationJobStatus.generated else status.value)
    )
    job.completed_at = now

    if status in (
        EmailGenerationJobStatus.generated,
        EmailGenerationJobStatus.insufficient_context,
    ):
        email_id = str(uuid.uuid4())
        db.add(
            Email(
                id=email_id,
                agent_run_id=run.id,
                recipient_email=claim.recipient_email,
                subject=draft_subject,
                body=draft_body,
                status=EmailStatus.pending_review,
            )
        )
        db.add(
            EmailStatusEvent(
                id=str(uuid.uuid4()),
                email_id=email_id,
                previous_status=None,
                new_status=EmailStatus.pending_review,
                actor=None,
            )
        )

    model_calls = run.model_calls
    retrieval_count = run.retrieval_count
    total_tokens = run.total_tokens
    latency_ms = run.latency_ms
    db.commit()
    logger.info(
        "Email generation job completed",
        extra={
            "lead_id": claim.lead_id,
            "email_generation_job_id": claim.job_id,
            "agent_run_id": claim.run_id,
            "email_generation_status": status.value,
            "agent_model_calls": model_calls,
            "agent_retrieval_count": retrieval_count,
            "agent_total_tokens": total_tokens,
            "agent_latency_ms": latency_ms,
        },
    )
    return job


def _finalize_system_error(
    db: Session,
    *,
    claim: ClaimedEmailGeneration,
    error_code: str,
) -> EmailGenerationJob:
    job, run = _load_running_job_and_run(db, claim)
    now = _utc_now()
    _set_run_system_error(run, code=error_code, now=now)
    job.status = EmailGenerationJobStatus.system_error
    job.error_code = error_code
    job.completed_at = now
    db.commit()
    return job


def _load_running_job_and_run(
    db: Session,
    claim: ClaimedEmailGeneration,
) -> tuple[EmailGenerationJob, AgentRun]:
    job = db.scalar(
        select(EmailGenerationJob)
        .where(EmailGenerationJob.id == claim.job_id)
        .with_for_update()
    )
    if job is None:
        db.rollback()
        raise EmailGenerationJobNotFoundError(claim.job_id)
    if job.status is not EmailGenerationJobStatus.running:
        db.rollback()
        raise EmailGenerationJobNotRunningError(claim.job_id)
    run = db.get(AgentRun, claim.run_id)
    if run is None or run.email_generation_job_id != claim.job_id:
        db.rollback()
        raise EmailGenerationJobNotFoundError(claim.run_id)
    if run.status is not AgentRunStatus.running:
        db.rollback()
        raise EmailGenerationJobNotRunningError(claim.run_id)
    return job, run


def _set_run_system_error(
    run: AgentRun,
    *,
    code: str,
    now: datetime,
) -> None:
    run.status = AgentRunStatus.system_error
    run.error_code = code
    run.warnings = []
    run.original_subject = None
    run.original_body = None
    run.completed_at = now


def _result_status(result: GenerationResult) -> EmailGenerationJobStatus:
    if result.status is GenerationStatus.GENERATED:
        if not result.subject or not result.body:
            raise ValueError("A generated result requires a draft")
        return EmailGenerationJobStatus.generated
    if result.status is GenerationStatus.INSUFFICIENT_CONTEXT:
        if result.subject is not None or result.body is not None:
            raise ValueError("An unsuccessful result cannot contain a draft")
        return EmailGenerationJobStatus.insufficient_context
    if result.status is GenerationStatus.PROVIDER_ERROR:
        if result.subject is not None or result.body is not None:
            raise ValueError("An unsuccessful result cannot contain a draft")
        return EmailGenerationJobStatus.provider_error
    raise ValueError("Unsupported agent result status")


def _fallback_draft_for_low_context(lead: Lead | None) -> tuple[str, str]:
    project = (lead.project if lead is not None else None) or "your project"
    location = ", ".join(
        value for value in (
            lead.location if lead is not None else None,
            lead.state if lead is not None else None,
        )
        if value
    )
    place = location or "your area"
    subject = f"Quick Accoya check-in for {project}"
    body = (
        f"Hi,\n\n"
        f"I wanted to quickly reach out about {project} in {place}. "
        "Accoya could be a strong fit depending on your material and performance goals.\n\n"
        "If useful, I can share a short, tailored recommendation once we have a bit more project detail "
        "(application, timing, and decision criteria).\n\n"
        "Best regards,"
    )
    return subject, body


def _invoke_agent(
    agent: EmailAgent,
    curated_input: dict[str, Any],
    *,
    timeout_seconds: float | None,
) -> GenerationResult:
    if timeout_seconds is None:
        return agent.generate(curated_input)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    responses: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            responses.put((True, agent.generate(curated_input)))
        except Exception as exc:
            responses.put((False, exc))

    thread = threading.Thread(
        target=invoke,
        name="email-generation-provider-call",
        daemon=True,
    )
    thread.start()
    try:
        succeeded, value = responses.get(timeout=timeout_seconds)
    except queue.Empty:
        raise AgentCallTimeoutError from None
    if not succeeded:
        assert isinstance(value, Exception)
        raise value
    if not isinstance(value, GenerationResult):
        raise TypeError("Agent returned an unsupported result")
    return value


def _latest_job(
    db: Session,
    lead_id: str,
    *,
    statuses: tuple[EmailGenerationJobStatus, ...] | None = None,
) -> EmailGenerationJob | None:
    statement = select(EmailGenerationJob).where(
        EmailGenerationJob.lead_id == lead_id
    )
    if statuses is not None:
        statement = statement.where(EmailGenerationJob.status.in_(statuses))
    return db.scalar(
        statement.order_by(
            EmailGenerationJob.queued_at.desc(),
            EmailGenerationJob.id.desc(),
        ).limit(1)
    )


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "AgentCallTimeoutError",
    "ClaimedEmailGeneration",
    "EmailGenerationJobNotFoundError",
    "EmailGenerationJobNotRunningError",
    "EmailGenerationConflictError",
    "EmailGenerationPersistenceError",
    "IdempotencyKeyConflictError",
    "LeadNotFoundError",
    "claim_next_job",
    "current_email_for_lead",
    "current_email_is_stale",
    "ensure_low_context_fallback_email",
    "emails_for_lead",
    "enqueue_generation",
    "enqueue_initial_generations",
    "execute_claimed_job",
    "get_generation_job",
    "heartbeat_job",
    "latest_generation_job",
    "recover_stale_jobs",
]
