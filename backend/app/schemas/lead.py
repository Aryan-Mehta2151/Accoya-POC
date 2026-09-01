"""Pydantic schemas for leads (EarlyBid opportunities)."""
from datetime import date, datetime
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.db.models import EmailStatus, LeadReviewStatus
from app.schemas.email import EmailRead
from app.schemas.email_generation import EmailGenerationJobRead


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    external_id: str
    section: str | None = None
    project: str | None = None
    location: str | None = None
    state: str | None = None
    signal: str | None = None
    intelligence: str | None = None
    score: float | None = None
    timing: str | None = None
    awarded_to: str | None = None
    priority_reasons: str | None = None
    summary: str | None = None
    contacts: str | None = None
    contact_email: str | None = None
    meeting_date: str | None = None
    tags: str | None = None
    url: str | None = None
    reported: Any | None = None
    due_date: date | None = None
    award_date: date | None = None
    start_date: date | None = None
    response_deadline_evidence: Any | None = None
    keywords_matched: list[str] = Field(default_factory=list)
    review_status: LeadReviewStatus | None = None
    deleted_by: str | None = None
    deleted_reasons: list[str] = Field(default_factory=list)
    source_feed: str | None = None
    created_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def serialize_native_uuid(cls, value: Any) -> str:
        """Keep the existing string ID wire contract with native UUID storage."""
        return str(value)

    @field_validator("tags", mode="before")
    @classmethod
    def serialize_json_tags(cls, value: Any) -> str | None:
        """Keep the existing string-or-null frontend contract for JSONB tags."""
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return json.dumps(value, separators=(",", ":"), sort_keys=True)


class LeadContactEdit(BaseModel):
    contacts: str | None = None
    contact_email: EmailStr | None = None

    @field_validator("contacts", "contact_email", mode="before")
    @classmethod
    def blank_values_clear_fields(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return value


class SyncResult(BaseModel):
    created: int
    updated: int
    total: int
    feed: str
    generation_queued: int


class LeadUploadResult(BaseModel):
    items: list[LeadRead]
    created: int
    updated: int
    total: int
    generation_queued: int


class CurrentEmailSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: EmailStatus
    recipient_email: str | None = None
    created_at: datetime
    updated_at: datetime


class LeadListRead(LeadRead):
    current_email: CurrentEmailSummary | None = None
    latest_generation: EmailGenerationJobRead | None = None


class LeadWorkspaceRead(BaseModel):
    lead: LeadRead
    emails: list[EmailRead]
    default_email_signature: str
    current_email_id: str | None = None
    current_email_is_stale: bool = False
    latest_generation: EmailGenerationJobRead | None = None
