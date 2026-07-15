"""SQLAlchemy ORM models."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class EmailStatus(str, enum.Enum):
    draft = "draft"
    pending_review = "pending_review"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"


class Lead(Base):
    """A single opportunity from the EarlyBid feed (one row = one card in the UI).

    Mirrors the `earlystack_client_feed_v1` CSV schema. `external_id` is the feed's
    stable opportunity id, used for upsert/dedupe on daily sync.
    """

    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    external_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)

    section: Mapped[str | None] = mapped_column(String(64))
    project: Mapped[str | None] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str | None] = mapped_column(String(8))
    signal: Mapped[str | None] = mapped_column(String(32))
    intelligence: Mapped[str | None] = mapped_column(String(32))
    score: Mapped[int | None] = mapped_column(Integer)
    timing: Mapped[str | None] = mapped_column(String(512))
    awarded_to: Mapped[str | None] = mapped_column(Text)
    priority_reasons: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    contacts: Mapped[str | None] = mapped_column(Text)
    # Contact email parsed out of `contacts`; the approved email is sent here.
    contact_email: Mapped[str | None] = mapped_column(String(320))
    meeting_date: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)

    # Full original row (JSON) for fidelity, and where it came from.
    raw_data: Mapped[str | None] = mapped_column(Text)
    source_feed: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    emails: Mapped[list[Email]] = relationship(back_populates="lead", cascade="all, delete-orphan")


class Email(Base):
    """A generated outreach email for a lead, moving through the approval workflow."""

    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"))
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[EmailStatus] = mapped_column(Enum(EmailStatus), default=EmailStatus.draft)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    lead: Mapped[Lead] = relationship(back_populates="emails")


class ChatMessage(Base):
    """Chat history for the QnA chatbot (grouped by session_id)."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategyDocument(Base):
    """Metadata for uploaded strategy docs stored in S3 and indexed in Bedrock KB."""

    __tablename__ = "strategy_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512))
    s3_key: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
