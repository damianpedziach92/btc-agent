from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from models.market import Instrument, Timeframe


class IndicatorSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    timestamp: datetime
    ma50: Decimal | None
    ma100: Decimal | None
    ma200: Decimal | None
    bb_middle: Decimal | None
    bb_upper: Decimal | None
    bb_lower: Decimal | None
