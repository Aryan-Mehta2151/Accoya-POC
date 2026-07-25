"""Add durable daily EarlyBid synchronization runs.

Revision ID: 0005_earlybid_daily_sync
Revises: 0004_email_generation_queue
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0005_earlybid_daily_sync"
down_revision: str | None = "0004_email_generation_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


run_status = postgresql.ENUM(
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    name="earlybid_sync_run_status",
    create_type=False,
)


def upgrade() -> None:
    """Create persistent, source-scoped daily synchronization slots."""

    bind = op.get_bind()
    run_status.create(bind, checkfirst=True)
    op.create_table(
        "earlybid_sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("reseller", sa.Text(), nullable=False),
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column(
            "scheduled_for",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            run_status,
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
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
        sa.Column(
            "created_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "updated_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "total_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "generation_queued_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 4",
            name="ck_earlybid_sync_runs_attempt_count",
        ),
        sa.CheckConstraint(
            "created_count >= 0 AND updated_count >= 0 "
            "AND total_count >= 0 AND generation_queued_count >= 0",
            name="ck_earlybid_sync_runs_result_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "created_count + updated_count <= total_count "
            "AND generation_queued_count <= created_count",
            name="ck_earlybid_sync_runs_result_count_bounds",
        ),
        sa.CheckConstraint(
            "status = 'succeeded' OR "
            "(created_count = 0 AND updated_count = 0 AND total_count = 0 "
            "AND generation_queued_count = 0)",
            name="ck_earlybid_sync_runs_terminal_result_shape",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND attempt_count = 0 "
            "AND claimed_by IS NULL AND claimed_at IS NULL "
            "AND heartbeat_at IS NULL AND next_attempt_at IS NULL "
            "AND completed_at IS NULL AND error_code IS NULL) OR "
            "(status = 'running' AND attempt_count BETWEEN 1 AND 4 "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND next_attempt_at IS NULL "
            "AND completed_at IS NULL AND error_code IS NULL) OR "
            "(status = 'retry_wait' AND attempt_count BETWEEN 1 AND 3 "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND next_attempt_at IS NOT NULL "
            "AND completed_at IS NULL AND error_code IS NOT NULL) OR "
            "(status = 'succeeded' AND attempt_count BETWEEN 1 AND 4 "
            "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL AND next_attempt_at IS NULL "
            "AND completed_at IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND next_attempt_at IS NULL "
            "AND completed_at IS NOT NULL AND error_code IS NOT NULL AND "
            "((attempt_count = 0 AND claimed_by IS NULL "
            "AND claimed_at IS NULL AND heartbeat_at IS NULL) OR "
            "(attempt_count BETWEEN 1 AND 4 AND claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL)))",
            name="ck_earlybid_sync_runs_lifecycle",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_earlybid_sync_runs"),
        sa.UniqueConstraint(
            "reseller",
            "client",
            "schedule_date",
            name="uq_earlybid_sync_runs_feed_schedule_date",
        ),
    )
    op.create_index(
        "ix_earlybid_sync_runs_due",
        "earlybid_sync_runs",
        ["status", "next_attempt_at", "scheduled_for"],
        unique=False,
    )
    op.create_index(
        "ix_earlybid_sync_runs_feed_schedule",
        "earlybid_sync_runs",
        ["reseller", "client", "schedule_date"],
        unique=False,
    )
    op.create_index(
        "ix_earlybid_sync_runs_heartbeat",
        "earlybid_sync_runs",
        ["status", "heartbeat_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove daily synchronization state."""

    op.drop_index(
        "ix_earlybid_sync_runs_heartbeat",
        table_name="earlybid_sync_runs",
    )
    op.drop_index(
        "ix_earlybid_sync_runs_feed_schedule",
        table_name="earlybid_sync_runs",
    )
    op.drop_index(
        "ix_earlybid_sync_runs_due",
        table_name="earlybid_sync_runs",
    )
    op.drop_table("earlybid_sync_runs")
    run_status.drop(op.get_bind(), checkfirst=True)
