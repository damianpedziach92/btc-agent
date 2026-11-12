import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models.candle import Candle
from models.market import Instrument, Timeframe
from services.candle_aggregation import aggregate_candles


START = datetime(2026, 11, 10, tzinfo=timezone.utc)


def minute_candle(index: int) -> Candle:
    price = Decimal(100 + index)
    return Candle(
        instrument=Instrument.BTC_USD,
        timeframe=Timeframe.ONE_MINUTE,
        timestamp=START + timedelta(minutes=index),
        open=price,
        high=price + 2,
        low=price - 2,
        close=price + 1,
        vwap=price,
        volume=Decimal(index % 3 + 1),
        trades=index % 5 + 1,
    )


class CandleAggregationTests(unittest.TestCase):
    def test_full_utc_day_produces_all_timeframes(self):
        source = [minute_candle(index) for index in range(1440)]
        expected_counts = {
            Timeframe.FIVE_MINUTES: 288,
            Timeframe.FIFTEEN_MINUTES: 96,
            Timeframe.ONE_HOUR: 24,
            Timeframe.FOUR_HOURS: 6,
            Timeframe.ONE_DAY: 1,
        }

        for timeframe, count in expected_counts.items():
            with self.subTest(timeframe=timeframe):
                result = aggregate_candles(source, timeframe)
                self.assertEqual(len(result), count)
                self.assertEqual(result[0].timestamp, START)
                self.assertEqual(result[0].timeframe, timeframe)
                self.assertEqual(result[0].open, source[0].open)
                self.assertEqual(result[0].close, source[timeframe.minutes - 1].close)
                self.assertEqual(result[0].high, source[timeframe.minutes - 1].high)
                self.assertEqual(result[0].low, source[0].low)
                self.assertEqual(result[0].volume, sum(
                    candle.volume for candle in source[:timeframe.minutes]
                ))
                self.assertEqual(result[0].trades, sum(
                    candle.trades for candle in source[:timeframe.minutes]
                ))
                self.assertEqual(result[0].vwap, sum(
                    candle.vwap * candle.volume
                    for candle in source[:timeframe.minutes]
                ) / result[0].volume)

    def test_incomplete_and_gapped_windows_are_skipped(self):
        source = [minute_candle(index) for index in range(45) if index != 20]
        result = aggregate_candles(source, Timeframe.FIFTEEN_MINUTES)
        self.assertEqual([candle.timestamp for candle in result], [
            START,
            START + timedelta(minutes=30),
        ])
        self.assertEqual(aggregate_candles(source, Timeframe.ONE_HOUR), [])

    def test_five_minute_candle_waits_for_last_minute_and_skips_gaps(self):
        source = [minute_candle(index) for index in range(15) if index != 7]
        result = aggregate_candles(source, Timeframe.FIVE_MINUTES)
        self.assertEqual([candle.timestamp for candle in result], [
            START, START + timedelta(minutes=10),
        ])

    def test_local_timezone_does_not_shift_utc_boundaries(self):
        source = [minute_candle(index) for index in range(1440, 1455)]
        source = [candle.model_copy(update={
            "timestamp": candle.timestamp.astimezone(timezone(timedelta(hours=1)))
        }) for candle in source]
        result = aggregate_candles(source, Timeframe.FIFTEEN_MINUTES)
        self.assertEqual(result[0].timestamp, START + timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
