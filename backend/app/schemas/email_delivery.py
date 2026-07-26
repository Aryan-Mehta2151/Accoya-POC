"""Public contracts for durable outreach-email delivery."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import EmailDeliveryJobStatus


Sha256Hex = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[0-9a-fA-F]{64}$",
    ),
]


class EmailDeliveryRequest(BaseModel):
    """Idempotent, content-bound request to send one approved email."""

    idempotency_key: UUID
    expected_content_hash: Sha256Hex
    acknowledge_duplicate_risk: bool = False


class EmailDeliveryJobRead(BaseModel):
    """Safe representation of one durable SMTP delivery attempt."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email_id: str
    retry_of_job_id: str | None = None
    status: EmailDeliveryJobStatus
    requested_by: str
    idempotency_key: str
    content_hash: str
    message_id: str
    sender_email: str
    recipient_email: str
    subject: str
    body_snapshot: str
    error_code: str | None = None
    attempt_count: int
    queued_at: datetime
    claimed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    send_started_at: datetime | None = None
    accepted_at: datetime | None = None
    completed_at: datetime | None = None


class EmailDeliveryErrorDetail(BaseModel):
    """Structured safe failure returned for delivery workflow conflicts."""

    code: str
    message: str
    context: dict[str, str] = Field(default_factory=dict)
