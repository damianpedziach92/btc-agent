"""Store one market analysis per closed 15-minute candle.

Revision ID: 7b84c6de1180
Revises: 9c51f4a2d77e
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b84c6de1180"
down_revision: Union[str, Sequence[str], None] = "9c51f4a2d77e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("candle_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("analysis", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol", "candle_timestamp", name="uq_market_analyses_symbol_candle"
        ),
    )


def downgrade() -> None:
    op.drop_table("market_analyses")
