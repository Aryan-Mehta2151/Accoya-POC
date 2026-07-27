"""Add durable access-request approvals.

Revision ID: 0008_access_request_approval
Revises: 0007_web_auth_security
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0008_access_request_approval"
down_revision: str | None = "0007_web_auth_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_CHECK = (
    "(status = 'pending' AND token_hash IS NOT NULL "
    "AND expires_at IS NOT NULL AND reviewed_at IS NULL "
    "AND reviewed_by IS NULL) OR "
    "(status IN ('approved', 'rejected', 'expired') "
    "AND token_hash IS NULL AND expires_at IS NULL "
    "AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)"
)


def upgrade() -> None:
    """Create access_requests for email-driven enrollment approvals."""

    access_request_status = postgresql.ENUM(
        "pending",
        "approved",
        "rejected",
        "expired",
        name="access_request_status",
        create_type=False,
    )
    access_request_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "access_requests",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            access_request_status,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_user_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_access_requests_token_hash"),
        sa.CheckConstraint(
            "length(trim(email)) > 0 AND email = lower(trim(email))",
            name="ck_access_requests_email_normalized",
        ),
        sa.CheckConstraint(
            "token_hash IS NULL OR length(token_hash) = 64",
            name="ck_access_requests_token_hash_sha256",
        ),
        sa.CheckConstraint(
            _STATUS_CHECK,
            name="ck_access_requests_status_shape",
        ),
    )

    op.create_index(
        "uq_access_requests_pending_email",
        "access_requests",
        ["email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_access_requests_status_requested_at",
        "access_requests",
        ["status", "requested_at"],
    )
    op.create_index(
        "ix_access_requests_email_requested_at",
        "access_requests",
        ["email", "requested_at"],
    )


def downgrade() -> None:
    """Drop access-request approval structures."""

    op.drop_index(
        "ix_access_requests_email_requested_at",
        table_name="access_requests",
    )
    op.drop_index(
        "ix_access_requests_status_requested_at",
        table_name="access_requests",
    )
    op.drop_index(
        "uq_access_requests_pending_email",
        table_name="access_requests",
    )
    op.drop_table("access_requests")

    access_request_status = postgresql.ENUM(
        "pending",
        "approved",
        "rejected",
        "expired",
        name="access_request_status",
        create_type=False,
    )
    access_request_status.drop(op.get_bind(), checkfirst=True)
