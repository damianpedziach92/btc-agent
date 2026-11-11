from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models.indicator_snapshot import IndicatorSnapshotORM
from models.indicators import IndicatorSnapshot
from models.market import Instrument, Timeframe


class IndicatorRepository:
    def __init__(self, session: Session):
        self.session = session

    def save(self, snapshot: IndicatorSnapshot) -> None:
        values = {"symbol": snapshot.instrument, **snapshot.model_dump(exclude={"instrument"})}
        statement = insert(IndicatorSnapshotORM).values(values)
        statement = statement.on_conflict_do_update(
            index_elements=[IndicatorSnapshotORM.symbol, IndicatorSnapshotORM.timeframe],
            set_={
                column: getattr(statement.excluded, column)
                for column in ("timestamp", "ma50", "ma100", "ma200", "bb_middle", "bb_upper", "bb_lower")
            },
            where=statement.excluded.timestamp >= IndicatorSnapshotORM.timestamp,
        )
        self.session.execute(statement)

    def get(self, instrument: Instrument, timeframe: Timeframe) -> IndicatorSnapshot | None:
        row = self.session.get(IndicatorSnapshotORM, (instrument, timeframe))
        if row is None:
            return None
        return IndicatorSnapshot(
            instrument=row.symbol,
            timeframe=row.timeframe,
            timestamp=row.timestamp,
            ma50=row.ma50,
            ma100=row.ma100,
            ma200=row.ma200,
            bb_middle=row.bb_middle,
            bb_upper=row.bb_upper,
            bb_lower=row.bb_lower,
        )
