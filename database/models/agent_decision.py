from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class AgentDecisionORM(Base):
    __tablename__ = "agent_decisions"
    __table_args__ = (
        UniqueConstraint("instrument", "candle_timestamp", name="uq_agent_decisions_instrument_candle"),
        CheckConstraint("decision IN ('BUY', 'SELL', 'WAIT')", name="ck_agent_decisions_decision"),
        CheckConstraint("paper_status IN ('PENDING', 'DONE')", name="ck_agent_decisions_paper_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    candle_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    open_proposal: Mapped[dict | None] = mapped_column(JSONB)
    position_actions: Mapped[list | None] = mapped_column(JSONB)
    pending_order_actions: Mapped[list | None] = mapped_column(JSONB)
    analysis_trace: Mapped[dict | None] = mapped_column(JSONB)
    paper_result: Mapped[dict | None] = mapped_column(JSONB)
    paper_status: Mapped[str] = mapped_column(String(12), nullable=False, default="DONE", server_default="DONE")
