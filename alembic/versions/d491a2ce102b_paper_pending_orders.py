"""Durable PAPER pending orders and action history.

Revision ID: d491a2ce102b
Revises: b7d20f1a09c1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "d491a2ce102b"
down_revision = "b7d20f1a09c1"
branch_labels = None
depends_on = None

money = sa.Numeric(28, 12)


def upgrade():
    op.add_column("agent_decisions", sa.Column("pending_order_actions", JSONB(), nullable=True))
    op.create_table(
        "paper_pending_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("paper_accounts.id"), nullable=False),
        sa.Column("instrument", sa.String(20), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("order_type", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("entry_level_usd", money, nullable=False),
        sa.Column("stop_loss", money, nullable=False),
        sa.Column("take_profit", money, nullable=False),
        sa.Column("scenario_key", sa.String(80), nullable=False),
        sa.Column("setup_type", sa.String(80), nullable=False),
        sa.Column("invalidating_condition", JSONB()),
        sa.Column("quantity_policy", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_decision_id", sa.Uuid(), sa.ForeignKey("agent_decisions.id"), nullable=False),
        sa.Column("last_updated_by_decision_id", sa.Uuid(), sa.ForeignKey("agent_decisions.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.Text()),
        sa.Column("position_id", sa.Uuid(), sa.ForeignKey("paper_positions.id")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fee_profile_id", sa.Uuid(), sa.ForeignKey("paper_fee_profiles.id"), nullable=False),
        sa.Column("fee_snapshot", JSONB(), nullable=False),
        sa.Column("last_evaluated_candle_timestamp", sa.DateTime(timezone=True)),
        sa.Column("execution_key", sa.String(160), nullable=False),
        sa.Column("trigger_trace", JSONB()),
        sa.UniqueConstraint("execution_key", name="uq_paper_pending_execution_key"),
        sa.CheckConstraint("status IN ('PENDING','FILLED','CANCELLED','EXPIRED','REJECTED','MISSED')", name="ck_paper_pending_status"),
        sa.CheckConstraint("order_type IN ('BUY_LIMIT','BUY_STOP','SELL_LIMIT','SELL_STOP')", name="ck_paper_pending_type"),
        sa.CheckConstraint("entry_level_usd > 0 AND stop_loss > 0 AND take_profit > 0", name="ck_paper_pending_levels"),
    )
    op.create_index("ix_paper_pending_account_status_expires", "paper_pending_orders",
                    ["account_id", "status", "expires_at"])
    op.create_table(
        "paper_order_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("paper_accounts.id"), nullable=False),
        sa.Column("order_id", sa.Uuid(), sa.ForeignKey("paper_pending_orders.id"), nullable=False),
        sa.Column("decision_id", sa.Uuid(), sa.ForeignKey("agent_decisions.id")),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("dedupe_key", sa.String(180), nullable=False),
        sa.Column("notification_status", sa.String(12), nullable=False, server_default="DISABLED"),
        sa.Column("notification_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("dedupe_key", name="uq_paper_order_event_dedupe"),
    )


def downgrade():
    op.drop_table("paper_order_events")
    op.drop_index("ix_paper_pending_account_status_expires", table_name="paper_pending_orders")
    op.drop_table("paper_pending_orders")
    op.drop_column("agent_decisions", "pending_order_actions")
