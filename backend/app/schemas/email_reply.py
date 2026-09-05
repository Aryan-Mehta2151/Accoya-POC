"""Public contracts for Microsoft Graph reply tracking."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class EmailReplySummaryRead(BaseModel):
    unread_reply_count: int
    replied_opportunity_count: int
    last_synced_at: datetime | None = None
    sync_status: Literal["initializing", "healthy", "stale", "disabled", "error"]
