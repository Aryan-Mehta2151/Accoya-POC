"""Add durable Microsoft Graph reply tracking.

Revision ID: 0011_email_reply_tracking
Revises: 0010_earlybid_lead_lifecycle
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011_email_reply_tracking"
down_revision: str | None = "0010_earlybid_lead_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


reply_classification = postgresql.ENUM(
    "human",
    "automatic",
    "bounce",
    "ambiguous",
    "unmatched",
    name="email_reply_classification",
    create_type=False,
)
reply_match_method = postgresql.ENUM(
    "references",
    "conversation",
    "none",
    name="email_reply_match_method",
    create_type=False,
)
mailbox_sync_status = postgresql.ENUM(
    "initializing",
    "idle",
    "running",
    "error",
    name="graph_mailbox_sync_status",
    create_type=False,
)


def upgrade() -> None:
    """Persist mailbox checkpoints, reply metadata, and sent-message identity."""

    bind = op.get_bind()
    reply_classification.create(bind, checkfirst=True)
    reply_match_method.create(bind, checkfirst=True)
    mailbox_sync_status.create(bind, checkfirst=True)

    op.add_column(
        "email_delivery_jobs",
        sa.Column("graph_message_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "email_delivery_jobs",
        sa.Column("internet_message_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "email_delivery_jobs",
        sa.Column("conversation_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "email_delivery_jobs",
        sa.Column("sent_item_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_email_delivery_jobs_graph_message_id",
        "email_delivery_jobs",
        ["graph_message_id"],
        unique=True,
        postgresql_where=sa.text("graph_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_email_delivery_jobs_internet_message_id",
        "email_delivery_jobs",
        ["internet_message_id"],
        unique=True,
        postgresql_where=sa.text("internet_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_email_delivery_jobs_conversation_id",
        "email_delivery_jobs",
        ["conversation_id"],
        unique=False,
    )

    op.create_table(
        "graph_mailbox_sync_states",
        sa.Column("mailbox_email", sa.Text(), nullable=False),
        sa.Column(
            "status",
            mailbox_sync_status,
            server_default="initializing",
            nullable=False,
        ),
        sa.Column("subscription_id", sa.Text(), nullable=True),
        sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inbox_delta_link", sa.Text(), nullable=True),
        sa.Column("sent_delta_link", sa.Text(), nullable=True),
        sa.Column("mailbox_scan_link", sa.Text(), nullable=True),
        sa.Column("sent_scan_link", sa.Text(), nullable=True),
        sa.Column("sent_backfill_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "initial_backfill_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "force_resync",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'running' AND claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
            "(status <> 'running' AND claimed_by IS NULL "
            "AND claimed_at IS NULL AND heartbeat_at IS NULL)",
            name="ck_graph_mailbox_sync_states_lease",
        ),
        sa.PrimaryKeyConstraint("mailbox_email", name="pk_graph_mailbox_sync_states"),
        sa.UniqueConstraint(
            "subscription_id",
            name="uq_graph_mailbox_sync_states_subscription_id",
        ),
    )
    op.create_index(
        "ix_graph_mailbox_sync_states_due",
        "graph_mailbox_sync_states",
        ["status", "next_sync_at"],
        unique=False,
    )

    op.create_table(
        "graph_mail_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("mailbox_email", sa.Text(), nullable=False),
        sa.Column("graph_message_id", sa.Text(), nullable=False),
        sa.Column("change_type", sa.String(length=20), nullable=False),
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(claimed_by IS NULL AND claimed_at IS NULL AND heartbeat_at IS NULL) OR "
            "(claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL)",
            name="ck_graph_mail_notifications_lease",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_graph_mail_notifications"),
        sa.UniqueConstraint(
            "mailbox_email",
            "graph_message_id",
            name="uq_graph_mail_notifications_mailbox_message",
        ),
    )
    op.create_index(
        "ix_graph_mail_notifications_due",
        "graph_mail_notifications",
        ["mailbox_email", "requested_at", "processed_at"],
        unique=False,
    )

    op.create_table(
        "email_replies",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("mailbox_email", sa.Text(), nullable=False),
        sa.Column("graph_message_id", sa.Text(), nullable=False),
        sa.Column("internet_message_id", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Text(), nullable=True),
        sa.Column(
            "reference_message_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("lead_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("email_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("delivery_job_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("sender_email", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_read", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("classification", reply_classification, nullable=False),
        sa.Column("match_method", reply_match_method, nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name="fk_email_replies_lead_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["email_id"], ["emails.id"], name="fk_email_replies_email_id", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_job_id"],
            ["email_delivery_jobs.id"],
            name="fk_email_replies_delivery_job_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_replies"),
        sa.UniqueConstraint(
            "mailbox_email",
            "graph_message_id",
            name="uq_email_replies_mailbox_graph_message",
        ),
        sa.UniqueConstraint(
            "mailbox_email",
            "internet_message_id",
            name="uq_email_replies_mailbox_internet_message",
        ),
    )
    op.create_index(
        "ix_email_replies_unread_lead_received",
        "email_replies",
        ["lead_id", "is_read", "received_at"],
        unique=False,
    )
    op.create_index(
        "ix_email_replies_conversation",
        "email_replies",
        ["mailbox_email", "conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_replies_classification",
        "email_replies",
        ["classification"],
        unique=False,
    )


def downgrade() -> None:
    """Remove reply tracking state while preserving the delivery queue."""

    op.drop_index("ix_email_replies_classification", table_name="email_replies")
    op.drop_index("ix_email_replies_conversation", table_name="email_replies")
    op.drop_index("ix_email_replies_unread_lead_received", table_name="email_replies")
    op.drop_table("email_replies")
    op.drop_index("ix_graph_mail_notifications_due", table_name="graph_mail_notifications")
    op.drop_table("graph_mail_notifications")
    op.drop_index("ix_graph_mailbox_sync_states_due", table_name="graph_mailbox_sync_states")
    op.drop_table("graph_mailbox_sync_states")

    op.drop_index("ix_email_delivery_jobs_conversation_id", table_name="email_delivery_jobs")
    op.drop_index("uq_email_delivery_jobs_internet_message_id", table_name="email_delivery_jobs")
    op.drop_index("uq_email_delivery_jobs_graph_message_id", table_name="email_delivery_jobs")
    for column in (
        "sent_item_observed_at",
        "conversation_id",
        "internet_message_id",
        "graph_message_id",
    ):
        op.drop_column("email_delivery_jobs", column)

    mailbox_sync_status.drop(op.get_bind(), checkfirst=True)
    reply_match_method.drop(op.get_bind(), checkfirst=True)
    reply_classification.drop(op.get_bind(), checkfirst=True)
