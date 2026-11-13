"""Store actual tool use and action-level review results.

Revision ID: e8a6b92c1d04
Revises: c6f9e2a17b43
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e8a6b92c1d04"
down_revision: Union[str, Sequence[str], None] = "c6f9e2a17b43"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_decisions", sa.Column("analysis_trace", postgresql.JSONB(), nullable=True))
    op.add_column("decision_reviews", sa.Column("action_reviews", postgresql.JSONB(), nullable=True))
    op.drop_constraint("ck_decision_reviews_assessment", "decision_reviews", type_="check")
    op.create_check_constraint(
        "ck_decision_reviews_assessment", "decision_reviews",
        "assessment IN ('NO_ISSUES', 'ISSUES_FOUND', 'INSUFFICIENT_DATA', 'NO_DECISIONS', 'NO_ACTIONS')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_decision_reviews_assessment", "decision_reviews", type_="check")
    op.create_check_constraint(
        "ck_decision_reviews_assessment", "decision_reviews",
        "assessment IN ('NO_ISSUES', 'ISSUES_FOUND', 'INSUFFICIENT_DATA', 'NO_DECISIONS')",
    )
    op.drop_column("decision_reviews", "action_reviews")
    op.drop_column("agent_decisions", "analysis_trace")
