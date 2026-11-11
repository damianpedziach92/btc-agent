import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents.market_tools import MarketTools
from database.models.candle import CandleORM
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository


START = datetime(2026, 11, 10, tzinfo=timezone.utc)


class AgentDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        CandleORM.__table__.create(cls.engine)
        cls.sessions = sessionmaker(bind=cls.engine)
        with cls.sessions.begin() as session:
            session.add_all(
                CandleORM(
                    symbol=Instrument.BTC_USD,
                    timeframe=Timeframe.ONE_MINUTE,
                    timestamp=START + timedelta(minutes=index),
                    open=Decimal(index + 1),
                    high=Decimal(index + 1),
                    low=Decimal(index + 1),
                    close=Decimal(index + 1),
                    vwap=Decimal(index + 1),
                    volume=Decimal(1),
                    trades=1,
                )
                for index in range(205)
            )

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def test_repository_is_descending_and_agent_candles_are_chronological(self):
        with self.sessions() as session:
            latest = CandleRepository(session).get_latest(
                Instrument.BTC_USD, Timeframe.ONE_MINUTE, 5
            )
        self.assertEqual([candle.close for candle in latest], [
            Decimal(205), Decimal(204), Decimal(203), Decimal(202), Decimal(201)
        ])

        with patch("agents.market_tools.SessionLocal", self.sessions):
            result = MarketTools().get_candles(
                Instrument.BTC_USD, Timeframe.ONE_MINUTE, 5
            )
        self.assertEqual(result["count"], 5)
        self.assertEqual([Decimal(candle["close"]) for candle in result["candles"]], [
            Decimal(201), Decimal(202), Decimal(203), Decimal(204), Decimal(205)
        ])

    def test_indicator_history_uses_earlier_candles_for_each_point(self):
        with patch("agents.market_tools.SessionLocal", self.sessions):
            result = MarketTools().get_indicators(
                Instrument.BTC_USD, Timeframe.ONE_MINUTE, 5
            )
        self.assertEqual(result["count"], 5)
        history = result["indicators"]
        self.assertEqual([Decimal(item["ma200"]) for item in history], [
            Decimal("101.5"), Decimal("102.5"), Decimal("103.5"),
            Decimal("104.5"), Decimal("105.5"),
        ])
        self.assertEqual(Decimal(history[-1]["bb_middle"]), Decimal("195.5"))
        deviation = Decimal("33.25").sqrt()
        self.assertEqual(
            Decimal(history[-1]["bb_upper"]), Decimal("195.5") + 2 * deviation
        )
        self.assertEqual(
            Decimal(history[-1]["bb_lower"]), Decimal("195.5") - 2 * deviation
        )


if __name__ == "__main__":
    unittest.main()
