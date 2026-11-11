from datetime import timedelta, timezone
from decimal import Decimal
from collections import deque

from sqlalchemy.orm import Session

from models.candle import Candle
from models.indicators import IndicatorSnapshot
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository
from repositories.indicator_repository import IndicatorRepository


BOLLINGER_PERIOD = 20
BOLLINGER_DEVIATIONS = Decimal(2)
MA_PERIODS = (50, 100, 200)


def _snapshot(latest: Candle, recent: list[Candle]) -> IndicatorSnapshot:
    """Build indicators from consecutive candles ordered newest first."""
    closes = [candle.close for candle in recent]

    def moving_average(period: int) -> Decimal | None:
        return sum(closes[:period], Decimal(0)) / period if len(closes) >= period else None

    middle = moving_average(BOLLINGER_PERIOD)
    if middle is None:
        upper = lower = None
    else:
        variance = sum(
            ((close - middle) ** 2 for close in closes[:BOLLINGER_PERIOD]),
            Decimal(0),
        ) / BOLLINGER_PERIOD
        deviation = variance.sqrt()
        upper = middle + BOLLINGER_DEVIATIONS * deviation
        lower = middle - BOLLINGER_DEVIATIONS * deviation

    return IndicatorSnapshot(
        instrument=latest.instrument,
        timeframe=latest.timeframe,
        timestamp=latest.timestamp,
        ma50=moving_average(50),
        ma100=moving_average(100),
        ma200=moving_average(200),
        bb_middle=middle,
        bb_upper=upper,
        bb_lower=lower,
    )


def calculate_indicators(candles: list[Candle]) -> IndicatorSnapshot | None:
    """Calculate indicators from the latest consecutive closed candles."""
    if not candles:
        return None

    candles = sorted(candles, key=lambda candle: candle.timestamp, reverse=True)
    latest = candles[0]
    if any(
        candle.instrument != latest.instrument or candle.timeframe != latest.timeframe
        for candle in candles
    ):
        raise ValueError("All candles must share an instrument and timeframe")

    step = timedelta(minutes=latest.timeframe.minutes)
    consecutive = [latest]
    for candle in candles[1:]:
        if consecutive[-1].timestamp.astimezone(timezone.utc) - candle.timestamp.astimezone(timezone.utc) != step:
            break
        consecutive.append(candle)

    return _snapshot(latest, consecutive[:max(MA_PERIODS)])


def calculate_indicator_history(
    candles: list[Candle], limit: int
) -> list[IndicatorSnapshot]:
    """Return the last `limit` snapshots, using earlier candles for warm-up."""
    if not candles or limit <= 0:
        return []

    candles = sorted(candles, key=lambda candle: candle.timestamp)
    first = candles[0]
    if any(
        candle.instrument != first.instrument or candle.timeframe != first.timeframe
        for candle in candles
    ):
        raise ValueError("All candles must share an instrument and timeframe")

    step = timedelta(minutes=first.timeframe.minutes)
    recent: deque[Candle] = deque(maxlen=max(MA_PERIODS))
    snapshots = []
    first_output_index = max(0, len(candles) - limit)
    for index, candle in enumerate(candles):
        if recent and candle.timestamp.astimezone(timezone.utc) - recent[-1].timestamp.astimezone(timezone.utc) != step:
            recent.clear()
        recent.append(candle)
        if index >= first_output_index:
            snapshots.append(_snapshot(candle, list(reversed(recent))))
    return snapshots


def update_indicators(session: Session, instrument: Instrument) -> list[IndicatorSnapshot]:
    candles = CandleRepository(session)
    snapshots = IndicatorRepository(session)
    result = []
    for timeframe in Timeframe:
        source = candles.get_latest(instrument, timeframe, max(MA_PERIODS))
        snapshot = calculate_indicators(source)
        if snapshot is not None:
            snapshots.save(snapshot)
            result.append(snapshot)
    return result
