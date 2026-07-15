"""Pydantic schemas for emails."""
from datetime import datetime

from pydantic import BaseModel

from app.db.models import EmailStatus


class EmailRead(BaseModel):
    id: str
    lead_id: str
    subject: str | None
    body: str
    status: EmailStatus
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmailStatusUpdate(BaseModel):
    status: EmailStatus


class EmailEdit(BaseModel):
    subject: str | None = None
    body: str | None = None
