
from sqlalchemy import text

from database.connection import engine
from models.market import Instrument, Timeframe


def check_gaps():
    query = text("""
        WITH bounds AS (
            SELECT
                MIN(timestamp) AS first_candle,
                MAX(timestamp) AS last_candle
            FROM candles
            WHERE symbol = :symbol
              AND timeframe = :timeframe
        )
        SELECT expected.timestamp
        FROM bounds
        CROSS JOIN LATERAL generate_series(
            bounds.first_candle,
            bounds.last_candle,
            INTERVAL '1 minute'
        ) AS expected(timestamp)
        LEFT JOIN candles c
            ON c.timestamp = expected.timestamp
           AND c.symbol = :symbol
           AND c.timeframe = :timeframe
        WHERE c.id IS NULL
        ORDER BY expected.timestamp
    """)

    with engine.connect() as connection:
        gaps = connection.execute(
            query,
            {
                "symbol": Instrument.BTC_USD.value,
                "timeframe": Timeframe.ONE_MINUTE.value,
            }
        ).scalars().all()

    print(f"Wykryto {len(gaps)} brakujących minut")

    for gap in gaps[:20]:
        print(gap)


if __name__ == "__main__":
    check_gaps()
