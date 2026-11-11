from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models.market_analysis import MarketAnalysisORM
from models.market import Instrument


class MarketAnalysisRepository:
    def __init__(self, session: Session):
        self.session = session

    def exists(self, instrument: Instrument, candle_timestamp: datetime) -> bool:
        return self.session.scalar(
            select(MarketAnalysisORM.id).where(
                MarketAnalysisORM.symbol == instrument,
                MarketAnalysisORM.candle_timestamp == candle_timestamp,
            )
        ) is not None

    def save(
        self, instrument: Instrument, candle_timestamp: datetime, model: str, analysis: str
    ) -> bool:
        statement = insert(MarketAnalysisORM).values(
            symbol=instrument,
            candle_timestamp=candle_timestamp,
            created_at=datetime.now(timezone.utc),
            model=model,
            analysis=analysis,
        ).on_conflict_do_nothing(
            constraint="uq_market_analyses_symbol_candle"
        ).returning(MarketAnalysisORM.id)
        return self.session.scalar(statement) is not None

    def get_latest(self, instrument: Instrument, limit: int = 10) -> list[MarketAnalysisORM]:
        return list(self.session.scalars(
            select(MarketAnalysisORM).where(
                MarketAnalysisORM.symbol == instrument
            ).order_by(MarketAnalysisORM.created_at.desc()).limit(limit)
        ))
