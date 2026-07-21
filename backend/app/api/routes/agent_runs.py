"""Production APIs for persisted Accoya agent executions."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import AgentRunStatus
from app.schemas.agent_run import (
    AgentRunCreate,
    AgentRunPage,
    AgentRunRead,
    AgentRunRetryRead,
    AgentRunSystemErrorDetail,
)
from app.services import agent_run_service, email_generator


router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])


@router.post(
    "",
    response_model=AgentRunRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Lead not found."},
        500: {
            "model": AgentRunSystemErrorDetail,
            "description": "The unexpected failure was persisted on the run.",
        },
    },
)
def create_agent_run(
    payload: AgentRunCreate,
    db: Session = Depends(get_db),
    agent: email_generator.EmailAgent = Depends(
        email_generator.get_accoya_email_agent
    ),
):
    """Synchronously execute a new independent run for the current lead."""

    try:
        return agent_run_service.execute_agent_run(
            db,
            lead_id=payload.lead_id,
            agent=agent,
        )
    except agent_run_service.LeadNotFoundError:
        raise HTTPException(status_code=404, detail="Lead not found") from None
    except agent_run_service.AgentRunSystemError as exc:
        return _system_error_response(exc)
    except agent_run_service.AgentRunPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Agent run could not be saved",
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
    response_model=AgentRunRetryRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Agent run or its lead was not found."},
        409: {"description": "Only terminal runs can be retried."},
        500: {
            "model": AgentRunSystemErrorDetail,
            "description": "The unexpected failure was persisted on the retry.",
        },
    },
)
def retry_agent_run(
    run_id: str,
    db: Session = Depends(get_db),
    agent: email_generator.EmailAgent = Depends(
        email_generator.get_accoya_email_agent
    ),
):
    """Create a linked attempt using the lead's current stored projection."""

    try:
        return agent_run_service.retry_agent_run(db, run_id=run_id, agent=agent)
    except (
        agent_run_service.AgentRunNotFoundError,
        agent_run_service.LeadNotFoundError,
    ):
        raise HTTPException(status_code=404, detail="Agent run not found") from None
    except agent_run_service.AgentRunNotRetryableError:
        raise HTTPException(
            status_code=409,
            detail="Only terminal agent runs can be retried",
        ) from None
    except agent_run_service.AgentRunSystemError as exc:
        return _system_error_response(exc)
    except agent_run_service.AgentRunPersistenceError:
        raise HTTPException(
            status_code=500,
            detail="Agent run could not be saved",
        ) from None


@router.get("/{run_id}", response_model=AgentRunRead)
def get_agent_run(run_id: str, db: Session = Depends(get_db)):
    """Return one safe persisted run record."""

    try:
        return agent_run_service.get_agent_run(db, run_id)
    except agent_run_service.AgentRunNotFoundError:
        raise HTTPException(status_code=404, detail="Agent run not found") from None


def _system_error_response(
    exc: agent_run_service.AgentRunSystemError,
) -> JSONResponse:
    detail = AgentRunSystemErrorDetail(
        code=exc.code,
        message="The agent run failed unexpectedly.",
        run_id=exc.run_id,
    )
    return JSONResponse(
        status_code=500,
        content=detail.model_dump(mode="json"),
    )
