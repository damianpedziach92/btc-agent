import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

import httpx

from database.connection import SessionLocal
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository
from services.indicators import calculate_indicator_history, MA_PERIODS
from services.market_structure import calculate_market_structure
from services.paper_trading import PaperTradingService
from models.paper import MIN_NET_RISK_REWARD, PaperSide, calculate_risk_reward


KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_PAIRS = {Instrument.BTC_USD: "XBTUSD"}
TIMEFRAMES = [timeframe.value for timeframe in Timeframe]
MAX_HISTORY = 50

TOOL_DEFINITIONS = [
    {
        "type": "function", "name": "get_risk_reward",
        "description": "Read-only net R:R preflight for executable CFD20 entry, SL and TP prices. Prices already include CFD side; quantity is 1 BTC and fees are zero.",
        "strict": True,
        "parameters": {"type": "object", "properties": {
            "side": {"type": "string", "enum": ["LONG", "SHORT"]},
            "entry_execution_price": {"type": "string"},
            "stop_execution_price": {"type": "string"},
            "target_execution_price": {"type": "string"},
        }, "required": ["side", "entry_execution_price", "stop_execution_price", "target_execution_price"],
        "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "get_paper_account",
        "description": "Read the simulated USD account, cash, equity estimate, margin, fee rates and risk limits. Read only.",
        "strict": True,
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "get_open_positions",
        "description": "Read all open simulated BTC/USD positions with entry reasons, prices, protection, costs and stale-data flags. Read only.",
        "strict": True,
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "get_price",
        "description": "Get latest BTC/USD trade, best bid and best ask from Kraken's public spot ticker. Includes retrieval time, not trade time.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": [Instrument.BTC_USD.value]},
            },
            "required": ["instrument"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_indicators",
        "description": "Get the last N indicator readings (MA50/100/200 and Bollinger bands), plus latest Wilder ATR14, previous-20-candle price channel and recent swing levels. All use closed candles. Null means insufficient consecutive candles. Start with 10-20 readings; maximum 50.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": [Instrument.BTC_USD.value]},
                "timeframe": {"type": "string", "enum": TIMEFRAMES},
                "limit": {"type": "integer", "description": "Number of indicator readings, from 1 to 50."},
            },
            "required": ["instrument", "timeframe", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_candles",
        "description": "Get the newest closed candles from the local database for an instrument and timeframe, in chronological order. Start with 10-20 candles; maximum 50.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": [Instrument.BTC_USD.value]},
                "timeframe": {"type": "string", "enum": TIMEFRAMES},
                "limit": {"type": "integer", "description": "Number of candles, from 1 to 50."},
            },
            "required": ["instrument", "timeframe", "limit"],
            "additionalProperties": False,
        },
    },
]


class MarketTools:
    def __init__(self, ticker_get: Callable = httpx.get):
        self.ticker_get = ticker_get

    def get_price(self, instrument: Instrument) -> dict:
        response = self.ticker_get(
            KRAKEN_TICKER_URL,
            params={"pair": KRAKEN_PAIRS[instrument]},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data["error"]:
            raise RuntimeError(f"Kraken ticker error: {data['error']}")
        ticker = next(iter(data["result"].values()))
        price = Decimal(ticker["c"][0])
        bid = Decimal(ticker["b"][0])
        ask = Decimal(ticker["a"][0])
        if (not price.is_finite() or not bid.is_finite() or not ask.is_finite()
            or price <= 0 or bid <= 0 or ask < bid):
            raise ValueError("Kraken ticker returned invalid bid/ask prices")
        return {
            "instrument": instrument.value,
            "price": str(price),
            "bid": str(bid),
            "ask": str(ask),
            "spread_usd_per_btc": str(ask - bid),
            "source": "kraken_spot_last_trade",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _check_limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY:
            raise ValueError(f"limit must be an integer between 1 and {MAX_HISTORY}")

    def get_indicators(
        self, instrument: Instrument, timeframe: Timeframe, limit: int
    ) -> dict:
        self._check_limit(limit)
        with SessionLocal() as session:
            source = CandleRepository(session).get_latest(
                instrument, timeframe, limit + max(MA_PERIODS) - 1
            )
        history = calculate_indicator_history(source, limit)
        return {
            "instrument": instrument.value,
            "timeframe": timeframe.value,
            "count": len(history),
            "market_structure": calculate_market_structure(source),
            "indicators": [
                snapshot.model_dump(mode="json", exclude={"instrument", "timeframe"})
                for snapshot in history
            ],
        }

    def get_candles(
        self, instrument: Instrument, timeframe: Timeframe, limit: int
    ) -> dict:
        self._check_limit(limit)
        with SessionLocal() as session:
            candles = CandleRepository(session).get_latest(instrument, timeframe, limit)
        return {
            "instrument": instrument.value,
            "timeframe": timeframe.value,
            "count": len(candles),
            "candles": [
                candle.model_dump(mode="json", exclude={"instrument", "timeframe"})
                for candle in reversed(candles)
            ],
        }

    def get_paper_account(self) -> dict:
        with SessionLocal() as session:
            return PaperTradingService(session).account_snapshot()

    def get_open_positions(self) -> dict:
        with SessionLocal() as session:
            return {"positions": PaperTradingService(session).open_positions_snapshot()}

    def call(self, name: str, arguments: str) -> str:
        params = json.loads(arguments)
        if name == "get_risk_reward":
            side = PaperSide(params["side"])
            try:
                entry = Decimal(params["entry_execution_price"])
                stop = Decimal(params["stop_execution_price"])
                target = Decimal(params["target_execution_price"])
            except InvalidOperation as exc:
                raise ValueError("Risk/reward prices must be decimal strings") from exc
            values = calculate_risk_reward(side, entry, stop, target, Decimal("1"), Decimal("0"), Decimal("0"))
            result = {"execution_model": "CFD_FIXED_20_USD", "quantity_btc": "1",
                      "risk_net_usd": str(values["risk_net_usd"]),
                      "reward_net_usd": str(values["reward_net_usd"]),
                      "rr_ratio": str(values["rr_ratio"]) if values["rr_ratio"] is not None else None,
                      "min_rr": str(MIN_NET_RISK_REWARD),
                      "passes": values["rr_ratio"] is not None and values["rr_ratio"] >= MIN_NET_RISK_REWARD}
        elif name == "get_paper_account":
            result = self.get_paper_account()
        elif name == "get_open_positions":
            result = self.get_open_positions()
        elif name == "get_price":
            instrument = Instrument(params["instrument"])
            result = self.get_price(instrument)
        elif name == "get_indicators":
            instrument = Instrument(params["instrument"])
            result = self.get_indicators(
                instrument, Timeframe(params["timeframe"]), params["limit"]
            )
        elif name == "get_candles":
            instrument = Instrument(params["instrument"])
            result = self.get_candles(
                instrument, Timeframe(params["timeframe"]), params["limit"]
            )
        else:
            raise ValueError(f"Unknown tool: {name}")
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
