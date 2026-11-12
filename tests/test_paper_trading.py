from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from database.connection import SessionLocal, engine
from database.models.candle import CandleORM
from database.models.agent_decision import AgentDecisionORM
from database.models.paper import (
    PaperAccountLedgerORM, PaperAccountORM, PaperFeeProfileORM,
    PaperPositionEventORM, PaperPositionORM,
)
from models.agent_decision import AgentDecision, OpenProposal
from models.market import Instrument, Timeframe
from models.paper import FeeRates, MarketType, PaperSide, RiskConfig, RiskEngine, entry_costs, estimated_target_net_pnl, execution_price, funding_cost, gross_pnl, money, rollover_cost
from scripts.seed_paper_account import okx_eea_taker_profile
from services.paper_trading import PaperTradingService


T0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
D = Decimal


@pytest.fixture
def paper():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    service = PaperTradingService(session, risk=RiskConfig(), account_name=f"test_{uuid4().hex}",
                                  allow_legacy_test_entries=True)
    try:
        yield service
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def rates(version=1, rollover="0.0002"):
    return FeeRates(MarketType.SPOT_MARGIN, "ESTIMATED", "test_simulation", version,
                   D("0.0026"), D("0.0026"), D("0.0002"), D(rollover), D("5"), D("10"))


def candle(session, at, open_="100", high="101", low="99", close="100"):
    session.add(CandleORM(
        symbol=Instrument.BTC_USD, timeframe=Timeframe.ONE_MINUTE, timestamp=at,
        open=D(open_), high=D(high), low=D(low), close=D(close),
        vwap=D(close), volume=D("1"), trades=1,
    ))
    session.flush()


def ready(paper):
    account = paper.seed_account(T0 - timedelta(minutes=1))
    assert paper.seed_account(T0) .id == account.id
    assert account.cash_balance == D("1000")
    profile = paper.configure_fee_profile(rates(), T0)
    assert paper.configure_fee_profile(rates(), T0).id == profile.id
    for minutes in (3, 2, 1):
        candle(paper.session, T0 - timedelta(minutes=minutes))
    return account


def proposal(side="LONG", sl=None, tp=None, valid=None):
    return OpenProposal(side=side, reason="Symulowany impuls", stop_loss=sl,
                        take_profit=tp, valid_until=valid or T0 + timedelta(minutes=10))


def open_one(paper, side="LONG", key="one", sl=None, tp=None):
    sl = D("95") if sl is None and side == "LONG" else D("105") if sl is None else sl
    return paper.open_position(proposal(side, sl, tp), D("100"), T0,
                               execution_key=key, now=T0)


def test_seed_open_close_ledger_and_idempotence(paper):
    account = ready(paper)
    result = open_one(paper, tp=D("105"))
    assert result["status"] == "ACCEPTED"
    assert open_one(paper)["position_id"] == result["position_id"]
    snap = paper.account_snapshot(T0)
    assert D(snap["reserved_margin"]) <= D("200")
    assert D(snap["available_margin"]) > D("790")
    view = paper.open_positions_snapshot(T0)[0]
    assert D(view["unrealized_net_estimate"]) < D(view["unrealized_gross_pnl"]) - D(view["initial_costs"])
    position_id = UUID(result["position_id"])
    closed = paper.close_position(position_id, "AGENT", D("110"), T0 + timedelta(minutes=1), source="test")
    assert closed["status"] == "CLOSED"
    assert paper.close_position(position_id, "AGENT", D("120"), T0 + timedelta(minutes=2), source="test")["status"] == "ALREADY_CLOSED"
    position = paper.session.get(PaperPositionORM, position_id)
    assert position.realized_gross_pnl == money(gross_pnl(PaperSide.LONG, position.entry_price, position.close_price, position.quantity_btc))
    assert position.realized_net_pnl == position.realized_gross_pnl - position.fee_total
    assert paper.account_snapshot(T0 + timedelta(minutes=1))["reserved_margin"] == "0"
    ledger = paper.session.scalars(select(PaperAccountLedgerORM).where(PaperAccountLedgerORM.account_id == account.id)).all()
    assert sum((row.amount_usd for row in ledger), D("0")) == account.cash_balance
    assert sum((row.margin_delta_usd for row in ledger), D("0")) == 0
    assert len([row for row in ledger if row.event_type == "EXIT_FEE"]) == 1


def test_m15_strategy_rejects_tight_stop_and_accepts_structural_stop(paper):
    ready(paper)
    paper.risk = RiskConfig(min_stop_m15_atr_multiple=D("1"),
                            stop_swing_m15_candles=2, stop_swing_atr_buffer=D("0.25"))
    missing = paper.open_position(proposal(sl=D("95"), tp=D("110")), D("100"), T0,
                                  execution_key="m15-missing", now=T0)
    assert missing["status"] == "REJECTED"
    assert "M15 ATR/swing" in missing["reason"]
    for index in range(30):
        at = T0 - timedelta(minutes=15 * (index + 1))
        paper.session.add(CandleORM(
            symbol=Instrument.BTC_USD, timeframe=Timeframe.FIFTEEN_MINUTES,
            timestamp=at, open=D("100"), high=D("102"), low=D("98"),
            close=D("100"), vwap=D("100"), volume=D("1"), trades=1,
        ))
    paper.session.flush()
    tight = paper.open_position(proposal(sl=D("98"), tp=D("110")), D("100"), T0,
                                execution_key="m15-tight", now=T0)
    assert tight["status"] == "REJECTED"
    assert "SL too close for M15 structure" in tight["reason"]
    wide = paper.open_position(proposal(sl=D("95"), tp=D("110")), D("100"), T0,
                               execution_key="m15-wide", now=T0)
    assert wide["status"] == "ACCEPTED"


def test_m15_strategy_short_stop_must_be_above_swing_and_atr(paper):
    ready(paper)
    paper.risk = RiskConfig(min_stop_m15_atr_multiple=D("1"),
                            stop_swing_m15_candles=2, stop_swing_atr_buffer=D("0.25"))
    for index in range(30):
        paper.session.add(CandleORM(
            symbol=Instrument.BTC_USD, timeframe=Timeframe.FIFTEEN_MINUTES,
            timestamp=T0 - timedelta(minutes=15 * (index + 1)),
            open=D("100"), high=D("102"), low=D("98"), close=D("100"),
            vwap=D("100"), volume=D("1"), trades=1,
        ))
    paper.session.flush()
    tight = paper.open_position(proposal("SHORT", sl=D("102"), tp=D("90")),
                                D("100"), T0, execution_key="m15-short-tight", now=T0)
    assert tight["status"] == "REJECTED"
    assert "SHORT SL must be >=" in tight["reason"]
    wide = paper.open_position(proposal("SHORT", sl=D("105"), tp=D("90")),
                               D("100"), T0, execution_key="m15-short-wide", now=T0)
    assert wide["status"] == "ACCEPTED"


def test_m15_profile_increases_paper_exposure_without_leverage():
    config = RiskConfig(
        max_risk_per_position=D("0.02"), max_total_risk=D("0.06"),
        max_position_margin=D("0.40"), max_total_margin=D("0.80"),
        min_free_margin=D("0.10"), emergency_drawdown=D("0.15"),
        min_stop_m15_atr_multiple=D("1"), stop_swing_atr_buffer=D("0.25"),
    )
    quantity, reason = RiskEngine(config).size(
        side=PaperSide.LONG, entry=D("100"), stop_loss=D("95"),
        take_profit=D("110"), equity=D("1000"), cash=D("1000"),
        reserved=D("0"), current_risk=D("0"), position_count=0,
        rates=okx_eea_taker_profile(),
    )
    assert reason == "ACCEPTED"
    assert D("300") < quantity * D("100") <= D("400")


@pytest.mark.parametrize("side,sl,tp,expected_entry,expected_exit", [
    ("LONG", "95", "110", "100.100000000000", "99.900000000000"),
    ("SHORT", "105", "90", "99.900000000000", "100.100000000000"),
])
def test_configured_spread_uses_observed_quote_midpoint_for_paper_fills(
    paper, side, sl, tp, expected_entry, expected_exit,
):
    ready(paper)
    result = paper.open_position(
        proposal(side, D(sl), D(tp)), D("100"), T0,
        execution_key=f"quote:{side}", now=T0,
        bid_price=D("99"), ask_price=D("101"),
    )
    assert result["status"] == "ACCEPTED"
    position_id = UUID(result["position_id"])
    position = paper.session.get(PaperPositionORM, position_id)
    assert position.entry_price == D(expected_entry)
    assert position.entry_source == "kraken_spot_mid_fixed_spread_paper"
    closed = paper.close_position(
        position_id, "AGENT", D("100"), T0 + timedelta(minutes=1),
        source="test_ticker", bid_price=D("99"), ask_price=D("101"),
    )
    assert closed["status"] == "CLOSED"
    assert position.close_price == D(expected_exit)
    events = paper.session.scalars(select(PaperPositionEventORM).where(
        PaperPositionEventORM.position_id == position_id,
    )).all()
    assert [event.payload["quote_mode"] for event in events if event.type in {"OPENED", "CLOSED"}] == [
        "fixed_spread_quote_mid", "fixed_spread_quote_mid",
    ]


def test_requested_paper_profile_charges_only_taker_and_four_bps_spread(paper):
    rates = okx_eea_taker_profile()
    assert rates.trade_entry_fee_rate == rates.trade_exit_fee_rate == D("0.001")
    assert rates.margin_opening_fee_rate == rates.rollover_rate_per_4h == rates.slippage_bps == 0
    assert rates.spread_bps == 4
    buy = execution_price(D("74000"), PaperSide.LONG, True, rates,
                          bid=D("73999.9"), ask=D("74000.1"))
    sell = execution_price(D("74000"), PaperSide.LONG, False, rates,
                           bid=D("73999.9"), ask=D("74000.1"))
    assert buy == D("74014.8") and sell == D("73985.2")
    assert buy - sell == D("29.6")
    quantity = D("200") / buy
    cost, _ = entry_costs(rates, PaperSide.LONG, quantity, buy)
    assert cost == D("0.20")
    assert rollover_cost(rates, PaperSide.LONG, quantity, D("200"), buy)[0] == 0
    account = ready(paper)
    paper.configure_fee_profile(rates, T0)
    opened = open_one(paper, tp=D("105"))
    assert opened["status"] == "ACCEPTED"
    position = paper.session.get(PaperPositionORM, UUID(opened["position_id"]))
    assert position.initial_costs == money(position.entry_notional * D("0.001"))
    view = paper.open_positions_snapshot(T0)[0]
    assert view["next_holding_fee_at"] is None
    estimated_exit = execution_price(D("100"), PaperSide.LONG, False, rates)
    estimated_net = ((estimated_exit - position.entry_price) * position.quantity_btc
                     - position.initial_costs - estimated_exit * position.quantity_btc * D("0.001"))
    assert D(view["unrealized_net_estimate"]) == estimated_net
    assert D(paper.account_snapshot(T0)["unrealized_net_estimate"]) == estimated_net
    assert paper.apply_holding_costs(position, account, T0 + timedelta(hours=4), D("100")) == 0
    assert paper.session.scalar(select(func.count()).select_from(PaperAccountLedgerORM).where(
        PaperAccountLedgerORM.position_id == position.id,
        PaperAccountLedgerORM.event_type == "ROLLOVER",
    )) == 0


def test_unconfigured_costs_block_new_position(paper):
    paper.seed_account(T0)
    for minutes in (3, 2, 1):
        candle(paper.session, T0 - timedelta(minutes=minutes))
    assert paper.account_snapshot(T0)["fee_status"] == "UNCONFIGURED"
    result = open_one(paper)
    assert result["status"] == "REJECTED"
    assert "UNCONFIGURED" in result["reason"]


def test_short_multiple_positions_limits_and_no_sl(paper):
    ready(paper)
    assert paper.open_position(proposal("SHORT", None, D("90")), D("100"), T0,
                               execution_key="no-sl", now=T0)["status"] == "REJECTED"
    first = open_one(paper, "SHORT", "short-1", sl=D("105"), tp=D("90"))
    assert first["status"] == "ACCEPTED"
    second = open_one(paper, "SHORT", "short-2", sl=D("105"))
    third = open_one(paper, "SHORT", "short-3", sl=D("105"))
    assert second["status"] == third["status"] == "ACCEPTED"
    assert paper.account_snapshot(T0)["open_position_count"] == 3
    assert D(paper.account_snapshot(T0)["reserved_margin"]) <= D("600")
    assert open_one(paper, "LONG", "opposite", sl=D("95"))["status"] == "REJECTED"
    position_id = UUID(first["position_id"])
    paper.close_position(position_id, "AGENT", D("90"), T0 + timedelta(minutes=1), source="test")
    position = paper.session.get(PaperPositionORM, position_id)
    assert position.realized_gross_pnl == money(gross_pnl(PaperSide.SHORT, position.entry_price, position.close_price, position.quantity_btc))
    assert position.realized_gross_pnl > 0


def test_no_sl_requires_explicit_mode_and_has_smaller_cap(paper):
    ready(paper)
    paper.risk = RiskConfig(allow_positions_without_sl=True)
    result = paper.open_position(proposal("LONG", None, None), D("100"), T0,
                                 execution_key="no-sl-enabled", now=T0)
    assert result["status"] == "ACCEPTED"
    position = paper.session.get(PaperPositionORM, UUID(result["position_id"]))
    assert position.reserved_margin <= D("50")
    candle(paper.session, T0 + timedelta(hours=4), high="101", low="99", close="100")
    candle(paper.session, T0 + timedelta(hours=4, minutes=1), high="101", low="99", close="100")
    paper.process_new_candles(T0 + timedelta(hours=4, minutes=2))
    assert position.close_reason == "TIMEOUT"
    assert position.accrued_holding_costs > 0


def test_stale_data_bad_levels_and_invalid_decision_rejected(paper):
    ready(paper)
    bad_level = paper.open_position(proposal("LONG", D("105"), D("110")), D("100"), T0,
                                    execution_key="bad-level", now=T0)
    assert bad_level["status"] == "REJECTED"
    stale_at = T0 + timedelta(minutes=5)
    stale = paper.open_position(proposal("LONG", D("95"), D("105")), D("100"), stale_at,
                                execution_key="stale-feed", now=stale_at)
    assert stale["status"] == "REJECTED"
    assert "stale" in stale["reason"].lower()
    candle(paper.session, T0 + timedelta(minutes=2))
    gapped_at = T0 + timedelta(minutes=3)
    gapped = paper.open_position(proposal("LONG", D("95"), D("105")), D("100"), gapped_at,
                                 execution_key="gapped-feed", now=gapped_at)
    assert gapped["status"] == "REJECTED"
    assert "gapped" in gapped["reason"].lower()
    with pytest.raises(ValueError):
        AgentDecision.model_validate({
            "decision": "BUY", "open_proposal": None,
            "position_actions": [], "to_monitor": [], "text": "x",
        })
    with pytest.raises(ValueError):
        proposal("LONG", D("-1"), D("105"))


def test_tp_with_gross_profit_but_net_loss_is_rejected_before_reserving_cash(paper):
    account = ready(paper)
    result = paper.open_position(
        proposal("SHORT", D("105"), D("99.5")), D("100"), T0,
        execution_key="net-negative-tp", now=T0,
    )
    assert result["status"] == "REJECTED"
    assert "estimated net" in result["reason"]
    assert account.cash_balance == D("1000")
    assert paper.account_snapshot(T0)["open_position_count"] == 0
    assert any(event["type"] == "REJECTED" and "estimated net" in event["payload"]["reason"]
               for event in paper.recent_events())


def test_tp_must_cover_max_hold_rollover_and_minimum_visible_profit():
    fee = rates()
    common = dict(side=PaperSide.SHORT, entry=D("100"), stop_loss=D("105"),
                  equity=D("1000"), cash=D("1000"), reserved=D("0"),
                  current_risk=D("0"), position_count=0, rates=fee)
    assert RiskEngine(RiskConfig()).size(take_profit=D("99.4"), **common)[0] is None
    assert RiskEngine(RiskConfig(max_hold_hours=1)).size(take_profit=D("99.4"), **common)[0] is not None
    assert RiskEngine(RiskConfig()).size(take_profit=D("98"), **common)[0] is not None
    assert RiskEngine(RiskConfig()).size(take_profit=None, **common)[0] is not None
    assert estimated_target_net_pnl(
        fee, PaperSide.SHORT, D("100"), D("99.5"), D("2"), D("105"), 6
    ) < 0

    free_fee = FeeRates(MarketType.SPOT_MARGIN, "ESTIMATED", "test", 1,
                        D("0"), D("0"), D("0"), D("0"), D("0"), D("0"))
    tiny = RiskEngine(RiskConfig()).size(
        side=PaperSide.LONG, entry=D("100"), stop_loss=D("95"),
        take_profit=D("100.001"), equity=D("1000"), cash=D("1000"),
        reserved=D("0"), current_risk=D("0"), position_count=0, rates=free_fee,
    )
    assert tiny[0] is None and "minimum is 0.01 USD" in tiny[1]


def test_previous_paper_short_target_would_now_be_rejected():
    fee = rates()
    entry = D("73365.2613")
    target = D("73280")
    quantity = D("0.00271916")
    assert estimated_target_net_pnl(
        fee, PaperSide.SHORT, entry, target, quantity, D("73580"), 0
    ) == D("-0.844814274938")
    sized, reason = RiskEngine(RiskConfig()).size(
        side=PaperSide.SHORT, entry=entry, stop_loss=D("73580"),
        take_profit=target, equity=D("998"), cash=D("998"),
        reserved=D("0"), current_risk=D("0"), position_count=0, rates=fee,
    )
    assert sized is None and "estimated net" in reason


@pytest.mark.parametrize("side,high,low,reason", [
    ("LONG", "101", "95", "STOP_LOSS"),
    ("LONG", "105", "99", "TAKE_PROFIT"),
    ("SHORT", "105", "99", "STOP_LOSS"),
    ("SHORT", "101", "95", "TAKE_PROFIT"),
    ("LONG", "105", "95", "STOP_LOSS"),
])
def test_ohlc_exit_no_lookahead_and_stop_first(paper, side, high, low, reason):
    ready(paper)
    result = open_one(paper, side, sl=D("95") if side == "LONG" else D("105"),
                      tp=D("105") if side == "LONG" else D("95"))
    position_id = UUID(result["position_id"])
    # The entry bar began before the position. Its extreme must never close it.
    candle(paper.session, T0, high="106", low="94", close="100")
    candle(paper.session, T0 + timedelta(minutes=1), high=high, low=low, close="100")
    candle(paper.session, T0 + timedelta(minutes=2), open_="94", high="101", low="93", close="94")
    paper.session.get(PaperPositionORM, position_id).entry_at = T0 + timedelta(seconds=30)
    paper.process_new_candles(T0 + timedelta(minutes=3))
    position = paper.session.get(PaperPositionORM, position_id)
    assert position.close_reason == reason
    assert position.close_source == "1m_ohlc_barrier_assumption"
    assert position.close_price == (position.stop_loss if reason == "STOP_LOSS" else position.take_profit)
    assert position.last_evaluated_candle_timestamp == T0 + timedelta(minutes=1)
    if high == "105" and low == "95":
        assert "AMBIGUOUS_BOTH_HIT" in position.close_reason_text
    events_before = paper.session.scalar(select(func.count()).select_from(PaperPositionEventORM).where(
        PaperPositionEventORM.position_id == position_id, PaperPositionEventORM.type == "CLOSED"))
    paper.process_new_candles(T0 + timedelta(minutes=3))
    events_after = paper.session.scalar(select(func.count()).select_from(PaperPositionEventORM).where(
        PaperPositionEventORM.position_id == position_id, PaperPositionEventORM.type == "CLOSED"))
    assert events_before == events_after == 1


@pytest.mark.parametrize("side,high,low,reason", [
    ("LONG", "106", "99", "TAKE_PROFIT"),
    ("SHORT", "101", "94", "TAKE_PROFIT"),
    ("LONG", "101", "94", "STOP_LOSS"),
    ("SHORT", "106", "99", "STOP_LOSS"),
    ("SHORT", "106", "94", "STOP_LOSS"),
])
def test_latest_closed_1m_bar_exits_before_next_agent_analysis(paper, side, high, low, reason):
    ready(paper)
    entered_at = T0 + timedelta(seconds=30)
    result = paper.open_position(
        proposal(side, D("95") if side == "LONG" else D("105"),
                 D("105") if side == "LONG" else D("95")),
        D("100"), entered_at, execution_key=f"live-{side}-{reason}", now=entered_at,
    )
    assert result["status"] == "ACCEPTED"
    position_id = UUID(result["position_id"])
    # This entry bar is incomplete at entry and must be ignored, even if it hit TP/SL.
    candle(paper.session, T0, high="110", low="90", close="100")
    candle(paper.session, T0 + timedelta(minutes=1), high=high, low=low, close="100")
    processed_at = T0 + timedelta(minutes=2, seconds=5)
    assert paper.process_new_candles(T0 + timedelta(minutes=1, seconds=59))["closed"] == 0
    position = paper.session.get(PaperPositionORM, position_id)
    assert position.status == "OPEN"
    assert position.last_evaluated_candle_timestamp is None
    assert paper.process_new_candles(processed_at)["closed"] == 1
    assert position.close_reason == reason
    assert position.close_source == "1m_ohlc_barrier_assumption"
    assert position.close_price == (position.stop_loss if reason == "STOP_LOSS" else position.take_profit)
    assert position.close_at == (T0 + timedelta(minutes=2))
    assert position.last_evaluated_candle_timestamp == T0 + timedelta(minutes=1)
    assert paper.process_new_candles(processed_at)["closed"] == 0
    closed_events = paper.session.scalars(select(PaperPositionEventORM).where(
        PaperPositionEventORM.position_id == position_id,
        PaperPositionEventORM.type == "CLOSED",
    )).all()
    assert len(closed_events) == 1
    assert closed_events[0].payload["reference_price_at"] is None
    if high == "106" and low == "94":
        assert "AMBIGUOUS_BOTH_HIT" in position.close_reason_text


def test_non_barrier_timeout_waits_for_post_close_price(paper):
    ready(paper)
    paper.risk = RiskConfig(max_hold_hours=1)
    opened = open_one(paper, "LONG", "timeout-price", sl=D("95"))
    position_id = UUID(opened["position_id"])
    candle(paper.session, T0 + timedelta(hours=1), high="101", low="99", close="100")
    processed_at = T0 + timedelta(hours=1, minutes=1, seconds=5)
    first = paper.process_new_candles(processed_at)
    assert first["closed"] == 0 and first["pending_price"] == 1
    assert paper.session.get(PaperPositionORM, position_id).status == "OPEN"
    ticker = {"price": "100", "retrieved_at": (processed_at - timedelta(seconds=1)).isoformat()}
    second = paper.process_new_candles(processed_at, ticker=ticker)
    assert second["closed"] == 1 and second["pending_price"] == 0
    assert paper.session.get(PaperPositionORM, position_id).close_reason == "TIMEOUT"


def test_rollover_snapshot_and_funding_sign(paper):
    account = ready(paper)
    result = open_one(paper, "SHORT", "rollover", sl=D("105"))
    position = paper.session.get(PaperPositionORM, UUID(result["position_id"]))
    profile = paper.configure_fee_profile(rates(2, rollover="0.01"), T0 + timedelta(minutes=1))
    assert profile.version == 2
    first_cost, basis = rollover_cost(rates(), PaperSide.SHORT, position.quantity_btc,
                                      position.entry_notional, position.entry_price)
    assert "fee_btc" in basis
    assert paper.apply_holding_costs(position, account, T0 + timedelta(hours=4), D("100")) == 1
    assert position.accrued_holding_costs == money(first_cost)
    paper.session.flush()
    paper.session.expunge_all()  # reload persisted state like a restarted worker
    reloaded = PaperTradingService(paper.session, risk=RiskConfig(), account_name=paper.account_name)
    position = paper.session.get(PaperPositionORM, position.id)
    account = reloaded._account()
    assert reloaded.apply_holding_costs(position, account, T0 + timedelta(hours=4), D("100")) == 0
    assert funding_cost(PaperSide.LONG, D("100"), D("0.0002")) == D("0.02")
    assert funding_cost(PaperSide.SHORT, D("100"), D("0.0002")) == D("-0.02")
    assert funding_cost(PaperSide.LONG, D("100"), D("-0.0002")) == D("-0.02")


def decision_row(paper, at, direction, open_proposal=None, actions=None):
    decision = AgentDecision.model_validate({
        "decision": direction,
        "open_proposal": open_proposal.model_dump(mode="json") if open_proposal else None,
        "position_actions": actions or [], "to_monitor": [], "text": "Test decyzji",
    })
    row = AgentDecisionORM(
        id=uuid4(), instrument=Instrument.BTC_USD.value,
        candle_timestamp=at - timedelta(minutes=15), created_at=at,
        decision=decision.decision.value, text=decision.text, model="test-model",
        open_proposal=decision.open_proposal.model_dump(mode="json") if decision.open_proposal else None,
        position_actions=[action.model_dump(mode="json") for action in decision.position_actions],
        paper_status="PENDING",
    )
    paper.session.add(row)
    paper.session.flush()
    return row


def test_agent_open_hold_close_and_stale_close_rejected(paper):
    ready(paper)
    opening = decision_row(paper, T0, "BUY", proposal("LONG", D("95"), D("105")))
    ticker = {"price": "100", "bid": "100.00", "ask": "100.10",
              "retrieved_at": (T0 + timedelta(seconds=1)).isoformat(), "source": "test_ticker"}
    result = paper.process_decision(opening.id, ticker, T0 + timedelta(seconds=1))
    assert result["results"][0]["status"] == "ACCEPTED"
    assert paper.process_decision(opening.id, ticker, T0 + timedelta(seconds=1))["reason"] == "Already processed"
    position_id = UUID(result["results"][0]["position_id"])
    assert paper.session.get(PaperPositionORM, position_id).entry_price == D("100.150050000000")
    view = paper.open_positions_snapshot(T0 + timedelta(seconds=1))[0]
    assert view["entry_reason"] == "Symulowany impuls"
    assert view["stop_loss"] == "95.000000000000"
    assert view["initial_costs"] != "0"

    hold = decision_row(paper, T0 + timedelta(seconds=2), "WAIT",
                        actions=[{"position_id": position_id, "action": "HOLD", "reason": "Scenariusz trwa"}])
    ledger_count = paper.session.scalar(select(func.count()).select_from(PaperAccountLedgerORM))
    paper.process_decision(hold.id, None, T0 + timedelta(seconds=2))
    assert paper.session.scalar(select(func.count()).select_from(PaperAccountLedgerORM)) == ledger_count

    close = decision_row(paper, T0 + timedelta(seconds=3), "WAIT",
                         actions=[{"position_id": position_id, "action": "CLOSE", "reason": "Teza wygasła"}])
    close_ticker = {"price": "102", "bid": "101.90", "ask": "102.10",
                    "retrieved_at": (T0 + timedelta(seconds=4)).isoformat(), "source": "test_ticker"}
    result = paper.process_decision(close.id, close_ticker, T0 + timedelta(seconds=4))
    assert result["results"][0]["reason"] == "AGENT"
    assert paper.session.get(PaperPositionORM, position_id).status == "CLOSED"
    assert paper.session.get(PaperPositionORM, position_id).close_price == D("101.898000000000")
    stale = decision_row(paper, T0 + timedelta(seconds=5), "WAIT",
                         actions=[{"position_id": position_id, "action": "CLOSE", "reason": "Spóźnione"}])
    late_ticker = {"price": "103", "retrieved_at": (T0 + timedelta(seconds=6)).isoformat(), "source": "test_ticker"}
    result = paper.process_decision(stale.id, late_ticker, T0 + timedelta(seconds=6))
    assert result["results"][0]["status"] == "REJECTED"
    assert len([row for row in paper.recent_events(10) if row["type"] == "REJECTED"]) >= 1


def test_parallel_opens_do_not_overallocate_margin():
    account_name = f"test_{uuid4().hex}"
    with SessionLocal.begin() as session:
        service = PaperTradingService(session, risk=RiskConfig(), account_name=account_name)
        now = T0 + timedelta(days=uuid4().int % 10000)
        mark = D("74000")
        for minutes in (3, 2, 1):
            candle(session, now - timedelta(minutes=minutes),
                   open_="74000", high="74001", low="73999", close="74000")
        account = service.seed_account(now)
        service.configure_fee_profile(rates(), now)
        account_id = account.id
    try:
        def attempt(index):
            with SessionLocal.begin() as session:
                service = PaperTradingService(session, risk=RiskConfig(), account_name=account_name)
                p = proposal("LONG", mark * D("0.95"), mark * D("1.05"), now + timedelta(minutes=5))
                service._allow_legacy_test_entries = True
                return service.open_position(p, mark, now, execution_key=f"parallel:{account_name}:{index}", now=now)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(6)))
        assert any(row["status"] == "ACCEPTED" for row in results)
        with SessionLocal() as session:
            snapshot = PaperTradingService(session, risk=RiskConfig(), account_name=account_name).account_snapshot(now)
        assert snapshot["open_position_count"] <= 5
        assert D(snapshot["reserved_margin"]) <= D(snapshot["equity_estimate"]) * D("0.60")
        assert D(snapshot["cash_balance"]) - D(snapshot["reserved_margin"]) >= D(snapshot["equity_estimate"]) * D("0.20")
    finally:
        with SessionLocal.begin() as session:
            for model in (PaperPositionEventORM, PaperAccountLedgerORM, PaperPositionORM,
                          PaperFeeProfileORM, PaperAccountORM):
                session.execute(delete(model).where(model.account_id == account_id) if model is not PaperAccountORM
                                else delete(model).where(model.id == account_id))
            session.execute(delete(CandleORM).where(
                CandleORM.symbol == Instrument.BTC_USD,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
                CandleORM.timestamp.in_([now - timedelta(minutes=m) for m in (3, 2, 1)]),
            ))


def test_auto_stop_wins_over_late_agent_close(paper):
    ready(paper)
    opened = open_one(paper, sl=D("95"))
    position_id = UUID(opened["position_id"])
    candle(paper.session, T0, high="101", low="94", close="96")
    candle(paper.session, T0 + timedelta(minutes=1), open_="93", high="95", low="92", close="94")
    paper.process_new_candles(T0 + timedelta(minutes=2))
    assert paper.session.get(PaperPositionORM, position_id).close_reason == "STOP_LOSS"
    late = decision_row(paper, T0 + timedelta(minutes=2), "WAIT",
                        actions=[{"position_id": position_id, "action": "CLOSE", "reason": "Spóźniony model"}])
    ticker = {"price": "94", "retrieved_at": (T0 + timedelta(minutes=2, seconds=1)).isoformat(),
              "source": "test_ticker"}
    result = paper.process_decision(late.id, ticker, T0 + timedelta(minutes=2, seconds=1))
    assert result["results"][0]["status"] == "REJECTED"
    closed = paper.session.scalars(select(PaperPositionEventORM).where(
        PaperPositionEventORM.position_id == position_id,
        PaperPositionEventORM.type == "CLOSED",
    )).all()
    assert len(closed) == 1


def test_pending_target_is_auto_closed_before_agent_close_action(paper):
    ready(paper)
    entered_at = T0 + timedelta(seconds=30)
    opened = paper.open_position(
        proposal("SHORT", D("105"), D("95")), D("100"), entered_at,
        execution_key="target-before-agent", now=entered_at,
    )
    position_id = UUID(opened["position_id"])
    candle(paper.session, T0, high="110", low="90", close="100")
    candle(paper.session, T0 + timedelta(minutes=1), high="101", low="94", close="96")
    decision_at = T0 + timedelta(minutes=2, seconds=5)
    decision = decision_row(paper, decision_at, "WAIT", actions=[{
        "position_id": position_id, "action": "CLOSE", "reason": "Agent widział TP",
    }])
    ticker_at = decision_at + timedelta(seconds=1)
    ticker = {"price": "100", "retrieved_at": ticker_at.isoformat(), "source": "test_ticker"}
    result = paper.process_decision(decision.id, ticker, ticker_at)
    position = paper.session.get(PaperPositionORM, position_id)
    assert position.close_reason == "TAKE_PROFIT"
    assert position.close_source == "1m_ohlc_barrier_assumption"
    assert position.close_price == position.take_profit
    assert result["results"][0]["status"] == "REJECTED"
    assert "closed" in result["results"][0]["reason"].lower()


def test_paper_session_keeps_one_position_and_auto_closes_another(paper):
    account = ready(paper)
    first = open_one(paper, key="target", sl=D("95"), tp=D("105"))
    second = open_one(paper, key="holding", sl=D("90"), tp=D("120"))
    assert first["status"] == second["status"] == "ACCEPTED"
    candle(paper.session, T0, high="101", low="99", close="100")
    candle(paper.session, T0 + timedelta(minutes=1), high="106", low="99", close="104")
    candle(paper.session, T0 + timedelta(minutes=2), open_="106", high="107", low="105", close="106")

    result = paper.process_new_candles(T0 + timedelta(minutes=3))
    assert result["closed"] == 1
    closed = paper.session.get(PaperPositionORM, UUID(first["position_id"]))
    holding = paper.session.get(PaperPositionORM, UUID(second["position_id"]))
    assert closed.close_reason == "TAKE_PROFIT"
    assert closed.realized_net_pnl == closed.realized_gross_pnl - closed.fee_total
    assert holding.status == "OPEN"
    assert paper.account_snapshot(T0 + timedelta(minutes=3))["open_position_count"] == 1
    ledger = paper.session.scalars(select(PaperAccountLedgerORM).where(
        PaperAccountLedgerORM.account_id == account.id)).all()
    assert sum((row.amount_usd for row in ledger), D("0")) == account.cash_balance
    assert sum((row.margin_delta_usd for row in ledger), D("0")) == holding.reserved_margin
    assert paper.process_new_candles(T0 + timedelta(minutes=3))["closed"] == 0


def test_historical_agent_close_can_be_auditably_corrected_to_first_tp(paper):
    account = ready(paper)
    opened = open_one(paper, "SHORT", "old-agent-close", sl=D("105"), tp=D("95"))
    position_id = UUID(opened["position_id"])
    candle(paper.session, T0, high="101", low="94", close="96")
    paper.close_position(position_id, "AGENT", D("100"), T0 + timedelta(minutes=2), source="test")
    position = paper.session.get(PaperPositionORM, position_id)
    old_net = position.realized_net_pnl
    old_cash = account.cash_balance
    with pytest.raises(ValueError, match="First barrier was TAKE_PROFIT"):
        paper.correct_closed_barrier_fill(position_id, "STOP_LOSS", T0 + timedelta(minutes=3))
    result = paper.correct_closed_barrier_fill(position_id, "TAKE_PROFIT", T0 + timedelta(minutes=3))
    assert result["status"] == "CORRECTED"
    assert position.close_reason == "TAKE_PROFIT"
    assert position.close_price == D("95")
    assert position.close_at == T0 + timedelta(minutes=1)
    assert position.realized_net_pnl == position.realized_gross_pnl - position.fee_total
    assert account.cash_balance == old_cash + position.realized_net_pnl - old_net
    assert paper.correct_closed_barrier_fill(position_id, "TAKE_PROFIT", T0 + timedelta(minutes=4))["status"] == "ALREADY_CORRECTED"
    events = paper.session.scalars(select(PaperPositionEventORM).where(
        PaperPositionEventORM.position_id == position_id,
        PaperPositionEventORM.type.in_(["CLOSED", "CORRECTED"]),
    )).all()
    assert {event.type for event in events} == {"CLOSED", "CORRECTED"}
    assert next(event for event in events if event.type == "CORRECTED").payload["old"]["realized_net_pnl"] == str(old_net)
    recent = paper.recent_events(5)
    assert any(event["type"] == "CORRECTED" and event["position_id"] == str(position_id) for event in recent)
    assert not any(event["type"] == "CLOSED" and event["position_id"] == str(position_id) for event in recent)
    ledger = paper.session.scalars(select(PaperAccountLedgerORM).where(
        PaperAccountLedgerORM.account_id == account.id)).all()
    assert sum((row.amount_usd for row in ledger), D("0")) == account.cash_balance
    assert len([row for row in ledger if row.event_type == "CORRECTION"]) == 1
