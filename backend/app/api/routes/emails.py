"""Email generation compatibility facade and review workflow."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import AgentRunStatus, Email, EmailStatusEvent
from app.schemas.email import (
    EmailEdit,
    EmailGenerationErrorDetail,
    EmailRead,
    EmailStatusUpdate,
)
from app.services import agent_run_service, email_generator

router = APIRouter(prefix="/emails", tags=["emails"])


def _canonical_email_id(email_id: str) -> str:
    """Return a canonical UUID string or the endpoint's stable 404 response."""

    try:
        return str(UUID(email_id))
    except (AttributeError, ValueError):
        raise HTTPException(status_code=404, detail="Email not found") from None


@router.post(
    "/generate/{lead_id}",
    response_model=EmailRead,
    responses={
        422: {
            "model": EmailGenerationErrorDetail,
            "description": "The agent found insufficient lead context.",
        },
        502: {
            "model": EmailGenerationErrorDetail,
            "description": "The configured generation provider failed.",
        },
    },
)
def generate_for_lead(
    lead_id: str,
    db: Session = Depends(get_db),
    agent: email_generator.EmailAgent = Depends(
        email_generator.get_accoya_email_agent
    ),
):
    """Generate through a persisted run while preserving the legacy DTO."""
    try:
        run = agent_run_service.execute_agent_run(
            db,
            lead_id=lead_id,
            agent=agent,
        )
    except agent_run_service.LeadNotFoundError:
        raise HTTPException(status_code=404, detail="Lead not found") from None
    except agent_run_service.AgentRunSystemError:
        raise HTTPException(
            status_code=500,
            detail="Email generation failed",
        ) from None
    except agent_run_service.AgentRunPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Generated email could not be saved",
        ) from None

    if run.status is AgentRunStatus.insufficient_context:
        return JSONResponse(
            status_code=422,
            content=EmailGenerationErrorDetail(
                code=run.status.value,
                message="The lead does not contain enough context to generate an email.",
                warnings=run.warnings,
            ).model_dump(),
        )
    if run.status is AgentRunStatus.provider_error:
        return JSONResponse(
            status_code=502,
            content=EmailGenerationErrorDetail(
                code=run.status.value,
                message="The email generation provider could not produce a draft.",
                warnings=run.warnings,
            ).model_dump(),
        )
    if run.status is not AgentRunStatus.generated:
        raise HTTPException(status_code=500, detail="Email generation failed")

    email = db.scalar(select(Email).where(Email.agent_run_id == run.id))
    if email is None:
        raise HTTPException(
            status_code=500,
            detail="Generated email could not be saved",
        )
    return email


@router.get("", response_model=list[EmailRead])
def list_emails(db: Session = Depends(get_db)):
    return db.scalars(select(Email).order_by(Email.created_at.desc())).all()


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
