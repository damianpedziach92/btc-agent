"""M5 scheduling and closed-candle gate for the PAPER analyst."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import scheduler
from services.market_analysis import run_market_analysis


def test_scheduler_has_twelve_m5_analyses_per_hour(monkeypatch):
    jobs = []

    class FakeScheduler:
        def __init__(self, **kwargs):
            pass

        def add_job(self, function, **kwargs):
            jobs.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "collect_market_data", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    scheduler.main()
    analyze = next(job for job in jobs if job["id"] == "analyze_btc_usd")
    assert analyze["max_instances"] == 1 and analyze["coalesce"] is True
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    fire_times = []
    previous = None
    for _ in range(12):
        next_at = analyze["trigger"].get_next_fire_time(previous, now)
        fire_times.append(next_at)
        previous, now = next_at, next_at + timedelta(seconds=1)
    assert [value.minute for value in fire_times] == list(range(0, 60, 5))
    assert all(value.second == 25 for value in fire_times)


def test_future_m5_is_not_analyzed_even_with_force():
    with (patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
          patch("services.market_analysis.SessionLocal"),
          patch("services.market_analysis.CandleRepository") as candles,
          patch("services.market_analysis.OpenAI") as openai):
        candles.return_value.get_latest.return_value = [SimpleNamespace(
            timestamp=datetime.now(timezone.utc) + timedelta(minutes=1))]
        assert run_market_analysis(force=True) is None
        openai.assert_not_called()
