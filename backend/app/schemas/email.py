"""Pydantic schemas for emails."""
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.db.models import EmailStatus
from app.email_text import normalize_email_body


EmailSubject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class EmailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    subject: EmailSubject
    body: str
    status: EmailStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        return normalize_email_body(value)


class EmailStatusUpdate(BaseModel):
    status: EmailStatus
    actor: str | None = None


class EmailEdit(BaseModel):
    subject: EmailSubject | None = None
    body: str | None = None

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str | None) -> str | None:
        return normalize_email_body(value) if value is not None else None


class EmailGenerationErrorDetail(BaseModel):
    """Safe failure details returned by the email generation endpoint."""

    code: str
    message: str
    warnings: list[str] = Field(default_factory=list)
