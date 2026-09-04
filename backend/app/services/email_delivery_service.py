"""Durable queue orchestration for real outreach-email delivery."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    AgentRun,
    Email,
    EmailDeliveryJob,
    EmailDeliveryJobStatus,
    EmailGenerationJob,
    EmailGenerationJobStatus,
    EmailStatus,
    EmailStatusEvent,
    Lead,
    LeadReviewStatus,
)
from app.email_content import email_content_hash
from app.email_signature import effective_signature_for_state
from app.services import email_service


logger = logging.getLogger(__name__)

ACTIVE_DELIVERY_STATUSES = (
    EmailDeliveryJobStatus.queued,
    EmailDeliveryJobStatus.running,
)
WORKER_LEASE_EXPIRED = "worker_lease_expired"
DELIVERY_EXECUTION_UNKNOWN = "delivery_execution_unknown"
DELIVERY_FINALIZATION_UNKNOWN = "delivery_finalization_unknown"
LEAD_INACTIVE = "lead_inactive"


class EmailNotFoundError(LookupError):
    """The requested email does not exist."""


class EmailDeliveryJobNotFoundError(LookupError):
    """The requested delivery job does not exist."""


class IdempotencyKeyConflictError(ValueError):
    """An idempotency key already belongs to another email."""


class EmailDeliveryConflictError(ValueError):
    """The email is not currently eligible for the requested delivery."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EmailDeliveryPersistenceError(RuntimeError):
    """Delivery state could not be durably persisted."""


class EmailDeliveryJobNotRunningError(RuntimeError):
    """A delivery result arrived after the job stopped being active."""


@dataclass(frozen=True)
class ClaimedEmailDelivery:
    """Detached email input returned after the claim transaction commits."""

    job_id: str
    email_id: str
    requested_by: str
    message_id: str
    sender_email: str
    recipient_email: str
    subject: str
    body: str


DeliveryTransport = Callable[..., None]


def delivery_configuration_error(settings: Settings) -> str | None:
    """Return a safe configuration code, or None when Graph mail is usable."""

    if not settings.microsoft_client_id.strip():
        return "microsoft_client_id_missing"
    if not settings.microsoft_tenant_id.strip():
        return "microsoft_tenant_id_missing"
    if not settings.microsoft_client_secret.strip():
        return "microsoft_client_secret_missing"
    try:
        validate_email(str(settings.microsoft_sender_email).strip(), check_deliverability=False)
    except EmailNotValidError:
        return "microsoft_sender_invalid"
    if settings.microsoft_graph_timeout_seconds <= 0:
        return "microsoft_graph_timeout_invalid"
    return None


def enqueue_delivery(
    db: Session,
    *,
    email_id: str,
    idempotency_key: str,
    expected_content_hash: str,
    acknowledge_duplicate_risk: bool,
    requested_by: str,
    sender_email: str,
) -> EmailDeliveryJob:
    """Idempotently queue an approved current email for delivery."""

    try:
        canonical_email_id = _canonical_uuid(email_id)
    except ValueError:
        raise EmailNotFoundError(email_id) from None
    key = idempotency_key.strip()
    if not key:
        raise ValueError("idempotency_key must not be blank")
    requester = requested_by.strip()
    if not requester:
        raise ValueError("requested_by must not be blank")

    lead_id = db.scalar(
        select(AgentRun.lead_id)
        .join(Email, Email.agent_run_id == AgentRun.id)
        .where(Email.id == canonical_email_id)
    )
    if lead_id is None:
        raise EmailNotFoundError(email_id)

    # Generation enqueueing uses the same lead lock. This makes send versus
    # regenerate races deterministic: exactly one workflow can become active.
    locked_lead = db.scalar(
        select(Lead).where(Lead.id == lead_id).with_for_update()
    )
    if locked_lead is None:
        raise EmailNotFoundError(email_id)
    if locked_lead.review_status is not LeadReviewStatus.active:
        raise EmailDeliveryConflictError(
            LEAD_INACTIVE,
            "EarlyBid has marked this opportunity as deleted",
        )

    existing = db.scalar(
        select(EmailDeliveryJob).where(
            EmailDeliveryJob.idempotency_key == key
        )
    )
    if existing is not None:
        if existing.email_id != canonical_email_id:
            raise IdempotencyKeyConflictError(key)
        return existing

    email = db.scalar(
        select(Email)
        .where(Email.id == canonical_email_id)
        .execution_options(populate_existing=True)
        .with_for_update(of=Email)
    )
    if email is None:
        raise EmailNotFoundError(email_id)

    active = _latest_job(
        db,
        canonical_email_id,
        statuses=ACTIVE_DELIVERY_STATUSES,
    )
    if active is not None:
        return active

    active_generation = db.scalar(
        select(EmailGenerationJob.id)
        .where(
            EmailGenerationJob.lead_id == lead_id,
            EmailGenerationJob.status.in_(
                (
                    EmailGenerationJobStatus.queued,
                    EmailGenerationJobStatus.running,
                )
            ),
        )
        .limit(1)
    )
    if active_generation is not None:
        raise EmailDeliveryConflictError(
            "generation_active",
            "Wait for the active email generation before sending",
        )

    current_email_id = db.scalar(
        select(Email.id)
        .join(AgentRun, Email.agent_run_id == AgentRun.id)
        .where(AgentRun.lead_id == lead_id)
        .order_by(Email.created_at.desc(), Email.id.desc())
        .limit(1)
    )
    if current_email_id != email.id:
        raise EmailDeliveryConflictError(
            "email_not_current",
            "Only the current outreach email can be sent",
        )
    if email.status is not EmailStatus.approved:
        raise EmailDeliveryConflictError(
            "email_not_approved",
            "The email must be approved before it can be sent",
        )

    recipient = _validated_address(
        email.recipient_email,
        code="recipient_invalid",
        message="A valid recipient email is required",
    )
    sender = _validated_address(
        sender_email,
        code="sender_invalid",
        message="The configured sender email is invalid",
    )
    if not email.subject.strip() or "\r" in email.subject or "\n" in email.subject:
        raise EmailDeliveryConflictError(
            "subject_invalid",
            "A nonblank single-line subject is required",
        )
    if not email.body.strip():
        raise EmailDeliveryConflictError(
            "body_invalid",
            "A nonblank email body is required",
        )

    effective_signature = effective_signature_for_state(
        email.signature,
        email.agent_run.lead.state,
    )
    actual_hash = email_content_hash(
        email.recipient_email,
        email.subject,
        email.body,
        effective_signature,
    )
    if expected_content_hash.strip().lower() != actual_hash:
        raise EmailDeliveryConflictError(
            "content_changed",
            "The email changed after the send confirmation was opened",
        )

    unknown_exists = db.scalar(
        select(EmailDeliveryJob.id)
        .where(
            EmailDeliveryJob.email_id == canonical_email_id,
            EmailDeliveryJob.status
            == EmailDeliveryJobStatus.delivery_unknown,
        )
        .limit(1)
    )
    if unknown_exists is not None and not acknowledge_duplicate_risk:
        raise EmailDeliveryConflictError(
            "duplicate_risk_acknowledgement_required",
            "A prior delivery may have succeeded; confirm the duplicate risk",
        )

    previous = _latest_job(db, canonical_email_id)
    job_id = str(uuid.uuid4())
    job = EmailDeliveryJob(
        id=job_id,
        email_id=canonical_email_id,
        retry_of_job_id=previous.id if previous is not None else None,
        status=EmailDeliveryJobStatus.queued,
        requested_by=requester,
        idempotency_key=key,
        content_hash=actual_hash,
        message_id=f"<{job_id}@accoya-outreach.local>",
        sender_email=sender,
        recipient_email=recipient,
        subject=email.subject,
        body_snapshot=email.rendered_body,
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
            select(EmailDeliveryJob).where(
                EmailDeliveryJob.idempotency_key == key
            )
        )
        if replay is not None:
            if replay.email_id != canonical_email_id:
                raise IdempotencyKeyConflictError(key) from exc
            return replay
        active = _latest_job(
            db,
            canonical_email_id,
            statuses=ACTIVE_DELIVERY_STATUSES,
        )
        if active is not None:
            return active
        raise EmailDeliveryPersistenceError(
            "Email delivery could not be queued"
        ) from exc
    except Exception as exc:
        db.rollback()
        logger.error(
            "Email delivery request could not be queued",
            extra={"email_id": canonical_email_id},
        )
        raise EmailDeliveryPersistenceError(
            "Email delivery could not be queued"
        ) from exc


def claim_next_job(
    db: Session,
    *,
    worker_id: str,
) -> ClaimedEmailDelivery | None:
    """Claim the oldest delivery with SKIP LOCKED and commit its lease."""

    worker = worker_id.strip()
    if not worker:
        raise ValueError("worker_id must not be blank")
    job = db.scalar(
        select(EmailDeliveryJob)
        .where(EmailDeliveryJob.status == EmailDeliveryJobStatus.queued)
        .order_by(EmailDeliveryJob.queued_at, EmailDeliveryJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        db.rollback()
        return None

    review_status = db.scalar(
        select(Lead.review_status)
        .join(AgentRun, AgentRun.lead_id == Lead.id)
        .join(Email, Email.agent_run_id == AgentRun.id)
        .where(Email.id == job.email_id)
    )
    if review_status is not LeadReviewStatus.active:
        job.status = EmailDeliveryJobStatus.failed
        job.error_code = LEAD_INACTIVE
        job.completed_at = _utc_now()
        db.commit()
        return None

    now = _utc_now()
    job.status = EmailDeliveryJobStatus.running
    job.attempt_count += 1
    job.claimed_by = worker
    job.claimed_at = now
    job.heartbeat_at = now
    job.send_started_at = now
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error(
            "Email delivery claim could not be persisted",
            extra={"email_delivery_job_id": job.id, "worker_id": worker},
        )
        raise
    return ClaimedEmailDelivery(
        job_id=str(job.id),
        email_id=str(job.email_id),
        requested_by=job.requested_by,
        message_id=job.message_id,
        sender_email=job.sender_email,
        recipient_email=job.recipient_email,
        subject=job.subject,
        body=job.body_snapshot,
    )


def heartbeat_job(db: Session, *, job_id: str, worker_id: str) -> bool:
    """Renew a running delivery lease for only its claiming worker."""

    job = db.scalar(
        select(EmailDeliveryJob)
        .where(EmailDeliveryJob.id == job_id)
        .with_for_update()
    )
    if (
        job is None
        or job.status is not EmailDeliveryJobStatus.running
        or job.claimed_by != worker_id
    ):
        db.rollback()
        return False
    job.heartbeat_at = _utc_now()
    db.commit()
    return True


def recover_stale_jobs(db: Session, *, stale_after_seconds: float) -> int:
    """Mark expired running delivery attempts unknown; never replay them."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    cutoff = _utc_now() - timedelta(seconds=stale_after_seconds)
    jobs = list(
        db.scalars(
            select(EmailDeliveryJob)
            .where(
                EmailDeliveryJob.status == EmailDeliveryJobStatus.running,
                EmailDeliveryJob.heartbeat_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        ).all()
    )
    now = _utc_now()
    for job in jobs:
        job.status = EmailDeliveryJobStatus.delivery_unknown
        job.error_code = WORKER_LEASE_EXPIRED
        job.completed_at = now
    if jobs:
        db.commit()
        logger.warning(
            "Expired email delivery leases were marked unknown",
            extra={"email_delivery_stale_count": len(jobs)},
        )
    else:
        db.rollback()
    return len(jobs)


def execute_claimed_job(
    db: Session,
    *,
    claim: ClaimedEmailDelivery,
    settings: Settings,
    transport: DeliveryTransport | None = None,
) -> EmailDeliveryJob:
    """Call the mail provider outside a transaction, then finalize outcome."""

    deliver = transport or email_service.send_outreach_email
    try:
        deliver(
            sender_email=claim.sender_email,
            recipient_email=claim.recipient_email,
            subject=claim.subject,
            body=claim.body,
            message_id=claim.message_id,
            settings=settings,
        )
    except email_service.EmailDeliveryFailure as exc:
        return _finalize_terminal(
            db,
            claim=claim,
            status=EmailDeliveryJobStatus.failed,
            error_code=exc.code,
        )
    except email_service.EmailDeliveryUnknown as exc:
        return _finalize_terminal(
            db,
            claim=claim,
            status=EmailDeliveryJobStatus.delivery_unknown,
            error_code=exc.code,
        )
    except Exception:
        logger.error(
            "Email delivery ended with an unclassified outcome",
            extra={"email_delivery_job_id": claim.job_id},
        )
        return _finalize_terminal(
            db,
            claim=claim,
            status=EmailDeliveryJobStatus.delivery_unknown,
            error_code=DELIVERY_EXECUTION_UNKNOWN,
        )

    try:
        return _finalize_success(db, claim=claim)
    except (EmailDeliveryJobNotFoundError, EmailDeliveryJobNotRunningError):
        raise
    except Exception:
        db.rollback()
        logger.error(
            "Accepted email delivery could not be finalized",
            extra={"email_delivery_job_id": claim.job_id},
        )
        return _record_unknown_after_persistence_error(db, claim=claim)


def get_delivery_job(db: Session, job_id: str) -> EmailDeliveryJob:
    """Load one delivery job by canonical UUID."""

    try:
        canonical_job_id = _canonical_uuid(job_id)
    except ValueError:
        raise EmailDeliveryJobNotFoundError(job_id) from None
    job = db.get(EmailDeliveryJob, canonical_job_id)
    if job is None:
        raise EmailDeliveryJobNotFoundError(job_id)
    return job


def has_blocking_delivery(db: Session, email_id: str) -> bool:
    """Return whether generation must be blocked for an email."""

    return (
        db.scalar(
            select(EmailDeliveryJob.id)
            .where(
                EmailDeliveryJob.email_id == email_id,
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
        is not None
    )


def active_delivery_for_email(
    db: Session,
    email_id: str,
) -> EmailDeliveryJob | None:
    """Return a queued/running attempt, if one exists."""

    return _latest_job(db, email_id, statuses=ACTIVE_DELIVERY_STATUSES)


def _finalize_success(
    db: Session,
    *,
    claim: ClaimedEmailDelivery,
) -> EmailDeliveryJob:
    job = _load_running_job(db, claim.job_id)
    email = db.scalar(
        select(Email).where(Email.id == claim.email_id).with_for_update(of=Email)
    )
    if email is None:
        db.rollback()
        raise EmailNotFoundError(claim.email_id)
    now = _utc_now()
    previous_status = email.status
    job.status = EmailDeliveryJobStatus.succeeded
    job.error_code = None
    job.accepted_at = now
    job.completed_at = now
    if previous_status is not EmailStatus.sent:
        email.status = EmailStatus.sent
        db.add(
            EmailStatusEvent(
                id=str(uuid.uuid4()),
                email_id=email.id,
                previous_status=previous_status,
                new_status=EmailStatus.sent,
                actor=job.requested_by,
            )
        )
    db.commit()
    logger.info(
        "Email delivery accepted by Microsoft Graph",
        extra={"email_delivery_job_id": claim.job_id},
    )
    return job


def _finalize_terminal(
    db: Session,
    *,
    claim: ClaimedEmailDelivery,
    status: EmailDeliveryJobStatus,
    error_code: str,
) -> EmailDeliveryJob:
    if status not in (
        EmailDeliveryJobStatus.failed,
        EmailDeliveryJobStatus.delivery_unknown,
    ):
        raise ValueError("Unsupported terminal delivery status")
    job = _load_running_job(db, claim.job_id)
    job.status = status
    job.error_code = error_code
    job.completed_at = _utc_now()
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise EmailDeliveryPersistenceError(
            "Email delivery outcome could not be persisted"
        ) from exc
    return job


def _record_unknown_after_persistence_error(
    db: Session,
    *,
    claim: ClaimedEmailDelivery,
) -> EmailDeliveryJob:
    try:
        job = db.scalar(
            select(EmailDeliveryJob)
            .where(EmailDeliveryJob.id == claim.job_id)
            .with_for_update()
        )
        if job is None:
            raise EmailDeliveryJobNotFoundError(claim.job_id)
        if job.status is EmailDeliveryJobStatus.succeeded:
            db.rollback()
            return job
        if job.status is not EmailDeliveryJobStatus.running:
            db.rollback()
            raise EmailDeliveryJobNotRunningError(claim.job_id)
        job.status = EmailDeliveryJobStatus.delivery_unknown
        job.error_code = DELIVERY_FINALIZATION_UNKNOWN
        job.completed_at = _utc_now()
        db.commit()
        return job
    except (EmailDeliveryJobNotFoundError, EmailDeliveryJobNotRunningError):
        raise
    except Exception as exc:
        db.rollback()
        raise EmailDeliveryPersistenceError(
            "Accepted delivery could not be marked unknown"
        ) from exc


def _load_running_job(db: Session, job_id: str) -> EmailDeliveryJob:
    job = db.scalar(
        select(EmailDeliveryJob)
        .where(EmailDeliveryJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        db.rollback()
        raise EmailDeliveryJobNotFoundError(job_id)
    if job.status is not EmailDeliveryJobStatus.running:
        db.rollback()
        raise EmailDeliveryJobNotRunningError(job_id)
    return job


def _latest_job(
    db: Session,
    email_id: str,
    *,
    statuses: tuple[EmailDeliveryJobStatus, ...] | None = None,
) -> EmailDeliveryJob | None:
    statement = select(EmailDeliveryJob).where(
        EmailDeliveryJob.email_id == email_id
    )
    if statuses is not None:
        statement = statement.where(EmailDeliveryJob.status.in_(statuses))
    return db.scalar(
        statement.order_by(
            EmailDeliveryJob.queued_at.desc(),
            EmailDeliveryJob.id.desc(),
        ).limit(1)
    )


def _validated_address(
    value: str | None,
    *,
    code: str,
    message: str,
) -> str:
    try:
        return validate_email(
            (value or "").strip(),
            check_deliverability=False,
        ).normalized
    except EmailNotValidError:
        raise EmailDeliveryConflictError(code, message) from None


def _canonical_uuid(value: str) -> str:
    return str(uuid.UUID(str(value)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ACTIVE_DELIVERY_STATUSES",
    "ClaimedEmailDelivery",
    "DELIVERY_EXECUTION_UNKNOWN",
    "DELIVERY_FINALIZATION_UNKNOWN",
    "EmailDeliveryConflictError",
    "EmailDeliveryJobNotFoundError",
    "EmailDeliveryJobNotRunningError",
    "EmailDeliveryPersistenceError",
    "EmailNotFoundError",
    "IdempotencyKeyConflictError",
    "WORKER_LEASE_EXPIRED",
    "active_delivery_for_email",
    "claim_next_job",
    "delivery_configuration_error",
    "enqueue_delivery",
    "execute_claimed_job",
    "get_delivery_job",
    "has_blocking_delivery",
    "heartbeat_job",
    "recover_stale_jobs",
]
