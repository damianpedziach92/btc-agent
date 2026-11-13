"""Versioned PAPER strategy identities and research parameters."""
from enum import Enum


class Strategy(str, Enum):
    INTRADAY_M15 = "INTRADAY_M15"
    SCALP_M5 = "SCALP_M5"


class ScalpSetup(str, Enum):
    M5_PULLBACK_RETEST = "M5_PULLBACK_RETEST"
    M5_RANGE_REJECTION = "M5_RANGE_REJECTION"
    M5_BREAKOUT_RETEST = "M5_BREAKOUT_RETEST"


class IntradaySetup(str, Enum):
    M15_PULLBACK_RETEST = "M15_PULLBACK_RETEST"
    M15_RANGE_REJECTION = "M15_RANGE_REJECTION"
    M15_BREAKOUT_RETEST = "M15_BREAKOUT_RETEST"


SCALP_PARAMETERS_V1 = {
    "version": "SCALP_M5_V1",
    "atr_period": 14,
    "swing_candles": 3,
    "stop_buffer_atr": "0.20",
    "minimum_stop_atr": "0.60",
    "max_hold_minutes": 60,
}


def validate_strategy_setup(strategy: Strategy | None, setup_type: str | None) -> str | None:
    if strategy is None:
        return "STRATEGY_REQUIRED"
    if strategy is Strategy.SCALP_M5 and setup_type not in {item.value for item in ScalpSetup}:
        return "UNAUTHORIZED_SCALP_SETUP"
    if strategy is Strategy.INTRADAY_M15 and setup_type not in {item.value for item in IntradaySetup}:
        return "UNAUTHORIZED_INTRADAY_SETUP"
    return None
