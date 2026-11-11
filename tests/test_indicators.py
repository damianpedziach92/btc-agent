import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models.candle import Candle
from models.market import Instrument, Timeframe
from services.indicators import calculate_indicator_history, calculate_indicators


START = datetime(2026, 11, 10, tzinfo=timezone.utc)


def candle(
    index: int, close: Decimal, timeframe: Timeframe = Timeframe.ONE_MINUTE
) -> Candle:
    return Candle(
        instrument=Instrument.BTC_USD,
        timeframe=timeframe,
        timestamp=START + timedelta(minutes=index * timeframe.minutes),
        open=close,
        high=close,
        low=close,
        close=close,
        vwap=close,
        volume=Decimal(1),
        trades=1,
    )


class IndicatorTests(unittest.TestCase):
    def test_ma_and_bollinger_use_close_and_population_deviation(self):
        source = [candle(index, Decimal(index + 1)) for index in range(200)]
        snapshot = calculate_indicators(source)
        self.assertEqual(snapshot.timestamp, source[-1].timestamp)
        self.assertEqual(snapshot.ma50, Decimal("175.5"))
        self.assertEqual(snapshot.ma100, Decimal("150.5"))
        self.assertEqual(snapshot.ma200, Decimal("100.5"))
        self.assertEqual(snapshot.bb_middle, Decimal("190.5"))
        deviation = Decimal("33.25").sqrt()
        self.assertEqual(snapshot.bb_upper, Decimal("190.5") + 2 * deviation)
        self.assertEqual(snapshot.bb_lower, Decimal("190.5") - 2 * deviation)

    def test_short_history_and_gap_leave_unavailable_values_empty(self):
        source = [candle(index, Decimal(100)) for index in range(200) if index != 150]
        snapshot = calculate_indicators(source)
        self.assertIsNone(snapshot.ma50)
        self.assertIsNone(snapshot.ma100)
        self.assertIsNone(snapshot.ma200)
        self.assertEqual(snapshot.bb_middle, Decimal(100))
        self.assertEqual(snapshot.bb_upper, Decimal(100))
        self.assertEqual(snapshot.bb_lower, Decimal(100))

        too_short = calculate_indicators(source[-19:])
        self.assertIsNone(too_short.bb_middle)
        self.assertIsNone(too_short.bb_upper)
        self.assertIsNone(too_short.bb_lower)

    def test_empty_source_returns_no_snapshot(self):
        self.assertIsNone(calculate_indicators([]))

    def test_each_timeframe_uses_its_own_candle_spacing(self):
        for timeframe in Timeframe:
            with self.subTest(timeframe=timeframe):
                source = [candle(index, Decimal(100), timeframe) for index in range(200)]
                snapshot = calculate_indicators(source)
                self.assertEqual(snapshot.timeframe, timeframe)
                self.assertEqual(snapshot.ma200, Decimal(100))
                self.assertEqual(snapshot.bb_middle, Decimal(100))

    def test_history_uses_warmup_without_future_candles(self):
        source = [candle(index, Decimal(100)) for index in range(205)]
        source[-1] = candle(204, Decimal(200))
        history = calculate_indicator_history(source, 6)
        self.assertEqual(len(history), 6)
        self.assertEqual(history[0].timestamp, source[199].timestamp)
        self.assertEqual(history[0].ma200, Decimal(100))
        self.assertEqual(history[0].bb_middle, Decimal(100))
        self.assertEqual(history[-1].ma200, Decimal("100.5"))
        self.assertEqual(history[-1].bb_middle, Decimal(105))

    def test_history_resets_moving_averages_after_gap(self):
        source = [candle(index, Decimal(100)) for index in range(220) if index != 150]
        history = calculate_indicator_history(source, 2)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1].ma50, Decimal(100))
        self.assertIsNone(history[-1].ma100)
        self.assertIsNone(history[-1].ma200)


if __name__ == "__main__":
    unittest.main()
