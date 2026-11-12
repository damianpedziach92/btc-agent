from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.connection import engine
from database.models.agent_decision import AgentDecisionORM
from database.models.candle import CandleORM
from database.models.paper import PaperAccountLedgerORM, PaperPendingOrderORM, PaperPositionORM
from models.agent_decision import OpenProposal, PendingOrderAction, PendingInvalidation
from models.market import Instrument, Timeframe
from models.paper import (CFD_FIXED_20_USD, FeeRates, PaperSide, RiskConfig,
                          barrier_touch, cfd20_rates, execution_price)
from services.paper_trading import PaperTradingService
from services.pending_orders import PendingOrderManager, evaluate_order_candle
from services.market_context import build_market_context

D = Decimal
T0 = datetime(2042, 1, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def paper():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    service = PaperTradingService(session, risk=RiskConfig(), account_name=f"cfd20_{uuid4().hex}")
    try:
        yield service
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def add_candle(session, at, *, open_="73000", high="73001", low="72999", close="73000"):
    session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.ONE_MINUTE,
        timestamp=at, open=D(open_), high=D(high), low=D(low), close=D(close),
        vwap=D(close), volume=D("1"), trades=1))
    session.flush()


def seed(paper):
    paper.seed_account(T0 - timedelta(minutes=5))
    first = paper.configure_fee_profile(cfd20_rates(), T0 - timedelta(minutes=4))
    assert paper.configure_fee_profile(cfd20_rates(), T0).id == first.id
    for minutes in (3, 2, 1):
        add_candle(paper.session, T0 - timedelta(minutes=minutes))
    return first


def decision(paper):
    row = AgentDecisionORM(id=uuid4(), instrument=Instrument.BTC_USD.value,
        candle_timestamp=T0-timedelta(minutes=15), created_at=T0+timedelta(seconds=20),
        decision="WAIT", text="PAPER pending", model="test", position_actions=[],
        pending_order_actions=[], paper_status="DONE")
    paper.session.add(row)
    paper.session.flush()
    return row


def quote():
    return {"price": "73000", "bid": "72999", "ask": "73001",
            "retrieved_at": (T0 + timedelta(seconds=25)).isoformat(), "source": "test"}


def create_action(order_type, level, sl, tp):
    return PendingOrderAction(action="CREATE", order_type=order_type,
        entry_level_usd=level, stop_loss=sl, take_profit=tp,
        expires_at=T0+timedelta(minutes=30), scenario_key=f"s_{order_type}",
        setup_type="pullback", reason="M15 setup confirmed")


def test_cfd20_fixed_spread_and_legacy_snapshot():
    rates = cfd20_rates()
    assert FeeRates.from_dict(rates.as_dict()).execution_model == CFD_FIXED_20_USD
    for mid in (D("50000"), D("74000"), D("120000")):
        for side in (PaperSide.LONG, PaperSide.SHORT):
            entry = execution_price(mid, side, True, rates)
            exit_ = execution_price(mid, side, False, rates)
            assert entry - exit_ == (D("20") if side is PaperSide.LONG else D("-20"))
            for quantity in (D("1"), D("0.01")):
                pnl = (exit_ - entry if side is PaperSide.LONG else entry - exit_) * quantity
                assert pnl == -D("20") * quantity
    legacy = rates.as_dict()
    del legacy["execution_model"], legacy["spread_usd_per_btc"]
    assert FeeRates.from_dict(legacy).execution_model == "LEGACY_BPS"


def test_cfd_barrier_touch_and_gap():
    rates = cfd20_rates()
    candle = SimpleNamespace(open=D("74000"), high=D("74105"), low=D("73900"))
    assert barrier_touch(PaperSide.LONG, candle, D("73800"), D("74100"), rates)[0] is None
    candle.high = D("74110")
    assert barrier_touch(PaperSide.LONG, candle, D("73800"), D("74100"), rates)[:3] == (
        "TAKE_PROFIT", False, D("74100"))
    candle.low = D("73700")
    assert barrier_touch(PaperSide.LONG, candle, D("73800"), D("74100"), rates)[:3] == (
        "STOP_LOSS", True, D("73800"))
    candle.open = D("73700")
    assert barrier_touch(PaperSide.LONG, candle, D("73800"), D("74100"), rates)[2:] == (
        D("73690"), "GAP_FILL_ASSUMPTION")
    short = SimpleNamespace(open=D("74000"), high=D("74100"), low=D("73900"))
    assert barrier_touch(PaperSide.SHORT, short, D("74110"), D("73800"), rates)[0] == "STOP_LOSS"


@pytest.mark.parametrize("order_type,level,sl,tp,bar,expected", [
    ("BUY_LIMIT", "72950", "72800", "73200", {"low": "72940"}, "72950"),
    ("BUY_STOP", "73050", "72800", "73400", {"high": "73040"}, "73050"),
    ("SELL_LIMIT", "73050", "73200", "72800", {"high": "73060"}, "73050"),
    ("SELL_STOP", "72950", "73200", "72600", {"low": "72960"}, "72950"),
])
def test_pending_order_fill_is_future_idempotent_and_zero_fee(paper, order_type, level, sl, tp, bar, expected):
    seed(paper)
    row = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    created = manager.apply_action(create_action(order_type, level, sl, tp), row.id,
                                   quote(), T0+timedelta(seconds=25))
    assert created["status"] == "PENDING"
    order_id = UUID(created["order_id"])
    assert not paper.session.scalars(select(PaperPositionORM).where(PaperPositionORM.account_id == paper._account().id)).all()
    # A bar which began before CREATE is never eligible, even if it touched.
    add_candle(paper.session, T0, high="74000", low="72000")
    assert manager.process(T0+timedelta(minutes=1, seconds=5))["filled"] == 0
    add_candle(paper.session, T0+timedelta(minutes=1), **bar)
    outcome = manager.process(T0+timedelta(minutes=2, seconds=5))
    assert outcome["filled"] == 1
    order = paper.session.get(PaperPendingOrderORM, order_id)
    position = paper.session.get(PaperPositionORM, order.position_id)
    assert order.status == "FILLED"
    assert position.entry_price == D(expected)
    assert position.fee_snapshot["execution_model"] == CFD_FIXED_20_USD
    assert position.fee_total == 0
    assert manager.process(T0+timedelta(minutes=2, seconds=6))["filled"] == 0
    assert len(paper.session.scalars(select(PaperPositionORM).where(
        PaperPositionORM.account_id == paper._account().id)).all()) == 1
    assert not [item for item in paper.session.scalars(select(PaperAccountLedgerORM).where(
        PaperAccountLedgerORM.account_id == paper._account().id)) if item.event_type == "ENTRY_COST" and item.amount_usd != 0]


def test_limit_never_worse_and_stop_gap_is_conservative():
    candle = SimpleNamespace(open=D("20600"), high=D("20800"), low=D("20500"))
    assert evaluate_order_candle("BUY_LIMIT", D("20700"), candle, D("20"))[0] == D("20610")
    candle.open = D("20800")
    assert evaluate_order_candle("BUY_LIMIT", D("20700"), candle, D("20"))[0] == D("20700")
    assert evaluate_order_candle("BUY_STOP", D("20700"), candle, D("20")) == (
        D("20810"), "STOP_GAP_OPEN")


def test_pending_invalid_side_and_revision_conflict(paper):
    seed(paper)
    row = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    bad = manager.apply_action(create_action("BUY_LIMIT", "73020", "72800", "73200"),
                               row.id, quote(), T0+timedelta(seconds=25))
    assert bad["status"] == "REJECTED" and "INVALID_SIDE" in bad["reason"]
    created = manager.apply_action(create_action("BUY_LIMIT", "72950", "72800", "73200"),
                                   row.id, quote(), T0+timedelta(seconds=25))
    assert created["status"] == "PENDING"
    order_id = UUID(created["order_id"])
    conflict = manager.apply_action(PendingOrderAction(action="CANCEL", order_id=order_id,
        expected_revision=2, reason="outdated"), row.id, None, T0+timedelta(seconds=26))
    assert conflict["status"] == "REJECTED"
    cancelled = manager.apply_action(PendingOrderAction(action="CANCEL", order_id=order_id,
        expected_revision=1, reason="invalidated"), row.id, None, T0+timedelta(seconds=26))
    assert cancelled["status"] == "CANCELLED"


def test_immediate_cfd_entry_requires_verifiable_closed_trigger(paper):
    seed(paper)
    row = decision(paper)
    base = dict(side="LONG", reason="M15 pullback, M1 close confirmed", stop_loss=D("72800"),
                take_profit=D("73400"), valid_until=T0+timedelta(minutes=10),
                setup_type="pullback", market_regime="uptrend", signal_timeframe="1m",
                trigger_candle_timestamp=T0-timedelta(minutes=1), trigger_level=D("72990"),
                trigger_condition="CLOSE_ABOVE", invalidation_level=D("72850"),
                expected_rr_after_costs=D("1.8"), signal_age_seconds=20,
                countertrend=False)
    at = T0+timedelta(seconds=25)
    accepted = paper.open_position(OpenProposal(**base), D("73000"), at,
                                   execution_key="valid-trigger", decision_id=row.id, now=at)
    assert accepted["status"] == "ACCEPTED"
    rejected = paper.open_position(OpenProposal(**{**base, "trigger_level": D("73100")}), D("73000"), at,
                                   execution_key="invalid-trigger", decision_id=row.id, now=at)
    assert rejected["status"] == "REJECTED"
    assert "did not close above" in rejected["reason"]


def test_market_context_excludes_future_candles_and_keeps_nulls(paper):
    seed(paper)
    add_candle(paper.session, T0, open_="73000", high="80000", low="70000", close="80000")
    context = build_market_context(paper.session, T0+timedelta(seconds=20))
    assert context["timeframes"]["1m"]["last_closed_at"] == T0.isoformat()
    assert context["timeframes"]["1m"]["recent_closed_candles"][-1]["close"] == "73000.0000000000"
    assert context["timeframes"]["1h"]["indicators"] is None
    assert context["sha256"] == build_market_context(paper.session, T0+timedelta(seconds=20))["sha256"]


@pytest.mark.parametrize("side,bar,reason,fill", [
    ("LONG", {"open_": "73000", "high": "73110", "low": "72950"}, "TAKE_PROFIT", "73100"),
    ("SHORT", {"open_": "73000", "high": "73050", "low": "72890"}, "TAKE_PROFIT", "72900"),
    ("LONG", {"open_": "72800", "high": "73000", "low": "72750"}, "STOP_LOSS", "72790"),
    ("SHORT", {"open_": "73200", "high": "73250", "low": "73000"}, "STOP_LOSS", "73210"),
])
def test_cfd_service_barrier_fill_uses_executable_side(paper, side, bar, reason, fill):
    seed(paper)
    stop, target = (D("72900"), D("73100")) if side == "LONG" else (D("73100"), D("72900"))
    proposal = OpenProposal(side=side, reason="test", stop_loss=stop, take_profit=target,
                            valid_until=T0+timedelta(minutes=5))
    opened = paper.open_position(proposal, D("73000"), T0,
                                 execution_key=f"barrier:{side}:{reason}", now=T0)
    assert opened["status"] == "ACCEPTED"
    add_candle(paper.session, T0, **bar)
    result = paper.process_new_candles(T0+timedelta(minutes=1, seconds=5))
    assert result["closed"] == 1
    position = paper.session.get(PaperPositionORM, UUID(opened["position_id"]))
    assert position.close_reason == reason
    assert position.close_price == D(fill)
    assert position.fee_total == 0
    assert position.realized_net_pnl == position.realized_gross_pnl


def test_cfd_activation_cannot_reactivate_legacy_profile(paper):
    first = seed(paper)
    assert paper.configure_fee_profile(cfd20_rates(), T0).id == first.id
    legacy = FeeRates.from_dict({**cfd20_rates().as_dict(),
        "execution_model": "LEGACY_BPS", "spread_usd_per_btc": "0", "spread_bps": "4"})
    with pytest.raises(ValueError, match="Legacy profiles cannot be reactivated"):
        paper.configure_fee_profile(legacy, T0)
    assert paper._fee_profile(paper._account().id).id == first.id


def test_cfd_sizing_respects_post_cost_margin_boundary(paper):
    seed(paper)
    paper.risk = RiskConfig(max_position_margin=D("0.90"), max_total_margin=D("0.90"),
                            min_free_margin=D("0.05"))
    proposal = OpenProposal(side="LONG", reason="boundary", stop_loss=D("72900"),
                            take_profit=D("73400"), valid_until=T0+timedelta(minutes=5))
    outcome = paper.open_position(proposal, D("73000"), T0, execution_key="margin-boundary", now=T0)
    assert outcome["status"] == "ACCEPTED"
    account = paper.account_snapshot(T0)
    assert D(account["reserved_margin"]) <= D(account["equity_estimate"])*D("0.90")
    assert D(account["cash_balance"])-D(account["reserved_margin"]) >= D(account["equity_estimate"])*D("0.05")


def test_pending_expiry_and_hold_are_idempotent(paper):
    seed(paper)
    row = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    action = create_action("BUY_LIMIT", "72950", "72800", "73200")
    action.expires_at = T0+timedelta(minutes=2)
    created = manager.apply_action(action, row.id, quote(), T0+timedelta(seconds=25))
    order_id = UUID(created["order_id"])
    hold = PendingOrderAction(action="HOLD", order_id=order_id, expected_revision=1,
                              reason="setup persists")
    assert manager.apply_action(hold, row.id, None, T0+timedelta(seconds=30))["status"] == "PENDING"
    assert manager.apply_action(hold, row.id, None, T0+timedelta(seconds=30))["status"] == "PENDING"
    add_candle(paper.session, T0)
    add_candle(paper.session, T0+timedelta(minutes=1), high="73020", low="72980")
    add_candle(paper.session, T0+timedelta(minutes=2), high="73000", low="72900")
    assert manager.process(T0+timedelta(minutes=3, seconds=5))["expired"] == 1
    assert manager.process(T0+timedelta(minutes=3, seconds=6))["expired"] == 0
    assert paper.session.get(PaperPendingOrderORM, order_id).status == "EXPIRED"


def test_pending_amend_ignores_bar_started_before_update(paper):
    seed(paper)
    first_decision = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    created = manager.apply_action(create_action("BUY_LIMIT", "72950", "72800", "73200"),
                                   first_decision.id, quote(), T0+timedelta(seconds=25))
    order_id = UUID(created["order_id"])
    add_candle(paper.session, T0)
    second_decision = AgentDecisionORM(id=uuid4(), instrument=Instrument.BTC_USD.value,
        candle_timestamp=T0-timedelta(minutes=14), created_at=T0+timedelta(minutes=1, seconds=20),
        decision="WAIT", text="AMEND", model="test", position_actions=[], pending_order_actions=[],
        analysis_trace={"paper_context": {"pending_orders": manager.snapshot()}}, paper_status="DONE")
    paper.session.add(second_decision)
    paper.session.flush()
    fresh = {**quote(), "retrieved_at": (T0+timedelta(minutes=1, seconds=25)).isoformat()}
    amended = manager.apply_action(PendingOrderAction(action="AMEND", order_id=order_id,
        expected_revision=1, entry_level_usd=D("72940"), reason="lower pullback"),
        second_decision.id, fresh, T0+timedelta(minutes=1, seconds=25))
    assert amended["status"] == "PENDING" and amended["revision"] == 2
    add_candle(paper.session, T0+timedelta(minutes=1), high="73000", low="72920")
    assert manager.process(T0+timedelta(minutes=2, seconds=5))["filled"] == 0
    add_candle(paper.session, T0+timedelta(minutes=2), high="73000", low="72930")
    assert manager.process(T0+timedelta(minutes=3, seconds=5))["filled"] == 1


def test_pending_fill_notification_is_deduplicated(paper, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    import services.telegram_notifications as notifications

    seed(paper)
    row = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    monkeypatch.setattr(notifications.TelegramConfig, "from_env", classmethod(lambda cls: object()))
    created = manager.apply_action(create_action("BUY_LIMIT", "72950", "72800", "73200"),
                                   row.id, quote(), T0+timedelta(seconds=25))
    add_candle(paper.session, T0)
    add_candle(paper.session, T0+timedelta(minutes=1), low="72940")
    assert manager.process(T0+timedelta(minutes=2, seconds=5))["filled"] == 1
    monkeypatch.setattr(notifications, "SessionLocal", sessionmaker(
        bind=paper.session.connection(), autoflush=False, expire_on_commit=False))

    class FakeClient:
        def __init__(self):
            self.messages = []

        def send_message(self, message):
            self.messages.append(message)

    client = FakeClient()
    assert notifications.process_pending_order_notifications(client=client)["sent"] == 1
    assert notifications.process_pending_order_notifications(client=client)["sent"] == 0
    assert created["order_id"] in client.messages[0]


def test_reviewer_context_includes_order_create_and_fill(paper):
    from services.decision_review import build_review_context

    seed(paper)
    row = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    action = create_action("BUY_LIMIT", "72950", "72800", "73200")
    row.pending_order_actions = [action.model_dump(mode="json")]
    created = manager.apply_action(action, row.id, quote(), T0+timedelta(seconds=25))
    row.paper_result = {"status": "DONE", "results": [created]}
    add_candle(paper.session, T0)
    add_candle(paper.session, T0+timedelta(minutes=1), low="72940")
    assert manager.process(T0+timedelta(minutes=2, seconds=5))["filled"] == 1
    context = build_review_context(paper.session, T0+timedelta(hours=2))
    assert {item["type"] for item in context["actions"]} >= {"ORDER_CREATE", "ORDER_FILL"}
    assert context["orders"][0]["status_at_window_end"] == "FILLED"
    assert context["actions"][0]["order_id"] == created["order_id"]


def test_machine_invalidation_precedes_same_bar_pending_trigger(paper):
    seed(paper)
    row = decision(paper)
    manager = PendingOrderManager(paper.session, paper)
    action = create_action("BUY_LIMIT", "72950", "72800", "73200")
    action.invalidating_condition = PendingInvalidation(timeframe="1m", operator="CLOSE_BELOW", level=D("72970"))
    created = manager.apply_action(action, row.id, quote(), T0+timedelta(seconds=25))
    add_candle(paper.session, T0)
    add_candle(paper.session, T0+timedelta(minutes=1), open_="73000", high="73010",
               low="72940", close="72960")
    assert manager.process(T0+timedelta(minutes=2, seconds=5))["filled"] == 0
    order = paper.session.get(PaperPendingOrderORM, UUID(created["order_id"]))
    assert order.status == "CANCELLED"
