import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from models.agent_decision import AgentDecision, Decision
from services.market_analysis import run_market_analysis


class MarketAnalysisPreviewTests(unittest.TestCase):
    def test_preview_reanalyzes_old_candle_without_reading_or_writing_report(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch("services.market_analysis.SessionLocal") as sessions,
            patch("services.market_analysis.CandleRepository") as candles,
            patch("services.market_analysis.AgentMemoryRepository") as memory,
            patch("services.market_analysis.PaperTradingService") as paper,
            patch("services.market_analysis.MarketTools") as tools,
            patch("services.market_analysis.build_market_context") as market_context,
            patch("services.market_analysis.OpenAI") as openai,
            patch("services.market_analysis.MarketAnalyst") as analyst,
            patch("services.market_analysis.process_pending_decisions") as paper_commands,
        ):
            candles.return_value.get_latest.return_value = [SimpleNamespace(
                timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc)
            )]
            expected = AgentDecision(decision=Decision.WAIT, to_monitor=[], text="Nowy wynik testowy")
            analyst.return_value.analyze.return_value = expected
            memory.return_value.load_active.return_value = []
            paper.return_value.account_snapshot.return_value = {"status": "UNSEEDED"}
            paper.return_value.open_positions_snapshot.return_value = []
            paper.return_value.recent_events.return_value = []
            tools.return_value.get_price.return_value = {"price": "74000", "retrieved_at": datetime.now(timezone.utc).isoformat()}
            market_context.return_value = {"sha256": "test"}

            result = run_market_analysis(preview=True)

            self.assertEqual(result, expected)
            memory.return_value.exists.assert_not_called()
            memory.return_value.save.assert_not_called()
            sessions.begin.assert_not_called()
            paper_commands.assert_not_called()
            openai.assert_called_once()
            self.assertEqual(candles.return_value.get_latest.call_args.args[1].value, "5m")

    def test_saved_decision_runs_paper_without_decision_telegram(self):
        decision_id = uuid4()
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch("services.market_analysis.SessionLocal") as sessions,
            patch("services.market_analysis.CandleRepository") as candles,
            patch("services.market_analysis.AgentMemoryRepository") as memory,
            patch("services.market_analysis.PaperTradingService") as paper,
            patch("services.market_analysis.MarketTools") as tools,
            patch("services.market_analysis.build_market_context") as market_context,
            patch("services.market_analysis.OpenAI"),
            patch("services.market_analysis.MarketAnalyst") as analyst,
            patch("services.market_analysis.process_pending_decisions") as paper_commands,
        ):
            candles.return_value.get_latest.return_value = [SimpleNamespace(
                timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc)
            )]
            expected = AgentDecision(decision=Decision.WAIT, to_monitor=[], text="Czekam")
            analyst.return_value.analyze.return_value = expected
            memory.return_value.load_active.return_value = []
            memory.return_value.exists.return_value = False
            memory.return_value.save.return_value = True
            paper.return_value.account_snapshot.return_value = {"status": "ACTIVE"}
            paper.return_value.open_positions_snapshot.return_value = []
            paper.return_value.recent_events.return_value = []
            tools.return_value.get_price.return_value = {"price": "74000", "retrieved_at": datetime.now(timezone.utc).isoformat()}
            market_context.return_value = {"sha256": "test"}
            sessions.begin.return_value.__enter__.return_value.scalar.return_value = decision_id

            self.assertEqual(run_market_analysis(force=True), expected)
            paper_commands.assert_called_once()
            self.assertEqual(memory.return_value.save.call_args.kwargs["analysis_trace"]["trigger_timeframe"], "5m")


if __name__ == "__main__":
    unittest.main()
