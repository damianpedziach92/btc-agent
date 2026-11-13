from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class DecisionReviewORM(Base):
    __tablename__ = "decision_reviews"
    __table_args__ = (
        UniqueConstraint("instrument", "window_end", name="uq_decision_reviews_instrument_window"),
        CheckConstraint(
            "assessment IN ('NO_ISSUES', 'ISSUES_FOUND', 'INSUFFICIENT_DATA', 'NO_DECISIONS', 'NO_ACTIONS')",
            name="ck_decision_reviews_assessment",
        ),
        CheckConstraint(
            "notification_status IN ('DISABLED', 'PENDING', 'SENT')",
            name="ck_decision_reviews_notification_status",
        ),
        CheckConstraint("notification_attempts >= 0", name="ck_decision_reviews_attempts"),
        Index("ix_decision_reviews_pending", "created_at",
              postgresql_where=text("notification_status = 'PENDING'")),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewer_model: Mapped[str] = mapped_column(String(100), nullable=False)
    instructions_hash: Mapped[str] = mapped_column(String(12), nullable=False)
    assessment: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    findings: Mapped[list] = mapped_column(JSONB, nullable=False)
    action_reviews: Mapped[list | None] = mapped_column(JSONB)
    data_gaps: Mapped[list] = mapped_column(JSONB, nullable=False)
    decision_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    notification_status: Mapped[str] = mapped_column(String(12), nullable=False)
    notification_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_error: Mapped[str | None] = mapped_column(Text)
