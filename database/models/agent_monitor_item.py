from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class AgentMonitorItemORM(Base):
    __tablename__ = "agent_monitor_items"
    __table_args__ = (
        UniqueConstraint("instrument", "key", name="uq_agent_monitor_items_instrument_key"),
        CheckConstraint(
            "status IN ('ACTIVE', 'TRIGGERED', 'INVALIDATED', 'EXPIRED')",
            name="ck_agent_monitor_items_status",
        ),
        Index("ix_agent_monitor_items_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_decision_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_decisions.id"), nullable=False
    )
