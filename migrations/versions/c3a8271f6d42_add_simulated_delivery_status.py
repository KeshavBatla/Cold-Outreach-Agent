"""allow explicitly simulated Gmail delivery records

Revision ID: c3a8271f6d42
Revises: 9e41c6a7b2d3
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3a8271f6d42"
down_revision: Union[str, None] = "9e41c6a7b2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("gmail_connections") as batch_op:
        batch_op.alter_column(
            "token_secret_ref",
            existing_type=sa.String(length=512),
            type_=sa.Text(),
            existing_nullable=True,
        )
    with op.batch_alter_table("delivery_records") as batch_op:
        batch_op.drop_constraint("ck_delivery_status", type_="check")
        batch_op.create_check_constraint(
            "ck_delivery_status",
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown', 'cancelled', 'simulated')",
        )


def downgrade() -> None:
    with op.batch_alter_table("delivery_records") as batch_op:
        batch_op.drop_constraint("ck_delivery_status", type_="check")
        batch_op.create_check_constraint(
            "ck_delivery_status",
            "status IN ('queued', 'sending', 'sent', 'failed', 'unknown', 'cancelled')",
        )
    with op.batch_alter_table("gmail_connections") as batch_op:
        batch_op.alter_column(
            "token_secret_ref",
            existing_type=sa.Text(),
            type_=sa.String(length=512),
            existing_nullable=True,
        )
