"""Position-only Telegram outbox and suppression of old queued reports.

Revision ID: f2b91d0c3a76
Revises: d491a2ce102b
"""
from alembic import op
import sqlalchemy as sa

revision = "f2b91d0c3a76"
down_revision = "d491a2ce102b"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_telegram_notification_status", "telegram_notifications", type_="check")
    op.create_check_constraint("ck_telegram_notification_status", "telegram_notifications",
                               "status IN ('PENDING', 'SENT', 'DISABLED')")
    op.execute("UPDATE telegram_notifications SET status='DISABLED' WHERE status='PENDING'")
    op.execute("UPDATE decision_reviews SET notification_status='DISABLED' WHERE notification_status='PENDING'")
    op.execute("UPDATE paper_order_events SET notification_status='DISABLED' WHERE notification_status='PENDING'")
    op.create_table(
        "paper_position_notifications",
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("paper_position_events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.CheckConstraint("status IN ('PENDING', 'SENT')", name="ck_paper_position_notification_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_paper_position_notification_attempts"),
    )
    op.create_index("ix_paper_position_notifications_pending", "paper_position_notifications", ["created_at"],
                    postgresql_where=sa.text("status = 'PENDING'"))


def downgrade():
    op.drop_index("ix_paper_position_notifications_pending", table_name="paper_position_notifications")
    op.drop_table("paper_position_notifications")
    op.execute("UPDATE telegram_notifications SET status='SENT' WHERE status='DISABLED'")
    op.drop_constraint("ck_telegram_notification_status", "telegram_notifications", type_="check")
    op.create_check_constraint("ck_telegram_notification_status", "telegram_notifications",
                               "status IN ('PENDING', 'SENT')")
