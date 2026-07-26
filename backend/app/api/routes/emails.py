"""Email-generation compatibility, review, editing, and delivery routes."""

from uuid import UUID, uuid4

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.dependencies.auth import get_current_user
from app.config import get_settings
from app.db.database import get_db
from app.db.models import Email, EmailStatus, EmailStatusEvent, User
from app.schemas.email import EmailEdit, EmailRead, EmailStatusUpdate
from app.schemas.email_delivery import EmailDeliveryJobRead, EmailDeliveryRequest
from app.schemas.email_generation import EmailGenerationJobRead
from app.services import email_delivery_service, email_generation_service

router = APIRouter(prefix="/emails", tags=["emails"])

_USER_TRANSITIONS: dict[EmailStatus, set[EmailStatus]] = {
    EmailStatus.draft: {EmailStatus.pending_review},
    EmailStatus.pending_review: {EmailStatus.approved, EmailStatus.rejected},
    EmailStatus.approved: {EmailStatus.rejected},
    EmailStatus.sent: set(),
    EmailStatus.rejected: set(),
}


def _canonical_email_id(email_id: str) -> str:
    """Return a canonical UUID string or the endpoint's stable 404 response."""

    try:
        return str(UUID(email_id))
    except (AttributeError, ValueError):
        raise HTTPException(status_code=404, detail="Email not found") from None


def _email_with_delivery(db: Session, email_id: str) -> Email | None:
    return db.scalar(
        select(Email)
        .where(Email.id == email_id)
        .options(selectinload(Email.delivery_jobs))
    )


def _raise_conflict(code: str, message: str) -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": code, "message": message},
    )


def _require_current_email(db: Session, email: Email) -> None:
    current = email_generation_service.current_email_for_lead(db, email.lead_id)
    if current is None or current.id != email.id:
        _raise_conflict(
            "email_not_current",
            "Only the current outreach email can be changed",
        )


def _require_ready_for_approval(email: Email) -> None:
    try:
        validate_email(
            (email.recipient_email or "").strip(),
            check_deliverability=False,
        )
    except EmailNotValidError:
        _raise_conflict(
            "recipient_invalid",
            "A valid recipient email is required before approval",
        )
    if not email.subject.strip() or "\r" in email.subject or "\n" in email.subject:
        _raise_conflict(
            "subject_invalid",
            "A nonblank single-line subject is required before approval",
        )
    if not email.body.strip():
        _raise_conflict(
            "body_invalid",
            "A nonblank email body is required before approval",
        )


@router.post(
    "/generate/{lead_id}",
    response_model=EmailGenerationJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    deprecated=True,
)
def generate_for_lead(
    lead_id: str,
    db: Session = Depends(get_db),
):
    """Deprecated compatibility adapter that queues provider work."""

    try:
        return email_generation_service.enqueue_generation(
            db,
            lead_id=lead_id,
            idempotency_key=str(uuid4()),
        )
    except email_generation_service.LeadNotFoundError:
        raise HTTPException(status_code=404, detail="Lead not found") from None
    except email_generation_service.EmailGenerationConflictError as exc:
        _raise_conflict(exc.code, exc.message)
    except email_generation_service.EmailGenerationPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Email generation could not be queued",
        ) from None


@router.get("", response_model=list[EmailRead])
def list_emails(db: Session = Depends(get_db)):
    return db.scalars(
        select(Email)
        .options(selectinload(Email.delivery_jobs))
        .order_by(Email.created_at.desc(), Email.id.desc())
    ).all()


@router.get("/{email_id}", response_model=EmailRead)
def get_email(email_id: str, db: Session = Depends(get_db)):
    """Resolve an email for canonical and legacy deep links."""

    email = _email_with_delivery(db, _canonical_email_id(email_id))
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@router.patch("/{email_id}", response_model=EmailRead)
def edit_email(
    email_id: str,
    payload: EmailEdit,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = db.scalar(
        select(Email)
        .where(Email.id == _canonical_email_id(email_id))
        .with_for_update(of=Email)
    )
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    _require_current_email(db, email)
    if email.status in (EmailStatus.sent, EmailStatus.rejected):
        _raise_conflict(
            "email_read_only",
            "Sent and rejected emails cannot be edited",
        )
    if email_delivery_service.active_delivery_for_email(db, email.id) is not None:
        _raise_conflict(
            "delivery_active",
            "The email cannot be edited while delivery is active",
        )

    changed = False
    if "recipient_email" in payload.model_fields_set:
        recipient = (
            str(payload.recipient_email).strip()
            if payload.recipient_email is not None
            else None
        )
        if recipient != email.recipient_email:
            email.recipient_email = recipient
            changed = True
    if payload.subject is not None and payload.subject != email.subject:
        email.subject = payload.subject
        changed = True
    if payload.body is not None and payload.body != email.body:
        email.body = payload.body
        changed = True

    if changed and email.status is EmailStatus.approved:
        email.status = EmailStatus.pending_review
        db.add(
            EmailStatusEvent(
                email_id=email.id,
                previous_status=EmailStatus.approved,
                new_status=EmailStatus.pending_review,
                actor=str(current_user.id),
            )
        )

    db.commit()
    return _email_with_delivery(db, email.id)


@router.post("/{email_id}/status", response_model=EmailRead)
def update_status(
    email_id: str,
    payload: EmailStatusUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = db.scalar(
        select(Email)
        .where(Email.id == _canonical_email_id(email_id))
        .with_for_update(of=Email)
    )
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    _require_current_email(db, email)
    if email_delivery_service.active_delivery_for_email(db, email.id) is not None:
        _raise_conflict(
            "delivery_active",
            "Review status cannot change while delivery is active",
        )

    previous_status = email.status
    if payload.status is EmailStatus.sent:
        _raise_conflict(
            "sent_requires_delivery",
            "Only confirmed SMTP delivery can mark an email as sent",
        )
    if payload.status is previous_status:
        db.rollback()
        return _email_with_delivery(db, email.id)
    if payload.status not in _USER_TRANSITIONS[previous_status]:
        _raise_conflict(
            "invalid_status_transition",
            f"Email cannot move from {previous_status.value} to {payload.status.value}",
        )
    if payload.status is EmailStatus.approved:
        _require_ready_for_approval(email)

    email.status = payload.status
    db.add(
        EmailStatusEvent(
            email_id=email.id,
            previous_status=previous_status,
            new_status=payload.status,
            actor=str(current_user.id),
        )
    )
    db.commit()
    return _email_with_delivery(db, email.id)


@router.post(
    "/{email_id}/send",
    response_model=EmailDeliveryJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def send_email(
    email_id: str,
    payload: EmailDeliveryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Idempotently queue the confirmed, approved payload for real SMTP."""

    settings = get_settings()
    if not settings.jwt_secret_key.strip():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "jwt_not_configured",
                "message": "Authenticated email delivery is not configured",
            },
        )
    configuration_error = email_delivery_service.delivery_configuration_error(
        settings
    )
    if configuration_error is not None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": configuration_error,
                "message": "SMTP delivery is not configured",
            },
        )

    try:
        return email_delivery_service.enqueue_delivery(
            db,
            email_id=email_id,
            idempotency_key=str(payload.idempotency_key),
            expected_content_hash=payload.expected_content_hash,
            acknowledge_duplicate_risk=payload.acknowledge_duplicate_risk,
            requested_by=str(current_user.id),
            sender_email=settings.smtp_email,
        )
    except email_delivery_service.EmailNotFoundError:
        raise HTTPException(status_code=404, detail="Email not found") from None
    except email_delivery_service.IdempotencyKeyConflictError:
        _raise_conflict(
            "idempotency_key_conflict",
            "Idempotency key is already in use",
        )
    except email_delivery_service.EmailDeliveryConflictError as exc:
        _raise_conflict(exc.code, exc.message)
    except email_delivery_service.EmailDeliveryPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Email delivery could not be queued",
        ) from None
