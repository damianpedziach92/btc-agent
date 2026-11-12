import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from agents.decision_reviewer import DecisionReviewer
from database.connection import engine
from database.models.agent_decision import AgentDecisionORM
from database.models.candle import CandleORM
from database.models.decision_review import DecisionReviewORM
from database.models.paper import PaperAccountORM, PaperFeeProfileORM, PaperPositionEventORM, PaperPositionORM
from models.market import Instrument, Timeframe
import services.decision_review as decision_review
import services.review_notifications as review_notifications
from services.decision_review import build_review_context, latest_closed_window_end
from services.review_notifications import process_pending_review_notifications
from services.telegram_notifications import TelegramDeliveryError


D = Decimal
END = datetime(2040, 1, 1, 12, tzinfo=timezone.utc)


def candle(session, timeframe, at, close):
    price = D(close)
    session.add(CandleORM(
        symbol=Instrument.BTC_USD, timeframe=timeframe, timestamp=at,
        open=price, high=price + 1, low=price - 1, close=price,
        vwap=price, volume=D("1"), trades=1,
    ))


def decision(session, at):
    row = AgentDecisionORM(
        id=uuid4(), instrument=Instrument.BTC_USD.value,
        candle_timestamp=at - timedelta(minutes=15), created_at=at,
        decision="BUY", text="Świeca 1m potwierdziła wzrost <test>", model="test",
        open_proposal={"side": "LONG", "reason": "impuls", "stop_loss": "99",
                       "take_profit": "105", "valid_until": (at + timedelta(minutes=5)).isoformat()},
        position_actions=[], paper_status="DONE",
        paper_result={"status": "DONE", "results": [{"status": "REJECTED", "reason": "test"}]},
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def isolated_sessions():
    connection = engine.connect()
    transaction = connection.begin()
    sessions = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        yield sessions
    finally:
        transaction.rollback()
        connection.close()


def test_window_boundaries_and_context_exclude_unclosed_candles(isolated_sessions):
    assert latest_closed_window_end(datetime(2040, 1, 1, 13, 59, tzinfo=timezone.utc)) == END
    with isolated_sessions.begin() as session:
        at = END - timedelta(hours=2) + timedelta(minutes=10, seconds=30)
        row = decision(session, at)
        candle(session, Timeframe.ONE_MINUTE, at.replace(minute=9, second=0), "100")
        candle(session, Timeframe.ONE_MINUTE, at.replace(minute=10, second=0), "101")
        candle(session, Timeframe.FIVE_MINUTES, at.replace(minute=5, second=0), "99")
        candle(session, Timeframe.FIVE_MINUTES, at.replace(minute=10, second=0), "103")
        candle(session, Timeframe.FIFTEEN_MINUTES, at.replace(hour=9, minute=45, second=0), "98")
        candle(session, Timeframe.FIFTEEN_MINUTES, at.replace(minute=0, second=0), "102")
        candle(session, Timeframe.ONE_HOUR, at.replace(hour=9, minute=0, second=0), "97")
        session.flush()
        context = build_review_context(session, END)
    item = context["decisions"][0]
    assert item["id"] == str(row.id)
    assert [c["close"] for c in item["known_at_decision"]["1m"]["recent_closed_candles"]] == ["100.0000000000"]
    assert [c["close"] for c in item["known_at_decision"]["5m"]["recent_closed_candles"]] == ["99.0000000000"]
    assert [c["close"] for c in item["known_at_decision"]["15m"]["recent_closed_candles"]] == ["98.0000000000"]
    assert item["observed_after_decision"]["next_closed_15m"]["close"] == "102.0000000000"
    assert item["known_at_decision"]["1m"]["indicators_at_last_close"]["ma50"] is None
    assert context["actions"][0]["type"] == "OPEN_LONG"
    assert context["actions"][0]["execution_result"]["status"] == "REJECTED"


def test_context_links_entry_auto_close_and_trigger_candle(isolated_sessions):
    with isolated_sessions.begin() as session:
        at = END - timedelta(hours=1, minutes=30)
        row = decision(session, at)
        row.paper_result = {"status": "DONE", "results": [{"status": "ACCEPTED", "position_id": None}]}
        account_id, profile_id, position_id = uuid4(), uuid4(), uuid4()
        session.add(PaperAccountORM(
                id=account_id, name=f"review-{uuid4().hex[:20]}", currency="USD",
            initial_balance=D("1000"), cash_balance=D("1000"), status="ACTIVE",
            version=1, created_at=at, updated_at=at,
        ))
        session.flush()
        session.add(PaperFeeProfileORM(
            id=profile_id, account_id=account_id, version=1, market_type="SPOT_MARGIN",
            status="ESTIMATED", source="test", trade_entry_fee_rate=D("0.001"),
            trade_exit_fee_rate=D("0.001"), margin_opening_fee_rate=D("0"),
            rollover_rate_per_4h=D("0"), slippage_bps=D("0"), spread_bps=D("4"),
            created_at=at,
        ))
        session.flush()
        close_at = at + timedelta(minutes=3)
        session.add(PaperPositionORM(
            id=position_id, account_id=account_id, instrument=Instrument.BTC_USD.value,
            market_type="SPOT_MARGIN", side="LONG", status="CLOSED", entry_price=D("100"),
            entry_at=at + timedelta(seconds=1), entry_candle_timestamp=None,
            entry_source="test", execution_key=f"test:{uuid4()}", quantity_btc=D("1"),
            entry_notional=D("100"), leverage=D("1"), reserved_margin=D("100"),
            stop_loss=D("99"), take_profit=D("105"), entry_reason="impuls",
            agent_decision_id=row.id, scenario_key=None, fee_profile_id=profile_id,
            fee_snapshot={}, initial_costs=D("0.1"), accrued_holding_costs=D("0"),
            close_price=D("105"), close_at=close_at,
            close_source="1m_ohlc_barrier_assumption", close_reason="TAKE_PROFIT",
            close_reason_text="", realized_gross_pnl=D("5"), realized_net_pnl=D("4.8"),
            fee_total=D("0.2"), unpaid_loss=D("0"),
            last_evaluated_candle_timestamp=None, last_holding_period=0,
        ))
        session.flush()
        session.add(PaperPositionEventORM(
            id=uuid4(), account_id=account_id, position_id=position_id,
            decision_id=None, type="CLOSED", event_time=close_at,
            payload={"reason": "TAKE_PROFIT", "theoretical_touch_at": close_at.isoformat()},
            dedupe_key=f"test-close:{position_id}",
        ))
        candle(session, Timeframe.ONE_MINUTE, close_at - timedelta(minutes=1), "105")
        session.flush()
        context = build_review_context(session, END)
    assert [action["type"] for action in context["actions"]] == ["OPEN_LONG", "AUTO_CLOSE"]
    assert context["actions"][0]["position_id"] == str(position_id)
    assert context["actions"][1]["trigger_candle"]["close"] == "105.0000000000"
    assert context["positions"][0]["realized_net_pnl"] == "4.800000000000"


class FakeResponses:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(status="completed", output=[], model="review-model",
                               output_text=json.dumps(self.payload))


def review_payload(decision_id):
    return {
        "assessment": "ISSUES_FOUND", "summary": "Jeden błąd opisu.",
        "action_reviews": [{
            "action_id": f"decision:{decision_id}:open", "action_type": "OPEN_LONG",
            "verdict": "ERROR", "reason": "Niepoparta teza", "evidence": "Świeca 1m: 100",
            "lesson": "Sprawdzić dane",
        }],
        "findings": [{
            "decision_id": str(decision_id), "action_id": f"decision:{decision_id}:open",
            "category": "DATA_CLAIM", "severity": "MEDIUM",
            "claim": "Świeca 1m potwierdziła wzrost <test>",
            "issue": "Niepoparta teza <trend>", "evidence": "Ostatnia zamknięta świeca 1m: 100",
            "lesson": "Odnosić opis do świec zamkniętych przed decyzją",
        }],
        "data_gaps": [],
    }


def test_reviewer_uses_structured_output_and_rejects_unknown_decision():
    decision_id = uuid4()
    responses = FakeResponses(review_payload(decision_id))
    reviewer = DecisionReviewer(SimpleNamespace(responses=responses), "test-model")
    context = {"decisions": [{"id": str(decision_id), "text": "Świeca 1m potwierdziła wzrost <test>"}],
               "actions": [{"id": f"decision:{decision_id}:open", "type": "OPEN_LONG", "decision_id": str(decision_id)}]}
    result = reviewer.analyze(context)
    assert result.findings[0].decision_id == decision_id
    assert responses.requests[0]["text"]["format"]["strict"] is True
    assert "tools" not in responses.requests[0]
    assert reviewer.last_model == "review-model"
    other_id = uuid4()
    with pytest.raises(RuntimeError, match="outside the audited window"):
        reviewer.analyze({"decisions": [{"id": str(other_id), "text": "Inna decyzja"}],
                          "actions": [{"id": f"decision:{other_id}:open", "type": "OPEN_LONG", "decision_id": str(other_id)}]})


def test_reviewer_drops_unverifiable_live_quote_claim():
    decision_id = uuid4()
    payload = review_payload(decision_id)
    payload["findings"][0]["claim"] = "kurs poniżej środkowej wstęgi"
    result = DecisionReviewer(SimpleNamespace(responses=FakeResponses(payload)), "test-model").analyze({
        "decisions": [{"id": str(decision_id), "text": "Kontekst 1h: kurs poniżej środkowej wstęgi"}],
        "actions": [{"id": f"decision:{decision_id}:open", "type": "OPEN_LONG", "decision_id": str(decision_id)}],
    })
    assert result.assessment.value == "INSUFFICIENT_DATA"
    assert result.findings == []
    assert result.action_reviews[0].verdict.value == "INSUFFICIENT_DATA"


def test_reviewer_marks_close_without_position_insufficient():
    decision_id, position_id = uuid4(), uuid4()
    action_id = f"decision:{decision_id}:close:{position_id}"
    payload = {
        "assessment": "NO_ISSUES", "summary": "Zamknięcie uzasadnione.",
        "action_reviews": [{"action_id": action_id, "action_type": "CLOSE",
                            "verdict": "JUSTIFIED", "reason": "Sygnał osłabł",
                            "evidence": "Świeca 1m", "lesson": "Sprawdź ryzyko"}],
        "findings": [], "data_gaps": [],
    }
    context = {"decisions": [{"id": str(decision_id), "text": "Zamykam"}],
               "positions": [],
               "actions": [{"id": action_id, "type": "CLOSE", "decision_id": str(decision_id),
                            "position_id": str(position_id)}]}
    result = DecisionReviewer(SimpleNamespace(responses=FakeResponses(payload)), "test-model").analyze(context)
    assert result.assessment.value == "INSUFFICIENT_DATA"
    assert result.action_reviews[0].verdict.value == "INSUFFICIENT_DATA"


def test_review_is_saved_once_and_telegram_retries_without_duplicate(isolated_sessions, monkeypatch):
    monkeypatch.setattr(decision_review, "SessionLocal", isolated_sessions)
    monkeypatch.setattr(review_notifications, "SessionLocal", isolated_sessions)
    monkeypatch.setattr(decision_review.TelegramConfig, "from_env", lambda: object())
    with isolated_sessions.begin() as session:
        at = END - timedelta(hours=2) + timedelta(minutes=10, seconds=30)
        row = decision(session, at)
        candle(session, Timeframe.ONE_MINUTE, at.replace(minute=9, second=0), "100")
    responses = FakeResponses(review_payload(row.id))
    client = SimpleNamespace(responses=responses)
    result = decision_review.run_decision_review(
        window_end=END, now=END + timedelta(minutes=3), client=client,
    )
    assert result["status"] == "CREATED"
    assert result["assessment"] == "ISSUES_FOUND"
    assert result["notification_status"] == "PENDING"
    assert decision_review.run_decision_review(
        window_end=END, now=END + timedelta(minutes=4), client=client,
    )["status"] == "EXISTS"
    assert len(responses.requests) == 1

    class FakeTelegram:
        def __init__(self):
            self.messages = []
            self.fail = True

        def send_message(self, text):
            self.messages.append(text)
            if self.fail:
                raise TelegramDeliveryError("Telegram HTTP 503")

    telegram = FakeTelegram()
    assert process_pending_review_notifications(client=telegram)["failed"] == 1
    telegram.fail = False
    assert process_pending_review_notifications(client=telegram)["sent"] == 1
    assert process_pending_review_notifications(client=telegram)["sent"] == 0
    assert "&lt;trend&gt;" in telegram.messages[-1]
    with isolated_sessions() as session:
        stored = session.get(DecisionReviewORM, result["id"])
        assert stored.notification_status == "SENT"
        assert stored.notification_attempts == 2


def test_empty_window_needs_no_model_call(isolated_sessions, monkeypatch):
    monkeypatch.setattr(decision_review, "SessionLocal", isolated_sessions)
    monkeypatch.setattr(decision_review.TelegramConfig, "from_env", lambda: None)
    result = decision_review.run_decision_review(
        window_end=END + timedelta(days=2), now=END + timedelta(days=2, minutes=3),
    )
    assert result["status"] == "CREATED"
    assert result["assessment"] == "NO_DECISIONS"
    assert result["notification_status"] == "DISABLED"
