"""Add optional outreach email signatures.

Revision ID: 0009_email_signatures
Revises: 0008_access_request_approval
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0009_email_signatures"
down_revision: str | None = "0008_access_request_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable per-email signature without backfilling old drafts."""

    op.add_column("emails", sa.Column("signature", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove the optional per-email signature."""

    op.drop_column("emails", "signature")
