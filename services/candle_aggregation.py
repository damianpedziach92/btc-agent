from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from models.candle import Candle
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository


AGGREGATE_TIMEFRAMES = (
    Timeframe.FIVE_MINUTES,
    Timeframe.FIFTEEN_MINUTES,
    Timeframe.ONE_HOUR,
    Timeframe.FOUR_HOURS,
    Timeframe.ONE_DAY,
)
MINUTE = timedelta(minutes=1)


def aggregate_candles(candles: list[Candle], timeframe: Timeframe) -> list[Candle]:
    """Build only complete UTC-aligned candles from one-minute candles."""
    if timeframe is Timeframe.ONE_MINUTE:
        raise ValueError("Use a timeframe longer than one minute")

    groups: dict[tuple[Instrument, datetime], list[Candle]] = defaultdict(list)
    for candle in candles:
        if candle.timeframe is not Timeframe.ONE_MINUTE:
            raise ValueError("Source candles must have a one-minute timeframe")
        timestamp = candle.timestamp.astimezone(timezone.utc)
        start_of_day = datetime.combine(timestamp.date(), datetime.min.time(), timezone.utc)
        minute_of_day = timestamp.hour * 60 + timestamp.minute
        start = start_of_day + timedelta(
            minutes=(minute_of_day // timeframe.minutes) * timeframe.minutes
        )
        groups[(candle.instrument, start)].append(candle)

    result = []
    for (instrument, start), group in sorted(groups.items(), key=lambda item: item[0][1]):
        group.sort(key=lambda candle: candle.timestamp)
        if len(group) != timeframe.minutes or any(
            candle.timestamp.astimezone(timezone.utc) != start + index * MINUTE
            for index, candle in enumerate(group)
        ):
            continue

        volume = sum((candle.volume for candle in group), Decimal(0))
        weighted_vwap = sum(
            (candle.vwap * candle.volume for candle in group), Decimal(0)
        )
        result.append(Candle(
            instrument=instrument,
            timeframe=timeframe,
            timestamp=start,
            open=group[0].open,
            high=max(candle.high for candle in group),
            low=min(candle.low for candle in group),
            close=group[-1].close,
            vwap=weighted_vwap / volume if volume else Decimal(0),
            volume=volume,
            trades=sum(candle.trades for candle in group),
        ))

    return result


def aggregate_day(session: Session, instrument: Instrument, day: date) -> dict[Timeframe, int]:
    repository = CandleRepository(session)
    source = repository.get_one_minute_for_day(instrument, day)
    return {
        timeframe: repository.save_many(aggregate_candles(source, timeframe))
        for timeframe in AGGREGATE_TIMEFRAMES
    }
