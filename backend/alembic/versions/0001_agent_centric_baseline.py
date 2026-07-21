"""Create the greenfield agent-centric schema.

Revision ID: 0001_agent_centric_baseline
Revises:
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_agent_centric_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


agent_run_status = postgresql.ENUM(
    "running",
    "generated",
    "insufficient_context",
    "provider_error",
    "system_error",
    name="agent_run_status",
    create_type=False,
)
email_status = postgresql.ENUM(
    "draft",
    "pending_review",
    "approved",
    "sent",
    "rejected",
    name="email_status",
    create_type=False,
)


def upgrade() -> None:
    """Create the complete baseline; legacy data is intentionally not imported."""

    bind = op.get_bind()
    agent_run_status.create(bind, checkfirst=True)
    email_status.create(bind, checkfirst=True)

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "source_system",
            sa.Text(),
            server_default="earlybid",
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("signal", sa.Text(), nullable=True),
        sa.Column("intelligence", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("timing", sa.Text(), nullable=True),
        sa.Column("next_step", sa.Text(), nullable=True),
        sa.Column("awarded_to", sa.Text(), nullable=True),
        sa.Column("priority_reasons", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("contacts", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.Text(), nullable=True),
        sa.Column("meeting_date", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column(
            "raw_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source_feed", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "archived_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.UniqueConstraint(
            "source_system",
            "external_id",
            name="uq_leads_source_system_external_id",
        ),
    )
    op.create_index("ix_leads_score", "leads", ["score"], unique=False)
    op.create_index(
        "ix_leads_archived_at", "leads", ["archived_at"], unique=False
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "retry_of_run_id", postgresql.UUID(as_uuid=False), nullable=True
        ),
        sa.Column(
            "status",
            agent_run_status,
            server_default="running",
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("selected_product_family", sa.Text(), nullable=True),
        sa.Column("selected_application", sa.Text(), nullable=True),
        sa.Column("nurturing_email_number", sa.Integer(), nullable=True),
        sa.Column("nurturing_email_theme", sa.Text(), nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("original_subject", sa.Text(), nullable=True),
        sa.Column("original_body", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("catalog_version", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "retrieval_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64",
            name="ck_agent_runs_input_hash_sha256",
        ),
        sa.CheckConstraint(
            "nurturing_email_number IS NULL OR "
            "nurturing_email_number BETWEEN 1 AND 7",
            name="ck_agent_runs_nurturing_email_number",
        ),
        sa.CheckConstraint(
            "model_calls >= 0 AND retrieval_count >= 0",
            name="ck_agent_runs_operation_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0) AND "
            "(latency_ms IS NULL OR latency_ms >= 0)",
            name="ck_agent_runs_telemetry_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL "
            "AND original_subject IS NULL AND original_body IS NULL) OR "
            "(status = 'generated' AND completed_at IS NOT NULL "
            "AND original_subject IS NOT NULL AND original_body IS NOT NULL) OR "
            "(status IN ('insufficient_context', 'provider_error', 'system_error') "
            "AND completed_at IS NOT NULL "
            "AND original_subject IS NULL AND original_body IS NULL)",
            name="ck_agent_runs_terminal_shape",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name="fk_agent_runs_lead_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_run_id"],
            ["agent_runs.id"],
            name="fk_agent_runs_retry_of_run_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index(
        "ix_agent_runs_lead_started_at",
        "agent_runs",
        ["lead_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_status_started_at",
        "agent_runs",
        ["status", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_retry_of_run_id",
        "agent_runs",
        ["retry_of_run_id"],
        unique=False,
    )

    op.create_table(
        "emails",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("recipient_email", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            email_status,
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name="fk_emails_agent_run_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_emails"),
        sa.UniqueConstraint("agent_run_id", name="uq_emails_agent_run_id"),
    )
    op.create_index(
        "ix_emails_status_created_at",
        "emails",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "email_status_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("email_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("previous_status", email_status, nullable=True),
        sa.Column("new_status", email_status, nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["email_id"],
            ["emails.id"],
            name="fk_email_status_events_email_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_status_events"),
    )
    op.create_index(
        "ix_email_status_events_email_created_at",
        "email_status_events",
        ["email_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
    )
    op.create_index(
        "ix_chat_messages_session_id",
        "chat_messages",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "strategy_documents",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("s3_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_documents"),
    )


def downgrade() -> None:
    """Remove the greenfield schema."""

    op.drop_table("strategy_documents")
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index(
        "ix_email_status_events_email_created_at",
        table_name="email_status_events",
    )
    op.drop_table("email_status_events")
    op.drop_index("ix_emails_status_created_at", table_name="emails")
    op.drop_table("emails")
    op.drop_index("ix_agent_runs_retry_of_run_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status_started_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_lead_started_at", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_leads_archived_at", table_name="leads")
    op.drop_index("ix_leads_score", table_name="leads")
    op.drop_table("leads")

    bind = op.get_bind()
    email_status.drop(bind, checkfirst=True)
    agent_run_status.drop(bind, checkfirst=True)
