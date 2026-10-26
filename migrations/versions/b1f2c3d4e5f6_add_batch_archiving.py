"""add is_archived to outreach_batches

Revision ID: b1f2c3d4e5f6
Revises: c3a8271f6d42
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1f2c3d4e5f6"
down_revision: Union[str, None] = "c3a8271f6d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("outreach_batches") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_archived",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("outreach_batches") as batch_op:
        batch_op.drop_column("is_archived")

