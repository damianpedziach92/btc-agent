
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models.candle import CandleORM
from models.candle import Candle
from models.market import Instrument, Timeframe


class CandleRepository:
    def __init__(self, session: Session):
        self.session = session

    def save_many(self, candles: list[Candle]) -> int:
        if not candles:
            return 0

        records = [
            {
                "symbol": candle.instrument,
                **candle.model_dump(exclude={"instrument"}),
            }
            for candle in candles
        ]

        statement = insert(CandleORM).values(records)

        statement = statement.on_conflict_do_nothing(
            constraint="uq_candles_symbol_timeframe_timestamp"
        ).returning(CandleORM.id)

        result = self.session.execute(statement)

        inserted_ids = result.scalars().all()

        return len(inserted_ids)

    def get_one_minute_bounds(self, instrument: Instrument) -> tuple[datetime, datetime] | None:
        first, last = self.session.execute(
            select(func.min(CandleORM.timestamp), func.max(CandleORM.timestamp)).where(
                CandleORM.symbol == instrument,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
            )
        ).one()
        return (first, last) if first is not None else None

    def get_latest(
        self, instrument: Instrument, timeframe: Timeframe, limit: int
    ) -> list[Candle]:
        rows = self.session.scalars(
            select(CandleORM).where(
                CandleORM.symbol == instrument,
                CandleORM.timeframe == timeframe,
            ).order_by(CandleORM.timestamp.desc()).limit(limit)
        ).all()
        return [self._to_candle(row) for row in rows]

    def get_one_minute_for_day(self, instrument: Instrument, day: date) -> list[Candle]:
        start = datetime.combine(day, datetime.min.time(), timezone.utc)
        end = start + timedelta(days=1)
        rows = self.session.scalars(
            select(CandleORM).where(
                CandleORM.symbol == instrument,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
                CandleORM.timestamp >= start,
                CandleORM.timestamp < end,
            ).order_by(CandleORM.timestamp)
        ).all()
        return [self._to_candle(row) for row in rows]

    @staticmethod
    def _to_candle(row: CandleORM) -> Candle:
        return Candle(
            instrument=row.symbol,
            timeframe=row.timeframe,
            timestamp=row.timestamp,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            vwap=row.vwap,
            volume=row.volume,
            trades=row.trades,
        )
