from enum import Enum


class Instrument(str, Enum):
    BTC_USD = "BTC/USD"


class Timeframe(str, Enum):
    ONE_MINUTE = "1m"
    FIFTEEN_MINUTES = "15m"
    ONE_HOUR = "1h"
    FOUR_HOURS = "4h"
    ONE_DAY = "1d"

    @property
    def minutes(self) -> int:
        return {
            Timeframe.ONE_MINUTE: 1,
            Timeframe.FIFTEEN_MINUTES: 15,
            Timeframe.ONE_HOUR: 60,
            Timeframe.FOUR_HOURS: 240,
            Timeframe.ONE_DAY: 1440,
        }[self]
