"""Response contracts for the Overview dashboard."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class OverviewSummaryRead(BaseModel):
    opportunities: int
    needs_review: int
    sent: int
    replies: int
    reply_sync_status: Literal[
        "initializing",
        "healthy",
        "stale",
        "disabled",
        "error",
    ]
    reply_last_synced_at: datetime | None = None
