from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum as SqlEnum, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from models.market import Instrument, Timeframe


class IndicatorSnapshotORM(Base):
    __tablename__ = "indicator_snapshots"

    symbol: Mapped[Instrument] = mapped_column(
        SqlEnum(
            Instrument,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=False,
            length=20,
            validate_strings=True,
        ),
        primary_key=True,
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        SqlEnum(
            Timeframe,
            values_callable=lambda enum: [member.value for member in enum],
            native_enum=False,
            length=10,
            validate_strings=True,
        ),
        primary_key=True,
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ma50: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    ma100: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    ma200: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    bb_middle: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    bb_upper: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    bb_lower: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
