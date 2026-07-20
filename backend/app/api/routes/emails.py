"""Email generation, review/approval workflow, and sending."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Email, EmailStatus, Lead
from app.schemas.email import EmailEdit, EmailRead, EmailStatusUpdate
from app.services import email_generator

router = APIRouter(prefix="/emails", tags=["emails"])


@router.post("/generate/{lead_id}", response_model=EmailRead)
def generate_for_lead(lead_id: str, db: Session = Depends(get_db)):
    """Generate an outreach email for a specific lead card."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    subject, body = email_generator.generate_email(lead)
    email = Email(
        lead_id=lead.id,
        subject=subject,
        body=body,
        status=EmailStatus.pending_review,
    )
    db.add(email)
    db.commit()
    db.refresh(email)
    return email


@router.get("", response_model=list[EmailRead])
def list_emails(db: Session = Depends(get_db)):
    return db.scalars(select(Email).order_by(Email.created_at.desc())).all()


@router.patch("/{email_id}", response_model=EmailRead)
def edit_email(email_id: str, payload: EmailEdit, db: Session = Depends(get_db)):
    email = db.get(Email, email_id)
    if not email:
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
    email = db.get(Email, email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    email.status = payload.status

    # TODO: when approved -> send to the client email on the lead, then mark sent
    # and index the sent email into the Bedrock KB so the chatbot can reference it.
    db.commit()
    db.refresh(email)
    return email
