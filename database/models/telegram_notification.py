from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class TelegramNotificationORM(Base):
    __tablename__ = "telegram_notifications"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'SENT', 'DISABLED')", name="ck_telegram_notification_status"),
        CheckConstraint("attempts >= 0", name="ck_telegram_notification_attempts"),
        Index("ix_telegram_notifications_pending", "created_at", postgresql_where=text("status = 'PENDING'")),
    )

    decision_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_decisions.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
