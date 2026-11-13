import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agents.market_tools import MarketTools
from database.models.candle import CandleORM
from models.candle import Candle
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository
from services.market_structure import calculate_market_structure


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
            session.add_all(
                CandleORM(
                    symbol=Instrument.BTC_USD,
                    timeframe=Timeframe.FIVE_MINUTES,
                    timestamp=START + timedelta(minutes=index * 5),
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
        structure = result["market_structure"]
        self.assertEqual(Decimal(structure["atr14"]), Decimal("1"))
        self.assertEqual(Decimal(structure["channel_high_20"]), Decimal("204"))
        self.assertEqual(Decimal(structure["channel_low_20"]), Decimal("185"))
        self.assertEqual(Decimal(structure["swing_low"]), Decimal("204"))

    def test_five_minute_candles_and_indicators_are_available_to_agent(self):
        with patch("agents.market_tools.SessionLocal", self.sessions):
            tools = MarketTools()
            candles = tools.get_candles(Instrument.BTC_USD, Timeframe.FIVE_MINUTES, 5)
            indicators = tools.get_indicators(Instrument.BTC_USD, Timeframe.FIVE_MINUTES, 5)
        self.assertEqual([Decimal(item["close"]) for item in candles["candles"]],
                         [Decimal(value) for value in range(201, 206)])
        self.assertEqual([Decimal(item["ma200"]) for item in indicators["indicators"]],
                         [Decimal(value) + Decimal("0.5") for value in range(101, 106)])
        self.assertEqual(indicators["timeframe"], "5m")
        self.assertEqual(Decimal(indicators["market_structure"]["atr14"]), Decimal("1"))

    def test_market_structure_does_not_bridge_missing_candles(self):
        rows = [Candle(
            instrument=Instrument.BTC_USD, timeframe=Timeframe.ONE_MINUTE,
            timestamp=START + timedelta(minutes=minute),
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), vwap=Decimal("100"), volume=Decimal("1"), trades=1,
        ) for minute in (0, 2)]
        structure = calculate_market_structure(rows)
        self.assertEqual(structure["consecutive_count"], 1)
        self.assertIsNone(structure["atr14"])
        self.assertIsNone(structure["channel_high_20"])


if __name__ == "__main__":
    unittest.main()
