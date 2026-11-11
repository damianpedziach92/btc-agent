
from datetime import timezone

from main import get_candles

from database.connection import SessionLocal
from repositories.candle_repository import CandleRepository
from services.candle_aggregation import aggregate_day
from services.indicators import update_indicators


def main():
    print("Pobieram dane z Krakena...")

    candles = get_candles()

    print(f"Pobrano {len(candles)} świec")

    with SessionLocal.begin() as session:
        repository = CandleRepository(session)

        inserted = repository.save_many(candles)
        affected_days = {
            (candle.instrument, candle.timestamp.astimezone(timezone.utc).date())
            for candle in candles
        }
        aggregated = 0
        for instrument, day in affected_days:
            aggregated += sum(aggregate_day(session, instrument, day).values())
        updated = sum(
            len(update_indicators(session, instrument))
            for instrument in {candle.instrument for candle in candles}
        )

    print(f"Zapisano {inserted} nowych świec")
    print(f"Zapisano {aggregated} nowych świec zbiorczych")
    print(f"Zaktualizowano {updated} zestawów wskaźników")


if __name__ == "__main__":
    main()
