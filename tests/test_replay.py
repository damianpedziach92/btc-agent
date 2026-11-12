from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.connection import engine
from database.models.candle import CandleORM
from database.models.paper import PaperAccountLedgerORM
from models.market import Instrument, Timeframe
from models.paper import RiskConfig
from services.replay import Replay


def test_replay_uses_closed_context_and_never_writes_ledger():
    at = datetime(2045, 1, 1, 12, tzinfo=timezone.utc)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False)
    try:
        for index in range(22):
            timestamp = at - timedelta(minutes=(22-index)*15)
            price = Decimal("73000") + index
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.FIFTEEN_MINUTES,
                timestamp=timestamp, open=price, high=price+Decimal("20"),
                low=price-Decimal("20"), close=price, vwap=price,
                volume=Decimal("1"), trades=1))
        for index, price in enumerate(("72980", "73000", "73020")):
            timestamp = at - timedelta(hours=3-index)
            value = Decimal(price)
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.ONE_HOUR,
                timestamp=timestamp, open=value, high=value+20, low=value-20,
                close=value, vwap=value, volume=Decimal("1"), trades=1))
        for index in range(3):
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.ONE_MINUTE,
                timestamp=at + timedelta(minutes=index), open=Decimal("73000"),
                high=Decimal("73001"), low=Decimal("72999"), close=Decimal("73000"),
                vwap=Decimal("73000"), volume=Decimal("1"), trades=1))
        session.flush()
        before = session.scalar(select(func.count()).select_from(PaperAccountLedgerORM))
        replay = Replay(session, at, at + timedelta(minutes=3), risk=RiskConfig())
        outcome = replay.run("trend", "test", at, at + timedelta(minutes=3))
        comparison = replay.compare()
        after = session.scalar(select(func.count()).select_from(PaperAccountLedgerORM))
        assert before == after
        assert outcome["trade_count"] == 1
        assert Decimal(outcome["gross_pnl"]) == 0
        assert Decimal(outcome["net_pnl"]) == -Decimal(outcome["spread_cost_usd"])
        assert outcome["llm_calls"] == 0
        assert {run["strategy"] for run in comparison["runs"]} >= {
            "M5_PULLBACK_RETEST", "M5_RANGE_REJECTION", "M5_BREAKOUT_RETEST"}
        assert all(run["pending_sent"] >= run["pending_filled"] for run in comparison["runs"])
    finally:
        session.close()
        transaction.rollback()
        connection.close()
