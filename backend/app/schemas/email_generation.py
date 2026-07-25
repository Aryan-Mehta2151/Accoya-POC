"""Public contracts for durable outreach-generation jobs."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.db.models import EmailGenerationJobStatus, EmailGenerationTrigger


class EmailGenerationRequest(BaseModel):
    """Idempotent request to generate or regenerate outreach for one lead."""

    idempotency_key: UUID


class EmailGenerationJobRead(BaseModel):
    """Provider-safe representation of one durable generation request."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    retry_of_job_id: str | None = None
    agent_run_id: str | None = None
    trigger: EmailGenerationTrigger
    status: EmailGenerationJobStatus
    requested_input_hash: str
    idempotency_key: str
    error_code: str | None = None
    attempt_count: int
    queued_at: datetime
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
