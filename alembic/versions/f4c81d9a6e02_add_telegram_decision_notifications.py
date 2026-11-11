"""Persist Telegram delivery for agent decisions.

Revision ID: f4c81d9a6e02
Revises: e9e754b42f66
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4c81d9a6e02"
down_revision: Union[str, Sequence[str], None] = "e9e754b42f66"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_decisions", sa.Column("paper_result", postgresql.JSONB(), nullable=True))
    op.create_table(
        "telegram_notifications",
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('PENDING', 'SENT')", name="ck_telegram_notification_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_telegram_notification_attempts"),
        sa.ForeignKeyConstraint(["decision_id"], ["agent_decisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index("ix_telegram_notifications_pending", "telegram_notifications", ["created_at"],
                    postgresql_where=sa.text("status = 'PENDING'"))


def downgrade() -> None:
    op.drop_index("ix_telegram_notifications_pending", table_name="telegram_notifications")
    op.drop_table("telegram_notifications")
    op.drop_column("agent_decisions", "paper_result")
