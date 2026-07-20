"""Pydantic schemas for leads (EarlyBid opportunities)."""
from datetime import datetime

from pydantic import BaseModel


class LeadRead(BaseModel):
    id: str
    external_id: str
    section: str | None = None
    project: str | None = None
    location: str | None = None
    state: str | None = None
    signal: str | None = None
    intelligence: str | None = None
    score: int | None = None
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

    class Config:
        from_attributes = True


class SyncResult(BaseModel):
    created: int
    updated: int
    total: int
    feed: str
