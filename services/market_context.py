"""One bounded, deterministic market snapshot for each analyst call."""
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.candle import CandleORM
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository
from services.indicators import calculate_indicators
from services.market_structure import calculate_market_structure


def build_market_context(session: Session, as_of: datetime, ticker: dict | None = None) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("Market context time must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    timeframes = {}
    for timeframe in (Timeframe.ONE_MINUTE, Timeframe.FIVE_MINUTES,
                      Timeframe.FIFTEEN_MINUTES, Timeframe.ONE_HOUR):
        step = timedelta(minutes=timeframe.minutes)
        rows = list(session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == timeframe,
            CandleORM.timestamp + step <= as_of,
        ).order_by(CandleORM.timestamp.desc()).limit(201)))
        source = [CandleRepository._to_candle(row) for row in rows]
        indicator = calculate_indicators(source)
        structure = calculate_market_structure(source)
        last_end = (rows[0].timestamp + step).astimezone(timezone.utc) if rows else None
        recent = []
        for candle in reversed(source[:10]):
            item = candle.model_dump(mode="json", exclude={"instrument", "timeframe"})
            item["timestamp"] = candle.timestamp.astimezone(timezone.utc).isoformat()
            recent.append(item)
        indicator_data = indicator.model_dump(mode="json", exclude={"instrument", "timeframe"}) if indicator else None
        if indicator_data:
            indicator_data["timestamp"] = indicator.timestamp.astimezone(timezone.utc).isoformat()
        if structure["timestamp"]:
            structure["timestamp"] = datetime.fromisoformat(structure["timestamp"]).astimezone(timezone.utc).isoformat()
        timeframes[timeframe.value] = {
            "last_closed_at": last_end.isoformat() if last_end else None,
            "age_seconds": int((as_of-last_end).total_seconds()) if last_end else None,
            "consecutive_count": structure["consecutive_count"],
            "recent_closed_candles": recent,
            "indicators": indicator_data,
            "market_structure": structure,
        }
    result = {"instrument": Instrument.BTC_USD.value, "as_of": as_of.isoformat(),
              "ticker": ticker, "timeframes": timeframes,
              "cost_model": {"execution_model": "CFD_FIXED_20_USD", "spread_usd_per_btc": "20",
                             "trade_fee_rate": "0", "financing_rate": "0"}}
    payload = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    result["sha256"] = sha256(payload.encode("utf-8")).hexdigest()
    return result
