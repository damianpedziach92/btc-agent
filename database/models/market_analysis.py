from datetime import datetime

from sqlalchemy import DateTime, Enum as SqlEnum, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from models.market import Instrument


class MarketAnalysisORM(Base):
    __tablename__ = "market_analyses"
    __table_args__ = (
        UniqueConstraint("symbol", "candle_timestamp", name="uq_market_analyses_symbol_candle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[Instrument] = mapped_column(
        SqlEnum(
            Instrument,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=False,
            length=20,
            validate_strings=True,
        ),
        nullable=False,
    )
    candle_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
