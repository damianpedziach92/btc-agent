import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from agents.market_analyst import MarketAnalyst, _fit_tool_output
from agents.market_tools import MarketTools, TOOL_DEFINITIONS
from models.agent_decision import AGENT_DECISION_SCHEMA, AgentDecision, Decision
from models.market import Instrument, Timeframe

DECISION_JSON = '{"decision":"WAIT","open_proposal":null,"position_actions":[],"to_monitor":[],"text":"Brak potwierdzenia."}'


class FakeResponses:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            call = SimpleNamespace(
                type="function_call",
                name="get_price",
                arguments='{"instrument":"BTC/USD"}',
                call_id="call_1",
            )
            return SimpleNamespace(output=[call], output_text="")
        return SimpleNamespace(output=[], output_text=DECISION_JSON)


class FakeTools:
    def call(self, name, arguments):
        assert name == "get_price"
        assert json.loads(arguments) == {"instrument": "BTC/USD"}
        return '{"price":"74000"}'


class CountingTools:
    def __init__(self):
        self.calls = 0

    def call(self, name, arguments):
        self.calls += 1
        return '{"price":"74000"}'


class BatchResponses:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            calls = [SimpleNamespace(
                type="function_call",
                name="get_price",
                arguments='{"instrument":"BTC/USD"}',
                call_id=f"call_{index}",
            ) for index in range(11)]
            return SimpleNamespace(output=calls, output_text="")
        return SimpleNamespace(output=[], output_text=DECISION_JSON)


class OversizeTools:
    def call(self, name, arguments):
        return "x" * 50_001


class MarketAgentTests(unittest.TestCase):
    def test_agent_passes_tool_result_back_to_model(self):
        responses = FakeResponses()
        client = SimpleNamespace(responses=responses)
        analyst = MarketAnalyst(client, FakeTools(), "test-model")
        report = analyst.analyze()
        self.assertEqual(report.decision, Decision.WAIT)
        self.assertEqual(report.text, "Brak potwierdzenia.")
        self.assertEqual(responses.requests[0]["text"]["format"]["schema"], AGENT_DECISION_SCHEMA)
        second_input = responses.requests[1]["input"]
        self.assertEqual(second_input[-1], {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"price":"74000"}',
        })
        self.assertEqual({tool["name"] for tool in TOOL_DEFINITIONS}, {
            "get_price", "get_indicators", "get_candles", "get_paper_account", "get_open_positions", "get_risk_reward"
        })
        self.assertEqual(analyst.tool_trace[0]["name"], "get_price")
        self.assertEqual(analyst.tool_trace[0]["result"]["price"], "74000")

    def test_ticker_and_parameter_validation(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"error": [], "result": {"XXBTZUSD": {
                "c": ["74000.25", "1"], "b": ["74000.20", "1"], "a": ["74000.30", "1"],
            }}},
        )
        tools = MarketTools(ticker_get=lambda *args, **kwargs: response)
        price = tools.get_price(Instrument.BTC_USD)
        self.assertEqual(price["price"], "74000.25")
        self.assertEqual(price["bid"], "74000.20")
        self.assertEqual(price["ask"], "74000.30")
        self.assertEqual(price["spread_usd_per_btc"], "0.10")
        self.assertEqual(price["source"], "kraken_spot_last_trade")
        with self.assertRaises(ValueError):
            tools.get_candles(Instrument.BTC_USD, Timeframe.ONE_HOUR, 51)
        with self.assertRaises(ValueError):
            tools.get_indicators(Instrument.BTC_USD, Timeframe.ONE_HOUR, 0)

    def test_agent_does_not_send_oversize_tool_output_to_model(self):
        responses = FakeResponses()
        client = SimpleNamespace(responses=responses)
        MarketAnalyst(client, OversizeTools(), "test-model").analyze()
        returned = responses.requests[1]["input"][-1]["output"]
        self.assertIn("use the available data", returned)
        self.assertLess(len(returned), 200)

    def test_large_history_keeps_newest_points_in_order(self):
        payload = {
            "count": 5,
            "indicators": [
                {"timestamp": str(index), "ma50": "7" * 100}
                for index in range(5)
            ],
        }
        fitted = json.loads(_fit_tool_output(json.dumps(payload), 350))
        self.assertTrue(fitted["truncated"])
        self.assertEqual(fitted["count"], len(fitted["indicators"]))
        self.assertGreater(len(fitted["indicators"]), 0)
        self.assertEqual(fitted["indicators"][-1]["timestamp"], "4")
        self.assertEqual(
            [item["timestamp"] for item in fitted["indicators"]],
            sorted(item["timestamp"] for item in fitted["indicators"]),
        )

    def test_batch_over_call_limit_finishes_with_available_data(self):
        responses = BatchResponses()
        tools = CountingTools()
        report = MarketAnalyst(
            SimpleNamespace(responses=responses), tools, "test-model"
        ).analyze()
        self.assertEqual(report.decision, Decision.WAIT)
        self.assertEqual(tools.calls, 1)  # identical requests use the cached result
        self.assertEqual(responses.requests[1]["tool_choice"], "none")
        self.assertIn(
            "Tool budget reached",
            responses.requests[1]["input"][-1]["output"],
        )

    def test_all_decisions_validate_and_no_trading_tool_exists(self):
        for value in ("BUY", "SELL", "WAIT"):
            payload = json.loads(DECISION_JSON)
            payload["decision"] = value
            if value != "WAIT":
                payload["open_proposal"] = {
                    "side": "LONG" if value == "BUY" else "SHORT",
                    "reason": "Test", "stop_loss": 72000, "take_profit": 76000,
                    "valid_until": "2026-11-11T15:00:00Z",
                }
            result = AgentDecision.model_validate(payload)
            self.assertEqual(result.decision.value, value)
        self.assertEqual({tool["name"] for tool in TOOL_DEFINITIONS}, {
            "get_price", "get_indicators", "get_candles", "get_paper_account", "get_open_positions", "get_risk_reward"
        })

    def test_invalid_contract_is_rejected(self):
        base = {"decision": "WAIT", "to_monitor": [], "text": "Czekaj"}
        invalid = [
            {**base, "decision": "HOLD"},
            {**base, "extra": 1},
            {**base, "to_monitor": [{"id": "abc", "text": "x", "status": "EXPIRED", "expires_at": "2026-11-12T00:00:00Z"}]},
            {**base, "to_monitor": [{"id": "abc", "text": "x", "status": "ACTIVE", "expires_at": "2026-11-12T00:00:00"}]},
            {**base, "to_monitor": [{"id": "abc", "text": "x", "status": "ACTIVE", "expires_at": "invalid"}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                AgentDecision.model_validate(payload)

    def test_multiple_calls_in_one_round(self):
        class TwoCalls:
            requests = []
            def create(self, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    return SimpleNamespace(output=[
                        SimpleNamespace(type="function_call", name="get_price", arguments='{"instrument":"BTC/USD"}', call_id=f"call_{i}")
                        for i in range(2)
                    ], output_text="")
                return SimpleNamespace(output=[], output_text=DECISION_JSON)

        responses = TwoCalls()
        MarketAnalyst(SimpleNamespace(responses=responses), CountingTools(), "test-model").analyze()
        self.assertEqual(
            [item["call_id"] for item in responses.requests[1]["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"],
            ["call_0", "call_1"],
        )

    def test_refusal_incomplete_and_invalid_output_rejected(self):
        for response in (
            SimpleNamespace(status="incomplete", output=[], output_text=DECISION_JSON),
            SimpleNamespace(status="completed", output=[SimpleNamespace(type="refusal")], output_text=""),
            SimpleNamespace(status="completed", output=[], output_text=""),
            SimpleNamespace(status="completed", output=[], output_text='{"decision":"NO_TRADE","to_monitor":[],"text":"x"}'),
        ):
            client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
            with self.subTest(response=response), self.assertRaises((RuntimeError, ValidationError)):
                MarketAnalyst(client, FakeTools(), "test-model").analyze()

    def test_invalid_structured_answer_gets_one_tool_free_repair(self):
        class RepairResponses:
            def __init__(self):
                self.requests = []

            def create(self, **kwargs):
                self.requests.append(kwargs)
                if len(self.requests) == 1:
                    return SimpleNamespace(output=[], output_text=json.dumps({
                        "decision": "BUY", "open_proposal": None,
                        "position_actions": [], "pending_order_actions": [],
                        "to_monitor": [], "text": "Niespójna decyzja"}))
                return SimpleNamespace(output=[], output_text=DECISION_JSON)

        responses = RepairResponses()
        analyst = MarketAnalyst(SimpleNamespace(responses=responses), FakeTools(), "test-model")
        result = analyst.analyze()
        self.assertEqual(result.decision, Decision.WAIT)
        self.assertEqual(analyst.validation_repair_count, 1)
        self.assertEqual(len(responses.requests), 2)
        self.assertEqual(responses.requests[1]["tool_choice"], "none")
        self.assertIn("open proposal", responses.requests[1]["input"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
