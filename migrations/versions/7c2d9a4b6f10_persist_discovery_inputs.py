"""persist batch discovery inputs

Revision ID: 7c2d9a4b6f10
Revises: 48d5e3e57a59
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7c2d9a4b6f10"
down_revision: Union[str, None] = "48d5e3e57a59"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "batch_items",
        sa.Column("mode", sa.String(length=24), server_default="open_role", nullable=False),
    )
    op.add_column("batch_items", sa.Column("candidate_profile", sa.Text(), nullable=True))
    op.add_column("batch_items", sa.Column("target_seniority", sa.String(length=200), nullable=True))
    op.add_column("batch_items", sa.Column("target_domain_keywords", sa.String(length=500), nullable=True))
    op.add_column("batch_items", sa.Column("location_preference", sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column("batch_items", "location_preference")
    op.drop_column("batch_items", "target_domain_keywords")
    op.drop_column("batch_items", "target_seniority")
    op.drop_column("batch_items", "candidate_profile")
    op.drop_column("batch_items", "mode")
