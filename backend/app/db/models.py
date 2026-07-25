"""SQLAlchemy ORM models for the agent-centric application database."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uuid() -> str:
    """Return a UUID string while PostgreSQL stores it in a native UUID column."""

    return str(uuid.uuid4())


# JSON keeps provider-free SQLite unit tests useful; PostgreSQL receives JSONB.
_JSONB = JSON().with_variant(JSONB(), "postgresql")
_UUID = Uuid(as_uuid=False)


class EmailStatus(str, enum.Enum):
    draft = "draft"
    pending_review = "pending_review"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"


class AgentRunStatus(str, enum.Enum):
    running = "running"
    generated = "generated"
    insufficient_context = "insufficient_context"
    provider_error = "provider_error"
    system_error = "system_error"


class EmailGenerationJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    generated = "generated"
    insufficient_context = "insufficient_context"
    provider_error = "provider_error"
    system_error = "system_error"


class EmailGenerationTrigger(str, enum.Enum):
    earlybid_sync = "earlybid_sync"
    csv_upload = "csv_upload"
    manual = "manual"
    retry = "retry"


_EMAIL_STATUS = Enum(EmailStatus, name="email_status")
_AGENT_RUN_STATUS = Enum(AgentRunStatus, name="agent_run_status")
_EMAIL_GENERATION_JOB_STATUS = Enum(
    EmailGenerationJobStatus,
    name="email_generation_job_status",
)
_EMAIL_GENERATION_TRIGGER = Enum(
    EmailGenerationTrigger,
    name="email_generation_trigger",
)


class Lead(Base):
    """The current normalized projection of one source opportunity."""

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "external_id",
            name="uq_leads_source_system_external_id",
        ),
        Index("ix_leads_score", "score"),
        Index("ix_leads_archived_at", "archived_at"),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    source_system: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="earlybid",
        server_default="earlybid",
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Agent input text is deliberately unrestricted; provider prompt limits are
    # an application concern rather than a persistence truncation policy.
    section: Mapped[str | None] = mapped_column(Text)
    project: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    signal: Mapped[str | None] = mapped_column(Text)
    intelligence: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    timing: Mapped[str | None] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(Text)
    awarded_to: Mapped[str | None] = mapped_column(Text)
    priority_reasons: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    contacts: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    meeting_date: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | str | None] = mapped_column(_JSONB)
    url: Mapped[str | None] = mapped_column(Text)

    raw_data: Mapped[dict[str, Any]] = mapped_column(
        _JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    source_feed: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    email_generation_jobs: Mapped[list[EmailGenerationJob]] = relationship(
        back_populates="lead",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EmailGenerationJob(Base):
    """Durable request for one asynchronous outreach-generation attempt."""

    __tablename__ = "email_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "length(requested_input_hash) = 64",
            name="ck_email_generation_jobs_input_hash_sha256",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_email_generation_jobs_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "(status = 'queued' AND claimed_at IS NULL "
            "AND heartbeat_at IS NULL AND completed_at IS NULL "
            "AND attempt_count = 0) OR "
            "(status = 'running' AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND completed_at IS NULL "
            "AND attempt_count > 0) OR "
            "(status IN ('generated', 'insufficient_context', "
            "'provider_error', 'system_error') "
            "AND completed_at IS NOT NULL AND attempt_count > 0)",
            name="ck_email_generation_jobs_lifecycle",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_email_generation_jobs_idempotency_key",
        ),
        Index(
            "ix_email_generation_jobs_one_active_per_lead",
            "lead_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_email_generation_jobs_status_queued_at",
            "status",
            "queued_at",
        ),
        Index(
            "ix_email_generation_jobs_lead_queued_at",
            "lead_id",
            "queued_at",
        ),
        Index(
            "ix_email_generation_jobs_retry_of_job_id",
            "retry_of_job_id",
        ),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(
        _UUID,
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    retry_of_job_id: Mapped[str | None] = mapped_column(
        _UUID,
        ForeignKey("email_generation_jobs.id", ondelete="SET NULL"),
    )
    trigger: Mapped[EmailGenerationTrigger] = mapped_column(
        _EMAIL_GENERATION_TRIGGER,
        nullable=False,
    )
    status: Mapped[EmailGenerationJobStatus] = mapped_column(
        _EMAIL_GENERATION_JOB_STATUS,
        nullable=False,
        default=EmailGenerationJobStatus.queued,
        server_default=EmailGenerationJobStatus.queued.value,
    )
    requested_input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    claimed_by: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="email_generation_jobs")
    retry_of: Mapped[EmailGenerationJob | None] = relationship(
        remote_side="EmailGenerationJob.id",
        back_populates="retries",
        foreign_keys=[retry_of_job_id],
    )
    retries: Mapped[list[EmailGenerationJob]] = relationship(
        back_populates="retry_of",
        foreign_keys=[retry_of_job_id],
    )
    agent_run: Mapped[AgentRun | None] = relationship(
        back_populates="email_generation_job",
        uselist=False,
    )

    @property
    def agent_run_id(self) -> str | None:
        """Expose the linked run without duplicating the foreign key."""

        return self.agent_run.id if self.agent_run is not None else None


class AgentRun(Base):
    """One immutable terminal attempt to generate outreach for a lead."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "length(input_hash) = 64",
            name="ck_agent_runs_input_hash_sha256",
        ),
        CheckConstraint(
            "nurturing_email_number IS NULL OR "
            "nurturing_email_number BETWEEN 1 AND 7",
            name="ck_agent_runs_nurturing_email_number",
        ),
        CheckConstraint(
            "model_calls >= 0 AND retrieval_count >= 0",
            name="ck_agent_runs_operation_counts_nonnegative",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0) AND "
            "(latency_ms IS NULL OR latency_ms >= 0)",
            name="ck_agent_runs_telemetry_nonnegative",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL "
            "AND original_subject IS NULL AND original_body IS NULL) OR "
            "(status = 'generated' AND completed_at IS NOT NULL "
            "AND original_subject IS NOT NULL AND original_body IS NOT NULL) OR "
            "(status IN ('insufficient_context', 'provider_error', 'system_error') "
            "AND completed_at IS NOT NULL "
            "AND original_subject IS NULL AND original_body IS NULL)",
            name="ck_agent_runs_terminal_shape",
        ),
        Index("ix_agent_runs_lead_started_at", "lead_id", "started_at"),
        Index("ix_agent_runs_status_started_at", "status", "started_at"),
        Index("ix_agent_runs_retry_of_run_id", "retry_of_run_id"),
        Index("ix_agent_runs_started_at_id", "started_at", "id"),
        UniqueConstraint(
            "email_generation_job_id",
            name="uq_agent_runs_email_generation_job_id",
        ),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(
        _UUID,
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    retry_of_run_id: Mapped[str | None] = mapped_column(
        _UUID,
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
    )
    email_generation_job_id: Mapped[str | None] = mapped_column(
        _UUID,
        ForeignKey("email_generation_jobs.id", ondelete="SET NULL"),
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        _AGENT_RUN_STATUS,
        nullable=False,
        default=AgentRunStatus.running,
        server_default=AgentRunStatus.running.value,
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    selected_product_family: Mapped[str | None] = mapped_column(Text)
    selected_application: Mapped[str | None] = mapped_column(Text)
    nurturing_email_number: Mapped[int | None] = mapped_column(Integer)
    nurturing_email_theme: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[str]] = mapped_column(
        _JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    error_code: Mapped[str | None] = mapped_column(Text)

    # This is the immutable generated draft. Human edits are stored on Email.
    original_subject: Mapped[str | None] = mapped_column(Text)
    original_body: Mapped[str | None] = mapped_column(Text)

    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    retrieval_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="agent_runs")
    retry_of: Mapped[AgentRun | None] = relationship(
        remote_side="AgentRun.id",
        back_populates="retries",
        foreign_keys=[retry_of_run_id],
    )
    retries: Mapped[list[AgentRun]] = relationship(
        back_populates="retry_of",
        foreign_keys=[retry_of_run_id],
    )
    email_generation_job: Mapped[EmailGenerationJob | None] = relationship(
        back_populates="agent_run",
    )
    email: Mapped[Email | None] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class Email(Base):
    """The mutable, human-reviewed copy of a generated run draft."""

    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_emails_agent_run_id"),
        Index("ix_emails_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    agent_run_id: Mapped[str] = mapped_column(
        _UUID,
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_email: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[EmailStatus] = mapped_column(
        _EMAIL_STATUS,
        nullable=False,
        default=EmailStatus.pending_review,
        server_default=EmailStatus.pending_review.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    agent_run: Mapped[AgentRun] = relationship(
        back_populates="email",
        lazy="joined",
    )
    status_events: Mapped[list[EmailStatusEvent]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EmailStatusEvent.created_at",
    )

    @property
    def lead_id(self) -> str:
        """Keep the existing EmailRead wire shape without duplicating lead_id."""

        return self.agent_run.lead_id


class EmailStatusEvent(Base):
    """Append-only audit record for an email review-status transition."""

    __tablename__ = "email_status_events"
    __table_args__ = (
        Index("ix_email_status_events_email_created_at", "email_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    email_id: Mapped[str] = mapped_column(
        _UUID,
        ForeignKey("emails.id", ondelete="CASCADE"),
        nullable=False,
    )
    previous_status: Mapped[EmailStatus | None] = mapped_column(_EMAIL_STATUS)
    new_status: Mapped[EmailStatus] = mapped_column(_EMAIL_STATUS, nullable=False)
    actor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    email: Mapped[Email] = relationship(back_populates="status_events")


class ChatMessage(Base):
    """Chat history for the QnA chatbot (grouped by session_id)."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "seq",
            name="uq_chat_messages_session_seq",
        ),
        Index("ix_chat_messages_session_seq", "session_id", "seq"),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    # Per-session 1-based message serial number for stable ordering.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16))  # "human" | "ai"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class User(Base):
    """User account for authentication."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    oauth_provider: Mapped[str | None] = mapped_column(String(50))  # "google"
    oauth_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PasswordResetToken(Base):
    """Password reset tokens for email-based authentication."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_password_reset_tokens_token"),
        Index("ix_password_reset_tokens_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        _UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class StrategyDocument(Base):
    """Metadata for strategy docs stored in S3 and indexed in Bedrock KB."""

    __tablename__ = "strategy_documents"

    id: Mapped[str] = mapped_column(_UUID, primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512))
    s3_key: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
