"""Add two-hour decision reviews and durable Telegram delivery.

Revision ID: c6f9e2a17b43
Revises: f4c81d9a6e02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c6f9e2a17b43"
down_revision: Union[str, Sequence[str], None] = "f4c81d9a6e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "decision_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument", sa.String(20), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewer_model", sa.String(100), nullable=False),
        sa.Column("instructions_hash", sa.String(12), nullable=False),
        sa.Column("assessment", sa.String(24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("data_gaps", postgresql.JSONB(), nullable=False),
        sa.Column("decision_ids", postgresql.JSONB(), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("notification_status", sa.String(12), nullable=False),
        sa.Column("notification_attempts", sa.Integer(), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "assessment IN ('NO_ISSUES', 'ISSUES_FOUND', 'INSUFFICIENT_DATA', 'NO_DECISIONS')",
            name="ck_decision_reviews_assessment",
        ),
        sa.CheckConstraint(
            "notification_status IN ('DISABLED', 'PENDING', 'SENT')",
            name="ck_decision_reviews_notification_status",
        ),
        sa.CheckConstraint("notification_attempts >= 0", name="ck_decision_reviews_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument", "window_end", name="uq_decision_reviews_instrument_window"),
    )
    op.create_index("ix_decision_reviews_pending", "decision_reviews", ["created_at"],
                    postgresql_where=sa.text("notification_status = 'PENDING'"))


def downgrade() -> None:
    op.drop_index("ix_decision_reviews_pending", table_name="decision_reviews")
    op.drop_table("decision_reviews")
