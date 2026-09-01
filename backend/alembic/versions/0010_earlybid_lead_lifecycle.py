"""Add EarlyBid lifecycle and expanded feed fields.

Revision ID: 0010_earlybid_lead_lifecycle
Revises: 0009_email_signatures
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0010_earlybid_lead_lifecycle"
down_revision: str | None = "0009_email_signatures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


review_status = postgresql.ENUM(
    "active",
    "deleted",
    name="lead_review_status",
    create_type=False,
)


def upgrade() -> None:
    """Persist the additive feed contract and permit safe pre-claim failures."""

    bind = op.get_bind()
    review_status.create(bind, checkfirst=True)
    op.add_column("leads", sa.Column("reported", postgresql.JSONB(), nullable=True))
    op.add_column("leads", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column("leads", sa.Column("award_date", sa.Date(), nullable=True))
    op.add_column("leads", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column(
        "leads",
        sa.Column("response_deadline_evidence", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column(
            "keywords_matched",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("leads", sa.Column("review_status", review_status, nullable=True))
    op.add_column("leads", sa.Column("deleted_by", sa.Text(), nullable=True))
    op.add_column(
        "leads",
        sa.Column(
            "deleted_reasons",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # Existing visible rows preserve today's behavior. Legacy locally archived
    # rows remain unknown and hidden until the full EarlyBid feed supplies truth.
    op.execute(
        "UPDATE leads SET review_status = 'active' "
        "WHERE archived_at IS NULL"
    )
    op.create_index(
        "ix_leads_review_status",
        "leads",
        ["review_status"],
        unique=False,
    )

    op.drop_constraint(
        "ck_earlybid_sync_runs_result_count_bounds",
        "earlybid_sync_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_earlybid_sync_runs_result_count_bounds",
        "earlybid_sync_runs",
        "created_count + updated_count <= total_count "
        "AND generation_queued_count <= created_count + updated_count",
    )

    op.drop_constraint(
        "ck_email_generation_jobs_lifecycle",
        "email_generation_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_generation_jobs_lifecycle",
        "email_generation_jobs",
        "(status = 'queued' AND claimed_at IS NULL "
        "AND heartbeat_at IS NULL AND completed_at IS NULL "
        "AND attempt_count = 0) OR "
        "(status = 'running' AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND completed_at IS NULL "
        "AND attempt_count > 0) OR "
        "(status IN ('generated', 'insufficient_context', "
        "'provider_error', 'system_error') AND completed_at IS NOT NULL "
        "AND (attempt_count > 0 OR status = 'system_error'))",
    )

    op.drop_constraint(
        "ck_email_delivery_jobs_lifecycle",
        "email_delivery_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_delivery_jobs_lifecycle",
        "email_delivery_jobs",
        "(status = 'queued' AND attempt_count = 0 "
        "AND claimed_by IS NULL AND claimed_at IS NULL "
        "AND heartbeat_at IS NULL AND send_started_at IS NULL "
        "AND accepted_at IS NULL AND completed_at IS NULL "
        "AND error_code IS NULL) OR "
        "(status = 'running' AND attempt_count > 0 "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
        "AND accepted_at IS NULL AND completed_at IS NULL "
        "AND error_code IS NULL) OR "
        "(status = 'succeeded' AND attempt_count > 0 "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
        "AND accepted_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND error_code IS NULL) OR "
        "(status = 'failed' AND attempt_count = 0 "
        "AND claimed_by IS NULL AND claimed_at IS NULL "
        "AND heartbeat_at IS NULL AND send_started_at IS NULL "
        "AND accepted_at IS NULL AND completed_at IS NOT NULL "
        "AND error_code IS NOT NULL) OR "
        "(status IN ('failed', 'delivery_unknown') AND attempt_count > 0 "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
        "AND accepted_at IS NULL AND completed_at IS NOT NULL "
        "AND error_code IS NOT NULL)",
    )


def downgrade() -> None:
    """Remove expanded feed state and restore the original queue constraints."""

    op.drop_constraint(
        "ck_email_delivery_jobs_lifecycle",
        "email_delivery_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_delivery_jobs_lifecycle",
        "email_delivery_jobs",
        "(status = 'queued' AND attempt_count = 0 "
        "AND claimed_by IS NULL AND claimed_at IS NULL "
        "AND heartbeat_at IS NULL AND send_started_at IS NULL "
        "AND accepted_at IS NULL AND completed_at IS NULL "
        "AND error_code IS NULL) OR "
        "(status = 'running' AND attempt_count > 0 "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
        "AND accepted_at IS NULL AND completed_at IS NULL "
        "AND error_code IS NULL) OR "
        "(status = 'succeeded' AND attempt_count > 0 "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
        "AND accepted_at IS NOT NULL AND completed_at IS NOT NULL "
        "AND error_code IS NULL) OR "
        "(status IN ('failed', 'delivery_unknown') AND attempt_count > 0 "
        "AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND send_started_at IS NOT NULL "
        "AND accepted_at IS NULL AND completed_at IS NOT NULL "
        "AND error_code IS NOT NULL)",
    )
    op.drop_constraint(
        "ck_email_generation_jobs_lifecycle",
        "email_generation_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_generation_jobs_lifecycle",
        "email_generation_jobs",
        "(status = 'queued' AND claimed_at IS NULL "
        "AND heartbeat_at IS NULL AND completed_at IS NULL "
        "AND attempt_count = 0) OR "
        "(status = 'running' AND claimed_at IS NOT NULL "
        "AND heartbeat_at IS NOT NULL AND completed_at IS NULL "
        "AND attempt_count > 0) OR "
        "(status IN ('generated', 'insufficient_context', "
        "'provider_error', 'system_error') "
        "AND completed_at IS NOT NULL AND attempt_count > 0)",
    )
    op.drop_constraint(
        "ck_earlybid_sync_runs_result_count_bounds",
        "earlybid_sync_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_earlybid_sync_runs_result_count_bounds",
        "earlybid_sync_runs",
        "created_count + updated_count <= total_count "
        "AND generation_queued_count <= created_count",
    )

    op.drop_index("ix_leads_review_status", table_name="leads")
    for column in (
        "deleted_reasons",
        "deleted_by",
        "review_status",
        "keywords_matched",
        "response_deadline_evidence",
        "start_date",
        "award_date",
        "due_date",
        "reported",
    ):
        op.drop_column("leads", column)
    review_status.drop(op.get_bind(), checkfirst=True)
