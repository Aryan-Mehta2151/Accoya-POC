"""Email generation compatibility facade and review workflow."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Email, EmailStatusEvent
from app.schemas.email import (
    EmailEdit,
    EmailRead,
    EmailStatusUpdate,
)
from app.schemas.email_generation import EmailGenerationJobRead
from app.services import email_generation_service

router = APIRouter(prefix="/emails", tags=["emails"])


def _canonical_email_id(email_id: str) -> str:
    """Return a canonical UUID string or the endpoint's stable 404 response."""

    try:
        return str(UUID(email_id))
    except (AttributeError, ValueError):
        raise HTTPException(status_code=404, detail="Email not found") from None


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
    except email_generation_service.EmailGenerationPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Email generation could not be queued",
        ) from None


@router.get("", response_model=list[EmailRead])
def list_emails(db: Session = Depends(get_db)):
    return db.scalars(
        select(Email).order_by(Email.created_at.desc(), Email.id.desc())
    ).all()


@router.get("/{email_id}", response_model=EmailRead)
def get_email(email_id: str, db: Session = Depends(get_db)):
    """Resolve an email for canonical and legacy deep links."""

    email = db.get(Email, _canonical_email_id(email_id))
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@router.patch("/{email_id}", response_model=EmailRead)
def edit_email(email_id: str, payload: EmailEdit, db: Session = Depends(get_db)):
    email = db.get(Email, _canonical_email_id(email_id))
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")
    if payload.subject is not None:
        email.subject = payload.subject
    if payload.body is not None:
        email.body = payload.body
    db.commit()
    db.refresh(email)
    return email


@router.post("/{email_id}/status", response_model=EmailRead)
def update_status(email_id: str, payload: EmailStatusUpdate, db: Session = Depends(get_db)):
    email = db.scalar(
        select(Email)
        .where(Email.id == _canonical_email_id(email_id))
        .with_for_update(of=Email)
    )
    if email is None:
        raise HTTPException(status_code=404, detail="Email not found")

    previous_status = email.status
    if previous_status != payload.status:
        email.status = payload.status
        db.add(
            EmailStatusEvent(
                email_id=email.id,
                previous_status=previous_status,
                new_status=payload.status,
                actor=payload.actor,
            )
        )

    # TODO: when approved -> send to the client email on the lead, then mark sent
    # and index the sent email into the Bedrock KB so the chatbot can reference it.
    db.commit()
    db.refresh(email)
    return email
