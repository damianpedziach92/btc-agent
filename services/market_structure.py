"""Structure and volatility from consecutive closed candles."""

from datetime import timedelta, timezone
from decimal import Decimal

from models.candle import Candle


ATR_PERIOD = 14
CHANNEL_PERIOD = 20


def calculate_market_structure(candles: list[Candle], swing_candles: int = 2) -> dict:
    """Return Wilder ATR14, previous-20 channel and recent swing levels.

    The channel excludes the newest candle so a breakout can be compared with
    the preceding range. All inputs must be stored closed candles.
    """
    if swing_candles < 1:
        raise ValueError("swing_candles must be positive")
    if not candles:
        return {"timestamp": None, "consecutive_count": 0, "atr14": None,
                "channel_high_20": None, "channel_low_20": None,
                "swing_high": None, "swing_low": None, "swing_candles": swing_candles}
    rows = sorted(candles, key=lambda candle: candle.timestamp, reverse=True)
    latest = rows[0]
    step = timedelta(minutes=latest.timeframe.minutes)
    consecutive = [latest]
    for candle in rows[1:]:
        if (candle.instrument != latest.instrument or candle.timeframe != latest.timeframe or
            consecutive[-1].timestamp.astimezone(timezone.utc) - candle.timestamp.astimezone(timezone.utc) != step):
            break
        consecutive.append(candle)
    chronological = list(reversed(consecutive))
    atr = None
    if len(chronological) >= ATR_PERIOD + 1:
        true_ranges = [max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ) for previous, current in zip(chronological, chronological[1:])]
        atr = sum(true_ranges[:ATR_PERIOD], Decimal(0)) / ATR_PERIOD
        for true_range in true_ranges[ATR_PERIOD:]:
            atr = (atr * (ATR_PERIOD - 1) + true_range) / ATR_PERIOD
    prior_channel = consecutive[1:CHANNEL_PERIOD + 1]
    recent = consecutive[:swing_candles]
    return {
        "timestamp": latest.timestamp.isoformat(),
        "consecutive_count": len(consecutive),
        "atr14": str(atr) if atr is not None else None,
        "channel_high_20": str(max(row.high for row in prior_channel)) if len(prior_channel) == CHANNEL_PERIOD else None,
        "channel_low_20": str(min(row.low for row in prior_channel)) if len(prior_channel) == CHANNEL_PERIOD else None,
        "swing_high": str(max(row.high for row in recent)) if len(recent) == swing_candles else None,
        "swing_low": str(min(row.low for row in recent)) if len(recent) == swing_candles else None,
        "swing_candles": swing_candles,
    }
