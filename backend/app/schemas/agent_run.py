"""Safe HTTP contracts for persisted agent executions."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import AgentRunStatus


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentRunCreate(BaseModel):
    """Request one queued agent execution for the lead's current state."""

    lead_id: Identifier


class AgentRunRead(BaseModel):
    """Minimal, provider-safe record of one immutable agent attempt."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    retry_of_run_id: str | None = None
    email_generation_job_id: str | None = None
    input_hash: str
    status: AgentRunStatus
    selected_product_family: str | None = None
    selected_application: str | None = None
    nurturing_email_number: int | None = None
    nurturing_email_theme: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    original_subject: str | None = None
    original_body: str | None = None
    prompt_version: str
    catalog_version: str
    model_name: str
    model_calls: int = 0
    retrieval_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    started_at: datetime
    completed_at: datetime | None = None


class AgentRunRetryRead(AgentRunRead):
    """Response for a retry; the wire shape intentionally matches a run."""


class AgentRunPage(BaseModel):
    """One descending page of runs and an opaque continuation cursor."""

    items: list[AgentRunRead]
    next_cursor: str | None = None


class AgentRunSystemErrorDetail(BaseModel):
    """Safe error returned after an unexpected failure has been persisted."""

    code: str
    message: str
    run_id: str
