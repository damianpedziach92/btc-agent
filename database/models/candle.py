
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    Integer,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from models.market import Instrument, Timeframe


class CandleORM(Base):
    __tablename__ = "candles"

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timeframe",
            "timestamp",
            name="uq_candles_symbol_timeframe_timestamp",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

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

    timeframe: Mapped[Timeframe] = mapped_column(
        SqlEnum(
            Timeframe,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=False,
            length=10,
            validate_strings=True,
        ),
        nullable=False,
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    open: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    high: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    low: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    vwap: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    volume: Mapped[Decimal] = mapped_column(Numeric(30, 12))

    trades: Mapped[int] = mapped_column(Integer)
