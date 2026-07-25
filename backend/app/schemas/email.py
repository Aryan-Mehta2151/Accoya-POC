"""Pydantic schemas for emails."""
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import EmailStatus


EmailSubject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class EmailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    recipient_email: str | None = None
    subject: EmailSubject
    body: str
    status: EmailStatus
    created_at: datetime
    updated_at: datetime

class EmailStatusUpdate(BaseModel):
    status: EmailStatus
    actor: str | None = None


class EmailEdit(BaseModel):
    subject: EmailSubject | None = None
    body: str | None = None


class EmailGenerationErrorDetail(BaseModel):
    """Safe failure details returned by the email generation endpoint."""

    code: str
    message: str
    warnings: list[str] = Field(default_factory=list)
