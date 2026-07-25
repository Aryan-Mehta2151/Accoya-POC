"""Add the durable asynchronous email-generation queue.

Revision ID: 0004_email_generation_queue
Revises: 5662aa7157b7
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_email_generation_queue"
down_revision: str | None = "5662aa7157b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


job_status = postgresql.ENUM(
    "queued",
    "running",
    "generated",
    "insufficient_context",
    "provider_error",
    "system_error",
    name="email_generation_job_status",
    create_type=False,
)
job_trigger = postgresql.ENUM(
    "earlybid_sync",
    "csv_upload",
    "manual",
    "retry",
    name="email_generation_trigger",
    create_type=False,
)


def upgrade() -> None:
    """Create queue state and link each worker-created agent run to its job."""

    bind = op.get_bind()
    job_status.create(bind, checkfirst=True)
    job_trigger.create(bind, checkfirst=True)

    op.create_table(
        "email_generation_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "retry_of_job_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
        sa.Column("trigger", job_trigger, nullable=False),
        sa.Column(
            "status",
            job_status,
            server_default="queued",
            nullable=False,
        ),
        sa.Column("requested_input_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
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
            "completed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "length(requested_input_hash) = 64",
            name="ck_email_generation_jobs_input_hash_sha256",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_email_generation_jobs_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name="fk_email_generation_jobs_lead_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_job_id"],
            ["email_generation_jobs.id"],
            name="fk_email_generation_jobs_retry_of_job_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_generation_jobs"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_email_generation_jobs_idempotency_key",
        ),
    )
    op.create_index(
        "ix_email_generation_jobs_one_active_per_lead",
        "email_generation_jobs",
        ["lead_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_email_generation_jobs_status_queued_at",
        "email_generation_jobs",
        ["status", "queued_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_generation_jobs_lead_queued_at",
        "email_generation_jobs",
        ["lead_id", "queued_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_generation_jobs_retry_of_job_id",
        "email_generation_jobs",
        ["retry_of_job_id"],
        unique=False,
    )

    op.add_column(
        "agent_runs",
        sa.Column(
            "email_generation_job_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_agent_runs_email_generation_job_id",
        "agent_runs",
        "email_generation_jobs",
        ["email_generation_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_agent_runs_email_generation_job_id",
        "agent_runs",
        ["email_generation_job_id"],
    )


def downgrade() -> None:
    """Remove queue state while preserving the pre-queue schema."""

    op.drop_constraint(
        "uq_agent_runs_email_generation_job_id",
        "agent_runs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_agent_runs_email_generation_job_id",
        "agent_runs",
        type_="foreignkey",
    )
    op.drop_column("agent_runs", "email_generation_job_id")

    op.drop_index(
        "ix_email_generation_jobs_retry_of_job_id",
        table_name="email_generation_jobs",
    )
    op.drop_index(
        "ix_email_generation_jobs_lead_queued_at",
        table_name="email_generation_jobs",
    )
    op.drop_index(
        "ix_email_generation_jobs_status_queued_at",
        table_name="email_generation_jobs",
    )
    op.drop_index(
        "ix_email_generation_jobs_one_active_per_lead",
        table_name="email_generation_jobs",
    )
    op.drop_table("email_generation_jobs")

    bind = op.get_bind()
    job_trigger.drop(bind, checkfirst=True)
    job_status.drop(bind, checkfirst=True)
