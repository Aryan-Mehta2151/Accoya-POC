"""Add the unfiltered agent-run pagination index.

Revision ID: 0002_agent_run_pagination_index
Revises: 0001_agent_centric_baseline
Create Date: 2026-07-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_agent_run_pagination_index"
down_revision: str | None = "0001_agent_centric_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Index the fields used by unfiltered descending cursor pagination."""

    op.create_index(
        "ix_agent_runs_started_at_id",
        "agent_runs",
        ["started_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the unfiltered agent-run pagination index."""

    op.drop_index("ix_agent_runs_started_at_id", table_name="agent_runs")
