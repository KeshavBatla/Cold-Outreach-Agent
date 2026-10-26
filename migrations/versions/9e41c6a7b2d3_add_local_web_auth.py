"""add local web authentication credentials and sessions

Revision ID: 9e41c6a7b2d3
Revises: 7c2d9a4b6f10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9e41c6a7b2d3"
down_revision: Union[str, None] = "7c2d9a4b6f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "local_credentials",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("login_name", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_local_credentials_login_name",
        "local_credentials",
        ["login_name"],
        unique=True,
    )
    op.create_table(
        "web_auth_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_web_auth_sessions_user_id", "web_auth_sessions", ["user_id"])
    op.create_index("ix_web_auth_sessions_expires_at", "web_auth_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_web_auth_sessions_expires_at", table_name="web_auth_sessions")
    op.drop_index("ix_web_auth_sessions_user_id", table_name="web_auth_sessions")
    op.drop_table("web_auth_sessions")
    op.drop_index("ix_local_credentials_login_name", table_name="local_credentials")
    op.drop_table("local_credentials")
