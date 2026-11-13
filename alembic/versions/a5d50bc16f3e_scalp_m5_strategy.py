"""Persist strategy identity and its entry-time parameters without rewriting history.

Revision ID: a5d50bc16f3e
Revises: f2b91d0c3a76
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "a5d50bc16f3e"
down_revision = "f2b91d0c3a76"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("paper_positions", "paper_pending_orders"):
        op.add_column(table, sa.Column("strategy", sa.String(24), nullable=True))
        op.add_column(table, sa.Column("strategy_parameters", JSONB(), nullable=True))
        op.create_check_constraint(f"ck_{table}_strategy", table,
                                   "strategy IS NULL OR strategy IN ('INTRADAY_M15','SCALP_M5')")
    op.add_column("paper_positions", sa.Column("setup_type", sa.String(80), nullable=True))


def downgrade():
    op.drop_column("paper_positions", "setup_type")
    for table in ("paper_positions", "paper_pending_orders"):
        op.drop_constraint(f"ck_{table}_strategy", table, type_="check")
        op.drop_column(table, "strategy_parameters")
        op.drop_column(table, "strategy")
