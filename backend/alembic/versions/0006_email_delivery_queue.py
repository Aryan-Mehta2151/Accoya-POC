"""Add the durable outreach-email delivery queue.

Revision ID: 0006_email_delivery_queue
Revises: 0005_earlybid_daily_sync
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0006_email_delivery_queue"
down_revision: str | None = "0005_earlybid_daily_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


delivery_status = postgresql.ENUM(
    "queued",
    "running",
    "succeeded",
    "failed",
    "delivery_unknown",
    name="email_delivery_job_status",
    create_type=False,
)


def upgrade() -> None:
    """Create durable SMTP attempts and their concurrency constraints."""

    bind = op.get_bind()
    delivery_status.create(bind, checkfirst=True)
    op.create_table(
        "email_delivery_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("email_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "retry_of_job_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
        sa.Column(
            "status",
            delivery_status,
            server_default="queued",
            nullable=False,
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("sender_email", sa.Text(), nullable=False),
        sa.Column("recipient_email", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_snapshot", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column(
            "queued_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "claimed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "heartbeat_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "send_started_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "accepted_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_email_delivery_jobs_content_hash_sha256",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_email_delivery_jobs_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND claimed_by IS NULL "
            "AND claimed_at IS NULL "
            "AND heartbeat_at IS NULL AND send_started_at IS NULL "
            "AND accepted_at IS NULL AND completed_at IS NULL "
            "AND attempt_count = 0 AND error_code IS NULL) OR "
            "(status = 'running' AND claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
            "AND accepted_at IS NULL AND completed_at IS NULL "
            "AND attempt_count > 0 AND error_code IS NULL) OR "
            "(status = 'succeeded' AND claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
            "AND accepted_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND attempt_count > 0 AND error_code IS NULL) OR "
            "(status IN ('failed', 'delivery_unknown') "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL "
            "AND send_started_at IS NOT NULL AND accepted_at IS NULL "
            "AND completed_at IS NOT NULL AND attempt_count > 0 "
            "AND error_code IS NOT NULL)",
            name="ck_email_delivery_jobs_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["email_id"],
            ["emails.id"],
            name="fk_email_delivery_jobs_email_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_job_id"],
            ["email_delivery_jobs.id"],
            name="fk_email_delivery_jobs_retry_of_job_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_delivery_jobs"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_email_delivery_jobs_idempotency_key",
        ),
        sa.UniqueConstraint(
            "message_id",
            name="uq_email_delivery_jobs_message_id",
        ),
    )
    op.create_index(
        "ix_email_delivery_jobs_one_active_per_email",
        "email_delivery_jobs",
        ["email_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_email_delivery_jobs_status_queued_at",
        "email_delivery_jobs",
        ["status", "queued_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_delivery_jobs_email_queued_at",
        "email_delivery_jobs",
        ["email_id", "queued_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_delivery_jobs_retry_of_job_id",
        "email_delivery_jobs",
        ["retry_of_job_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the delivery queue."""

    op.drop_index(
        "ix_email_delivery_jobs_retry_of_job_id",
        table_name="email_delivery_jobs",
    )
    op.drop_index(
        "ix_email_delivery_jobs_email_queued_at",
        table_name="email_delivery_jobs",
    )
    op.drop_index(
        "ix_email_delivery_jobs_status_queued_at",
        table_name="email_delivery_jobs",
    )
    op.drop_index(
        "ix_email_delivery_jobs_one_active_per_email",
        table_name="email_delivery_jobs",
    )
    op.drop_table("email_delivery_jobs")
    delivery_status.drop(op.get_bind(), checkfirst=True)
