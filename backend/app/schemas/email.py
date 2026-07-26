"""Pydantic schemas for editable outreach emails."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

from app.db.models import EmailStatus
from app.schemas.email_delivery import EmailDeliveryJobRead


EmailSubject = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[^\r\n]+$",
    ),
]


class EmailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    recipient_email: str | None = None
    # Drafts may contain provider or legacy values that still need human
    # correction; edit and approval validation enforce the sendable shape.
    subject: str
    body: str
    status: EmailStatus
    latest_delivery: EmailDeliveryJobRead | None = None
    has_unknown_delivery: bool = False
    delivery_content_hash: str
    created_at: datetime
    updated_at: datetime


class EmailStatusUpdate(BaseModel):
    status: EmailStatus
    actor: str | None = None


class EmailEdit(BaseModel):
    recipient_email: EmailStr | None = None
    subject: EmailSubject | None = None
    body: str | None = None

    @field_validator('recipient_email', mode='before')
    @classmethod
    def blank_recipient_clears_value(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class EmailGenerationErrorDetail(BaseModel):
    """Safe failure details returned by the email generation endpoint."""

    code: str
    message: str
    warnings: list[str] = Field(default_factory=list)
