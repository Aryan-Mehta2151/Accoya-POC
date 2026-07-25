"""Production APIs for persisted Accoya agent executions."""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import AgentRunStatus, EmailGenerationTrigger
from app.schemas.agent_run import (
    AgentRunCreate,
    AgentRunPage,
    AgentRunRead,
)
from app.schemas.email_generation import EmailGenerationJobRead
from app.services import agent_run_service, email_generation_service


router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])


@router.post(
    "",
    response_model=EmailGenerationJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    deprecated=True,
)
def create_agent_run(
    payload: AgentRunCreate,
    db: Session = Depends(get_db),
):
    """Deprecated adapter that queues a run for the current lead."""

    try:
        return email_generation_service.enqueue_generation(
            db,
            lead_id=payload.lead_id,
            idempotency_key=str(uuid4()),
        )
    except email_generation_service.LeadNotFoundError:
        raise HTTPException(status_code=404, detail="Lead not found") from None
    except email_generation_service.EmailGenerationPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Agent run could not be queued",
        ) from None


@router.get("", response_model=AgentRunPage)
def list_agent_runs(
    lead_id: str | None = None,
    run_status: AgentRunStatus | None = Query(default=None, alias="status"),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """List newest runs with optional lead/status filters and cursor paging."""

    try:
        page = agent_run_service.list_agent_runs(
            db,
            lead_id=lead_id,
            status=run_status,
            cursor=cursor,
            limit=limit,
        )
    except agent_run_service.InvalidRunCursorError:
        raise HTTPException(status_code=400, detail="Invalid cursor") from None
    return AgentRunPage(items=page.items, next_cursor=page.next_cursor)


@router.post(
    "/{run_id}/retry",
    response_model=EmailGenerationJobRead,
    status_code=status.HTTP_202_ACCEPTED,
    deprecated=True,
)
def retry_agent_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    """Queue a linked retry using the lead's current stored projection."""

    try:
        previous = agent_run_service.get_agent_run(db, run_id)
    except agent_run_service.AgentRunNotFoundError:
        raise HTTPException(status_code=404, detail="Agent run not found") from None
    if previous.status is AgentRunStatus.running:
        raise HTTPException(
            status_code=409,
            detail="Only terminal agent runs can be retried",
        )
    try:
        return email_generation_service.enqueue_generation(
            db,
            lead_id=previous.lead_id,
            idempotency_key=str(uuid4()),
            trigger=EmailGenerationTrigger.retry,
            retry_of_job_id=previous.email_generation_job_id,
        )
    except email_generation_service.LeadNotFoundError:
        raise HTTPException(status_code=404, detail="Lead not found") from None
    except email_generation_service.EmailGenerationPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Agent run could not be queued",
        ) from None


@router.get("/{run_id}", response_model=AgentRunRead)
def get_agent_run(run_id: str, db: Session = Depends(get_db)):
    """Return one safe persisted run record."""

    try:
        return agent_run_service.get_agent_run(db, run_id)
    except agent_run_service.AgentRunNotFoundError:
        raise HTTPException(status_code=404, detail="Agent run not found") from None
