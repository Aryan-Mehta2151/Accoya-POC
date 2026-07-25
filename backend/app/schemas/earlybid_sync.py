"""Safe API representations of durable EarlyBid synchronization state."""

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.db.models import EarlyBidSyncRunStatus


class EarlyBidSyncRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    feed: str
    schedule_date: date
    scheduled_for: datetime
    status: EarlyBidSyncRunStatus
    attempt_count: int
    error_code: str | None = None
    next_attempt_at: datetime | None = None
    created: int
    updated: int
    total: int
    generation_queued: int
    claimed_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def serialize_native_uuid(cls, value: Any) -> str:
        return str(value)

    @field_validator(
        "scheduled_for",
        "next_attempt_at",
        "claimed_at",
        "completed_at",
        mode="after",
    )
    @classmethod
    def normalize_utc_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class EarlyBidSyncStatusRead(BaseModel):
    timezone: str
    next_scheduled_at: datetime
    overdue: bool
    latest_run: EarlyBidSyncRunRead | None = None
