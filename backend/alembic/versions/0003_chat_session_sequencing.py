"""Add per-session ordering and human/ai roles to chat_messages.

Revision ID: 0003_chat_session_sequencing
Revises: 0002_agent_run_pagination_index
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_chat_session_sequencing"
down_revision: str | None = "0002_agent_run_pagination_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add chat_messages.seq, backfill ordering, and standardize roles."""

    op.add_column(
        "chat_messages",
        sa.Column("seq", sa.Integer(), nullable=True),
    )

    # Backfill a stable 1-based per-session order from existing timestamps.
    op.execute(
        """
        WITH ordered AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY session_id
                    ORDER BY created_at, id
                ) AS rn
            FROM chat_messages
        )
        UPDATE chat_messages AS cm
        SET seq = ordered.rn
        FROM ordered
        WHERE cm.id = ordered.id
        """
    )

    # Standardize legacy role labels to the human/ai contract.
    op.execute("UPDATE chat_messages SET role = 'human' WHERE role = 'user'")
    op.execute("UPDATE chat_messages SET role = 'ai' WHERE role = 'assistant'")

    op.alter_column("chat_messages", "seq", nullable=False)

    op.create_unique_constraint(
        "uq_chat_messages_session_seq",
        "chat_messages",
        ["session_id", "seq"],
    )
    op.create_index(
        "ix_chat_messages_session_seq",
        "chat_messages",
        ["session_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    """Reverse the chat sequencing and role standardization."""

    op.drop_index("ix_chat_messages_session_seq", table_name="chat_messages")
    op.drop_constraint(
        "uq_chat_messages_session_seq",
        "chat_messages",
        type_="unique",
    )
    op.execute("UPDATE chat_messages SET role = 'assistant' WHERE role = 'ai'")
    op.execute("UPDATE chat_messages SET role = 'user' WHERE role = 'human'")
    op.drop_column("chat_messages", "seq")
