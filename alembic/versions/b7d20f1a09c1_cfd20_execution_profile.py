"""Version PAPER execution profiles without rewriting historical positions.

Revision ID: b7d20f1a09c1
Revises: e8a6b92c1d04
"""
from alembic import op
import sqlalchemy as sa

revision = "b7d20f1a09c1"
down_revision = "e8a6b92c1d04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("paper_fee_profiles", sa.Column("execution_model", sa.String(32), nullable=False,
                                                     server_default="LEGACY_BPS"))
    op.add_column("paper_fee_profiles", sa.Column("spread_usd_per_btc", sa.Numeric(28, 12), nullable=False,
                                                     server_default="0"))
    op.create_check_constraint("ck_paper_fee_execution_model", "paper_fee_profiles",
                               "execution_model IN ('LEGACY_BPS', 'CFD_FIXED_20_USD')")
    op.create_check_constraint("ck_paper_fee_spread_usd_nonnegative", "paper_fee_profiles",
                               "spread_usd_per_btc >= 0")


def downgrade():
    op.drop_constraint("ck_paper_fee_spread_usd_nonnegative", "paper_fee_profiles", type_="check")
    op.drop_constraint("ck_paper_fee_execution_model", "paper_fee_profiles", type_="check")
    op.drop_column("paper_fee_profiles", "spread_usd_per_btc")
    op.drop_column("paper_fee_profiles", "execution_model")
