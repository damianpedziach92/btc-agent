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
        candle(session, Timeframe.FIFTEEN_MINUTES, at.replace(hour=9, minute=45, second=0), "98")
        candle(session, Timeframe.FIFTEEN_MINUTES, at.replace(minute=0, second=0), "102")
        candle(session, Timeframe.ONE_HOUR, at.replace(hour=9, minute=0, second=0), "97")
        session.flush()
        context = build_review_context(session, END)
    item = context["decisions"][0]
    assert item["id"] == str(row.id)
    assert [c["close"] for c in item["known_at_decision"]["1m"]["recent_closed_candles"]] == ["100.0000000000"]
    assert [c["close"] for c in item["known_at_decision"]["15m"]["recent_closed_candles"]] == ["98.0000000000"]
    assert item["observed_after_decision"]["next_closed_15m"]["close"] == "102.0000000000"
    assert item["known_at_decision"]["1m"]["indicators_at_last_close"]["ma50"] is None


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
        "findings": [{
            "decision_id": str(decision_id), "category": "DATA_CLAIM", "severity": "MEDIUM",
            "issue": "Niepoparta teza <trend>", "evidence": "Ostatnia zamknięta świeca 1m: 100",
            "lesson": "Odnosić opis do świec zamkniętych przed decyzją",
        }],
        "data_gaps": [],
    }


def test_reviewer_uses_structured_output_and_rejects_unknown_decision():
    decision_id = uuid4()
    responses = FakeResponses(review_payload(decision_id))
    reviewer = DecisionReviewer(SimpleNamespace(responses=responses), "test-model")
    context = {"decisions": [{"id": str(decision_id)}]}
    result = reviewer.analyze(context)
    assert result.findings[0].decision_id == decision_id
    assert responses.requests[0]["text"]["format"]["strict"] is True
    assert "tools" not in responses.requests[0]
    assert reviewer.last_model == "review-model"
    with pytest.raises(RuntimeError, match="outside the audited window"):
        reviewer.analyze({"decisions": [{"id": str(uuid4())}]})


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
