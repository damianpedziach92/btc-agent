
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from models.market import Instrument, Timeframe


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vwap: Decimal
    volume: Decimal
    trades: int
