"""Add latest indicator snapshots for each instrument and timeframe.

Revision ID: 9c51f4a2d77e
Revises: d0ce157ff0c9
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c51f4a2d77e"
down_revision: Union[str, Sequence[str], None] = "d0ce157ff0c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "indicator_snapshots",
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ma50", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("ma100", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("ma200", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("bb_middle", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("bb_upper", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("bb_lower", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.PrimaryKeyConstraint("symbol", "timeframe"),
    )


def downgrade() -> None:
    op.drop_table("indicator_snapshots")
