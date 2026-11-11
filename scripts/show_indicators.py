from database.connection import SessionLocal
from models.market import Instrument, Timeframe
from services.indicators import update_indicators


def main() -> None:
    with SessionLocal.begin() as session:
        snapshots = update_indicators(session, Instrument.BTC_USD)

    by_timeframe = {snapshot.timeframe: snapshot for snapshot in snapshots}
    for timeframe in Timeframe:
        snapshot = by_timeframe.get(timeframe)
        if snapshot is None:
            print(f"{Instrument.BTC_USD.value} {timeframe.value} | brak świec")
            continue
        print(f"{snapshot.instrument.value} {snapshot.timeframe.value} | świeca {snapshot.timestamp.isoformat()}")
        for name in ("ma50", "ma100", "ma200", "bb_middle", "bb_upper", "bb_lower"):
            value = getattr(snapshot, name)
            print(f"  {name}: {value if value is not None else 'brak wystarczającej historii'}")


if __name__ == "__main__":
    main()
