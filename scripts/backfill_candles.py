from datetime import timedelta, timezone

from database.connection import SessionLocal
from models.market import Instrument
from repositories.candle_repository import CandleRepository
from services.candle_aggregation import AGGREGATE_TIMEFRAMES, aggregate_day
from services.indicators import update_indicators


def main() -> None:
    instrument = Instrument.BTC_USD
    with SessionLocal() as session:
        bounds = CandleRepository(session).get_one_minute_bounds(instrument)

    if bounds is None:
        print("Brak świec 1m do agregacji")
        return

    day = bounds[0].astimezone(timezone.utc).date()
    last_day = bounds[1].astimezone(timezone.utc).date()
    totals = {timeframe: 0 for timeframe in AGGREGATE_TIMEFRAMES}

    while day <= last_day:
        with SessionLocal.begin() as session:
            counts = aggregate_day(session, instrument, day)
        for timeframe, count in counts.items():
            totals[timeframe] += count
        day += timedelta(days=1)

    for timeframe in AGGREGATE_TIMEFRAMES:
        print(f"{timeframe.value}: zapisano {totals[timeframe]} nowych świec")

    with SessionLocal.begin() as session:
        snapshots = update_indicators(session, instrument)
    print(f"Zaktualizowano {len(snapshots)} zestawów wskaźników")


if __name__ == "__main__":
    main()
