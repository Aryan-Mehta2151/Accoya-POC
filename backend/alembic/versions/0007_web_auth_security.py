"""Harden browser authentication and account identity storage.

Revision ID: 0007_web_auth_security
Revises: 0006_email_delivery_queue
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_web_auth_security"
down_revision: str | None = "0006_email_delivery_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Normalize identities and invalidate legacy passwords and reset links."""

    # Abort rather than guessing how to merge accounts that normalize to the
    # same address or already share an external identity.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM users
                GROUP BY lower(btrim(email))
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'users contain duplicate emails after normalization';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM users
                WHERE btrim(email) = ''
            ) THEN
                RAISE EXCEPTION 'users contain a blank email address';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM users
                WHERE (oauth_provider IS NULL) <> (oauth_id IS NULL)
                   OR (oauth_provider IS NOT NULL AND (
                       btrim(oauth_provider) = '' OR btrim(oauth_id) = ''
                   ))
            ) THEN
                RAISE EXCEPTION
                    'users contain an incomplete OAuth identity';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM users
                WHERE oauth_provider IS NOT NULL
                GROUP BY oauth_provider, oauth_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'users contain a duplicate OAuth identity';
            END IF;
        END $$;
        """
    )

    # Legacy self-service signup accepted passwords without the new policy.
    # Invalidate every old hash so activation cannot silently restore a weak
    # credential; an administrator must explicitly set a new password.
    op.execute(
        "UPDATE users SET email = lower(btrim(email)), password_hash = NULL"
    )
    op.add_column(
        "users",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "auth_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_users_auth_version_nonnegative",
        "users",
        "auth_version >= 0",
    )
    op.create_check_constraint(
        "ck_users_oauth_identity_complete",
        "users",
        "(oauth_provider IS NULL AND oauth_id IS NULL) OR "
        "(oauth_provider IS NOT NULL AND oauth_id IS NOT NULL "
        "AND length(trim(oauth_provider)) > 0 "
        "AND length(trim(oauth_id)) > 0)",
    )
    op.create_check_constraint(
        "ck_users_email_normalized",
        "users",
        "email = lower(trim(email)) AND length(trim(email)) > 0",
    )
    op.create_unique_constraint(
        "uq_users_oauth_provider_id",
        "users",
        ["oauth_provider", "oauth_id"],
    )
    op.create_index(
        "uq_users_email_normalized",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )

    # Existing reset links are intentionally invalid after the deployment.
    op.execute("DELETE FROM password_reset_tokens")
    op.drop_constraint(
        "uq_password_reset_tokens_token",
        "password_reset_tokens",
        type_="unique",
    )
    op.drop_column("password_reset_tokens", "token")
    op.add_column(
        "password_reset_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
    )
    op.create_unique_constraint(
        "uq_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
    )
    op.create_check_constraint(
        "ck_password_reset_tokens_hash_sha256",
        "password_reset_tokens",
        "length(token_hash) = 64",
    )


def downgrade() -> None:
    """Restore the legacy schema; invalidated reset secrets stay invalid."""

    op.drop_constraint(
        "ck_password_reset_tokens_hash_sha256",
        "password_reset_tokens",
        type_="check",
    )
    op.drop_constraint(
        "uq_password_reset_tokens_token_hash",
        "password_reset_tokens",
        type_="unique",
    )
    op.add_column(
        "password_reset_tokens",
        sa.Column("token", sa.String(length=255), nullable=True),
    )
    op.execute("UPDATE password_reset_tokens SET token = id::text")
    op.alter_column(
        "password_reset_tokens",
        "token",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_password_reset_tokens_token",
        "password_reset_tokens",
        ["token"],
    )
    op.drop_column("password_reset_tokens", "token_hash")

    op.drop_index("uq_users_email_normalized", table_name="users")
    op.drop_constraint(
        "uq_users_oauth_provider_id",
        "users",
        type_="unique",
    )
    op.drop_constraint(
        "ck_users_email_normalized",
        "users",
        type_="check",
    )
    op.drop_constraint(
        "ck_users_oauth_identity_complete",
        "users",
        type_="check",
    )
    op.drop_constraint(
        "ck_users_auth_version_nonnegative",
        "users",
        type_="check",
    )
    op.drop_column("users", "auth_version")
    op.drop_column("users", "is_active")
