import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.error import URLError
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from database.connection import engine
from database.models.agent_decision import AgentDecisionORM
from database.models.agent_monitor_item import AgentMonitorItemORM
from database.models.telegram_notification import TelegramNotificationORM
import services.telegram_notifications as telegram_notifications
from services.telegram_notifications import (
    TelegramBotClient, TelegramConfig, TelegramDeliveryError,
    enqueue_decision_notification, format_decision_notification,
    process_pending_notifications, _message_chunks,
)


def test_telegram_message_contains_decision_account_and_all_positions():
    decision = SimpleNamespace(
        decision="WAIT", created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        text="Czekam na wybicie 73446.9", open_proposal=None,
        position_actions=[{"action": "HOLD", "position_id": str(uuid4()), "reason": "Trend trwa"}],
        paper_status="DONE", paper_result={"status": "DONE", "results": []},
    )
    account = {
        "status": "ACTIVE", "equity_estimate": "1001.23", "cash_balance": "999.00",
        "available_margin": "800", "reserved_margin": "199", "realized_pnl": "1.50",
        "fees_paid": "0.50", "pnl_quality": "ESTIMATED_PAPER_ONLY", "stale_data": False,
    }
    positions = [{
        "side": side, "id": str(uuid4()), "quantity_btc": "0.001", "entry_price": "70000",
        "current_mark_price": "70100", "stop_loss": "69000", "take_profit": None,
        "unrealized_net_estimate": "0.30", "entry_reason": "Impuls",
    } for side in ("LONG", "LONG")]
    message = format_decision_notification(decision, account, positions, [
        {"key": "break_70000", "description": "Obserwuj <odbicie> & wolumen"}
    ], [{"side": "SHORT", "id": str(uuid4()), "close_reason": "STOP_LOSS",
         "close_price": "73580.000000000000", "realized_net_estimate": "-1.662683219738"}])
    assert "<b>Decyzja:</b> WAIT" in message
    assert "<b>Akcja:</b> HOLD" in message
    assert "equity 1001.23 USD" in message
    assert "<b>Gotówka:</b> 999.00 USD" in message
    assert "<b>Otwarte pozycje (2):</b>" in message
    assert message.count("• <b>LONG") == 2
    assert "wynik netto szac. 0.30 USD" in message
    assert "<b>Monitorowane (1):</b>" in message
    assert "73446.90" in message
    assert "Obserwuj &lt;odbicie&gt; &amp; wolumen" in message
    assert "wejście 70000.00 | kurs 70100.00" in message
    assert "<b>SL:</b> 69000.00 | <b>TP:</b> brak" in message
    assert "<b>Zamknięte od poprzedniej analizy (1):</b>" in message
    assert "STOP_LOSS 73580.00 | netto -1.66 USD" in message
    assert len(message) <= 4096


def test_five_positions_remain_visible_with_long_agent_text():
    ids = [str(uuid4()) for _ in range(5)]
    decision = SimpleNamespace(
        decision="BUY", created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        text="analiza " * 1000,
        open_proposal={"side": "LONG", "stop_loss": "69000", "take_profit": "72000",
                       "reason": "powód " * 1000},
        position_actions=[{"action": "HOLD", "position_id": id_, "reason": "trzymaj " * 1000}
                          for id_ in ids],
        paper_status="DONE", paper_result={"status": "DONE", "results": []},
    )
    account = {
        "status": "ACTIVE", "equity_estimate": "1000", "cash_balance": "1000",
        "available_margin": "400", "reserved_margin": "600", "realized_pnl": "0",
        "fees_paid": "0", "pnl_quality": "ESTIMATED_PAPER_ONLY", "stale_data": False,
    }
    positions = [{
        "side": "LONG", "id": id_, "quantity_btc": "0.00100000", "entry_price": "70000.000000000000",
        "current_mark_price": "70100.000000000000", "stop_loss": "69000.000000000000",
        "take_profit": "72000.000000000000", "unrealized_net_estimate": "0.300000000000",
        "entry_reason": "powód " * 1000,
    } for id_ in ids]
    message = format_decision_notification(decision, account, positions)
    assert len(message) <= 4096
    assert message.count("• <b>LONG") == 5
    assert all(id_[:8] in message for id_ in ids)


def test_telegram_client_posts_plain_text_and_sanitizes_token_on_error():
    seen = []

    def success(request, timeout):
        seen.append((request, timeout))
        return io.BytesIO(b'{"ok":true,"result":{"message_id":1}}')

    client = TelegramBotClient(TelegramConfig("SECRET_TOKEN", "123"), opener=success)
    client.send_message("Test")
    request, timeout = seen[0]
    assert timeout == 5
    assert json.loads(request.data) == {"chat_id": "123", "text": "Test", "parse_mode": "HTML"}
    assert request.get_method() == "POST"

    def failure(request, timeout):
        raise URLError(request.full_url)

    try:
        TelegramBotClient(TelegramConfig("SECRET_TOKEN", "123"), opener=failure).send_message("Test")
        assert False, "Expected TelegramDeliveryError"
    except TelegramDeliveryError as exc:
        assert "SECRET_TOKEN" not in str(exc)


def test_telegram_outbox_retries_failure_then_marks_sent_once(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    connection = engine.connect()
    transaction = connection.begin()
    local_sessions = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(telegram_notifications, "SessionLocal", local_sessions)
    decision_id = uuid4()
    with local_sessions.begin() as session:
        session.add(AgentDecisionORM(
            id=decision_id, instrument="BTC/USD",
            candle_timestamp=datetime(2035, 1, 1, tzinfo=timezone.utc),
            created_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
            decision="WAIT", text="Powiadomienie testowe", model="test",
            open_proposal=None, position_actions=[], paper_status="DONE",
            paper_result={"status": "DONE", "results": []},
        ))
        session.flush()
        session.add(AgentMonitorItemORM(
            id=uuid4(), instrument="BTC/USD", key="monitor_test",
            description="Obserwuj wybicie <70000> & wolumen", status="ACTIVE",
            created_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2035, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2036, 1, 1, tzinfo=timezone.utc),
            created_by_decision_id=decision_id,
        ))
        assert enqueue_decision_notification(session, decision_id)
        assert enqueue_decision_notification(session, decision_id)

    class FakeClient:
        def __init__(self):
            self.messages = []
            self.fail = True

        def send_message(self, text):
            self.messages.append(text)
            if self.fail:
                raise TelegramDeliveryError("Telegram HTTP 503")

    client = FakeClient()
    try:
        assert process_pending_notifications(client=client)["failed"] == 1
        with local_sessions() as session:
            row = session.get(TelegramNotificationORM, decision_id)
            assert row.status == "PENDING"
            assert row.attempts == 1
            assert row.last_error == "Telegram HTTP 503"
        client.fail = False
        assert process_pending_notifications(client=client)["sent"] == 1
        assert process_pending_notifications(client=client)["sent"] == 0
        assert len(client.messages) == 2
        assert "Powiadomienie testowe" in client.messages[1]
        assert "monitor_test" in client.messages[1]
        assert "&lt;70000.00&gt; &amp; wolumen" in client.messages[1]
        with local_sessions() as session:
            row = session.get(TelegramNotificationORM, decision_id)
            assert row.status == "SENT"
            assert row.sent_at is not None
            assert row.attempts == 2
    finally:
        transaction.rollback()
        connection.close()


def test_unconfigured_telegram_does_not_queue(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    connection = engine.connect()
    transaction = connection.begin()
    local_sessions = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    with local_sessions.begin() as session:
        assert not enqueue_decision_notification(session, uuid4())
    transaction.rollback()
    connection.close()
    assert process_pending_notifications()["disabled"] is True


def test_many_monitor_items_are_split_without_losing_sections():
    decision = SimpleNamespace(
        decision="WAIT", created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        text="analiza " * 1000, open_proposal=None, position_actions=[],
        paper_status="DONE", paper_result={"status": "DONE", "results": []},
    )
    account = {
        "status": "ACTIVE", "equity_estimate": "1000", "cash_balance": "1000",
        "available_margin": "1000", "reserved_margin": "0", "realized_pnl": "0",
        "fees_paid": "0", "pnl_quality": "ESTIMATED_PAPER_ONLY", "stale_data": False,
    }
    monitors = [{"key": f"monitor_{index}", "description": "Obserwuj przebicie i wolumen " * 20}
                for index in range(20)]
    positions = [{
        "side": "LONG", "id": str(uuid4()), "quantity_btc": "0.00100000",
        "entry_price": "70000", "current_mark_price": "70100",
        "stop_loss": "69000", "take_profit": "72000",
        "unrealized_net_estimate": "0.30", "entry_reason": "długi powód " * 50,
    } for _ in range(5)]
    message = format_decision_notification(decision, account, positions, monitors)
    parts = _message_chunks(message)
    assert len(parts) >= 2
    assert all(1 <= len(part) <= 4096 for part in parts)
    assert "monitor_0" in message and "monitor_19" in message
    assert "<b>Monitorowane (20):</b>" in message
