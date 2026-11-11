
from datetime import datetime, timezone

import httpx

from models.candle import Candle
from models.market import Instrument, Timeframe

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"


def parse_candle(raw: list, instrument: Instrument, timeframe: Timeframe) -> Candle:
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        timestamp=datetime.fromtimestamp(
            raw[0], tz=timezone.utc
        ),
        open=raw[1],
        high=raw[2],
        low=raw[3],
        close=raw[4],
        vwap=raw[5],
        volume=raw[6],
        trades=raw[7],
    )


def get_candles() -> list[Candle]:
    instrument = Instrument.BTC_USD
    timeframe = Timeframe.ONE_MINUTE

    params = {
        "pair": "XBTUSD",
        "interval": 1,
    }

    response = httpx.get(
        KRAKEN_URL,
        params=params,
        timeout=10,
    )

    response.raise_for_status()
    data = response.json()

    if data["error"]:
        raise RuntimeError(data["error"])

    pair = next(
        key for key in data["result"]
        if key != "last"
    )

    raw_candles = data["result"][pair]

    return [
        parse_candle(raw, instrument, timeframe)
        for raw in raw_candles[:-1]
    ]


if __name__ == "__main__":
    candles = get_candles()

    print(f"Pobrano {len(candles)} świec")

    last_candle = candles[-1]

    print(f"Data: {last_candle.timestamp}")
    print(f"Open: {last_candle.open}")
    print(f"High: {last_candle.high}")
    print(f"Low: {last_candle.low}")
    print(f"Close: {last_candle.close}")
    print(f"Volume: {last_candle.volume}")
