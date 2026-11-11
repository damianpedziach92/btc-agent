"""Add structured agent decisions and durable monitor items.

Revision ID: aa284d380b71
Revises: 7b84c6de1180
"""

from alembic import op
import sqlalchemy as sa


revision = "aa284d380b71"
down_revision = "7b84c6de1180"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("instrument", sa.String(20), nullable=False),
        sa.Column("candle_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.UniqueConstraint("instrument", "candle_timestamp", name="uq_agent_decisions_instrument_candle"),
        sa.CheckConstraint("decision IN ('BUY', 'SELL', 'WAIT')", name="ck_agent_decisions_decision"),
    )
    op.create_table(
        "agent_monitor_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("instrument", sa.String(20), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_decision_id", sa.Uuid(), sa.ForeignKey("agent_decisions.id"), nullable=False),
        sa.UniqueConstraint("instrument", "key", name="uq_agent_monitor_items_instrument_key"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'TRIGGERED', 'INVALIDATED', 'EXPIRED')",
            name="ck_agent_monitor_items_status",
        ),
    )
    op.create_index(
        "ix_agent_monitor_items_status_expires_at", "agent_monitor_items", ["status", "expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_monitor_items_status_expires_at", table_name="agent_monitor_items")
    op.drop_table("agent_monitor_items")
    op.drop_table("agent_decisions")
