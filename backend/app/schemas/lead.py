"""Pydantic schemas for leads (EarlyBid opportunities)."""
from datetime import datetime
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.db.models import EmailStatus
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


class LeadArchiveResult(BaseModel):
    id: str
    archived: bool


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
    current_email_id: str | None = None
    current_email_is_stale: bool = False
    latest_generation: EmailGenerationJobRead | None = None
