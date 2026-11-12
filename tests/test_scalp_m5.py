from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from database.connection import engine
from database.models.candle import CandleORM
from database.models.agent_decision import AgentDecisionORM
from database.models.paper import PaperPendingOrderORM, PaperPositionORM
from models.agent_decision import AgentDecision, OpenProposal, PendingOrderAction
from models.market import Instrument, Timeframe
from models.paper import PaperSide, RiskConfig, RiskEngine, ZERO, cfd20_rates, risk_reward_for_rates
from models.strategy import Strategy, validate_strategy_setup
from services.market_context import build_market_context
from services.paper_trading import PaperTradingService
from services.pending_orders import PendingOrderManager
from services.scalp_candidates import build_scalp_candidates


D = Decimal
AT = datetime(2046, 1, 1, 12, tzinfo=timezone.utc)


def test_scalp_m5_stop_uses_closed_m5_and_rejects_gap():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False)
    try:
        paper = PaperTradingService(session, risk=RiskConfig(min_stop_m15_atr_multiple=D("1")),
                                    account_name=f"scalp_{uuid4().hex}")
        for index in range(25):
            timestamp = AT - timedelta(minutes=(25-index)*5)
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.FIVE_MINUTES,
                timestamp=timestamp, open=D("72000"), high=D("72020"), low=D("71980"),
                close=D("72000"), vwap=D("72000"), volume=D("1"), trades=1))
        session.flush()
        assert paper.validate_stop_structure(PaperSide.LONG, D("72010"), D("71950"), AT,
                                             Strategy.SCALP_M5) is None
        assert "M15 ATR/swing" in paper.validate_stop_structure(
            PaperSide.LONG, D("72010"), D("71950"), AT, Strategy.INTRADAY_M15)
        for index in range(20):
            timestamp = AT-timedelta(minutes=(20-index)*15)
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.FIFTEEN_MINUTES,
                timestamp=timestamp, open=D("72000"), high=D("72100"), low=D("71900"),
                close=D("72000"), vwap=D("72000"), volume=D("1"), trades=1))
        session.flush()
        assert "SL too close for M15 structure" in paper.validate_stop_structure(
            PaperSide.LONG, D("72010"), D("71950"), AT, Strategy.INTRADAY_M15)
        assert paper.validate_stop_structure(PaperSide.LONG, D("72010"), D("71950"), AT,
                                             Strategy.SCALP_M5) is None
        assert "INVALID_SL_STRUCTURE" in paper.validate_stop_structure(
            PaperSide.LONG, D("72010"), D("71980"), AT, Strategy.SCALP_M5)
        assert paper.validate_stop_structure(PaperSide.SHORT, D("71990"), D("72050"), AT,
                                             Strategy.SCALP_M5) is None
        assert "ATR_DATA_GAP" in paper.validate_stop_structure(
            PaperSide.LONG, D("72010"), D("71950"), AT+timedelta(minutes=20), Strategy.SCALP_M5)
        context = build_market_context(session, AT)
        assert len(context["scalp_m5"]["candidates"]) == 6
        assert context["scalp_m5"]["candidates"][0]["strategy"] == "SCALP_M5"
        session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.FIVE_MINUTES,
            timestamp=AT, open=D("72000"), high=D("80000"), low=D("70000"),
            close=D("80000"), vwap=D("80000"), volume=D("1"), trades=1))
        session.flush()
        assert build_market_context(session, AT)["scalp_m5"] == context["scalp_m5"]
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_strategy_schema_is_explicit_and_old_decisions_still_parse():
    old = AgentDecision.model_validate({"decision": "WAIT", "open_proposal": None,
        "position_actions": [], "to_monitor": [], "text": "historyczne WAIT"})
    assert old.pending_order_actions == []
    proposal = OpenProposal(side="LONG", reason="test", valid_until=AT,
                            strategy="SCALP_M5", setup_type="M5_PULLBACK_RETEST")
    assert proposal.strategy is Strategy.SCALP_M5
    action = PendingOrderAction(action="CREATE", order_type="BUY_LIMIT",
        entry_level_usd=D("72010"), stop_loss=D("71950"), take_profit=D("72100"),
        expires_at=AT+timedelta(minutes=10), scenario_key="retest", setup_type="M5_PULLBACK_RETEST",
        strategy="SCALP_M5", reason="closed M5 retest")
    assert action.strategy is Strategy.SCALP_M5
    rr = risk_reward_for_rates(cfd20_rates(), PaperSide.LONG, D("72010"),
                               D("71950"), D("72100"), D("0.01"))
    assert rr["rr_ratio"] == D("1.5")
    assert validate_strategy_setup(Strategy.SCALP_M5, "unknown") == "UNAUTHORIZED_SCALP_SETUP"
    assert validate_strategy_setup(Strategy.INTRADAY_M15, "unknown") == "UNAUTHORIZED_INTRADAY_SETUP"


def test_scalp_risk_fraction_cannot_exceed_global_limit():
    config = RiskConfig(max_risk_per_position=D("0.03"), max_total_risk=D("0.09"),
                        max_position_margin=D("0.9"), max_total_margin=D("0.9"),
                        min_free_margin=D("0.01"), scalp_max_risk_per_position=D("0.01"))
    kwargs = dict(side=PaperSide.LONG, entry=D("100"), stop_loss=D("50"),
                  take_profit=D("200"), equity=D("1000"), cash=D("1000"),
                  reserved=ZERO, current_risk=ZERO, position_count=0, rates=cfd20_rates())
    global_quantity, _ = RiskEngine(config).size(**kwargs)
    scalp_quantity, _ = RiskEngine(config).size(**kwargs,
        risk_fraction_override=config.scalp_max_risk_per_position)
    assert global_quantity is not None and scalp_quantity is not None
    assert scalp_quantity < global_quantity
    assert scalp_quantity * D("50") <= D("10")


def test_pending_limit_has_its_own_structural_stop_and_rr():
    bars = [{"timestamp": (AT-timedelta(minutes=(10-index)*5)).isoformat(),
             "open": "72000", "high": "72090", "low": "71890", "close": "72000"}
            for index in range(10)]
    empty_structure = {"channel_high_20": None, "channel_low_20": None}
    context = {"as_of": AT.isoformat(), "timeframes": {
        "5m": {"last_closed_at": AT.isoformat(), "consecutive_count": 25,
               "recent_closed_candles": bars, "market_structure": {"atr14": "200"}},
        "15m": {"recent_closed_candles": [], "market_structure": empty_structure},
        "1h": {"recent_closed_candles": [], "market_structure": empty_structure},
    }}
    candidates = build_scalp_candidates(context)["candidates"]
    candidate = next(item for item in candidates if item["setup_type"] == "M5_RANGE_REJECTION"
                     and item["direction"] == "LONG")
    assert candidate["reason_no_trade"] == "RR_LOW"
    assert candidate["pending_eligible"] is True
    assert D(candidate["pending_stop_loss"]) <= D(candidate["pending_entry_level"])-D("120")
    assert D(candidate["pending_rr_after_costs"]) >= D("1.5")


def test_scalp_pending_create_amend_fill_keeps_strategy_and_rr():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False)
    try:
        paper = PaperTradingService(session, risk=RiskConfig(min_stop_m15_atr_multiple=D("1")),
                                    account_name=f"scp_{uuid4().hex}")
        paper.seed_account(AT-timedelta(hours=3))
        paper.configure_fee_profile(cfd20_rates(), AT-timedelta(hours=2))
        for index in range(25):
            at = AT-timedelta(minutes=(25-index)*5)
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.FIVE_MINUTES,
                timestamp=at, open=D("72000"), high=D("72020"), low=D("71980"),
                close=D("72000"), vwap=D("72000"), volume=D("1"), trades=1))
        for minutes in (3, 2, 1):
            at = AT-timedelta(minutes=minutes)
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.ONE_MINUTE,
                timestamp=at, open=D("72000"), high=D("72001"), low=D("71999"),
                close=D("72000"), vwap=D("72000"), volume=D("1"), trades=1))
        decision = AgentDecisionORM(id=uuid4(), instrument=Instrument.BTC_USD.value,
            candle_timestamp=AT-timedelta(minutes=5), created_at=AT+timedelta(seconds=20),
            decision="WAIT", text="pending scalp", model="test", position_actions=[],
            pending_order_actions=[], paper_status="DONE")
        session.add(decision)
        session.flush()
        manager = PendingOrderManager(session, paper)
        quote = {"price": "72000", "bid": "71999", "ask": "72001",
                 "retrieved_at": (AT+timedelta(seconds=25)).isoformat()}
        action = PendingOrderAction(action="CREATE", order_type="BUY_LIMIT",
            entry_level_usd=D("71990"), stop_loss=D("71950"), take_profit=D("72100"),
            expires_at=AT+timedelta(minutes=15), scenario_key="m5_retest",
            setup_type="M5_PULLBACK_RETEST", strategy="SCALP_M5", reason="M5 retest")
        created = manager.apply_action(action, decision.id, quote, AT+timedelta(seconds=25))
        assert created["status"] == "PENDING", created
        order = session.get(PaperPendingOrderORM, created["order_id"])
        assert order.strategy == "SCALP_M5" and order.strategy_parameters["version"] == "SCALP_M5_V1"
        decision2 = AgentDecisionORM(id=uuid4(), instrument=Instrument.BTC_USD.value,
            candle_timestamp=AT, created_at=AT+timedelta(minutes=1, seconds=20),
            decision="WAIT", text="amend", model="test", position_actions=[],
            pending_order_actions=[], analysis_trace={"paper_context": {"pending_orders": manager.snapshot()}},
            paper_status="DONE")
        session.add(decision2)
        session.flush()
        amend = PendingOrderAction(action="AMEND", order_id=order.id, expected_revision=1,
            entry_level_usd=D("71985"), strategy="SCALP_M5", reason="deeper retest")
        fresh_quote = {**quote, "retrieved_at": (AT+timedelta(minutes=1, seconds=25)).isoformat()}
        assert manager.apply_action(amend, decision2.id, fresh_quote,
                                    AT+timedelta(minutes=1, seconds=25))["status"] == "PENDING"
        for minute, low in ((0, "71999"), (1, "71999"), (2, "71960"), (3, "71999")):
            at = AT+timedelta(minutes=minute)
            session.add(CandleORM(symbol=Instrument.BTC_USD, timeframe=Timeframe.ONE_MINUTE,
                timestamp=at, open=D("72000"), high=D("72001"), low=D(low),
                close=D("72000"), vwap=D("72000"), volume=D("1"), trades=1))
        session.flush()
        assert manager.process(AT+timedelta(minutes=4, seconds=5))["filled"] == 1
        assert order.status == "FILLED" and D(order.trigger_trace["rr_ratio"]) >= D("1.5")
        position = session.get(PaperPositionORM, order.position_id)
        assert position.strategy == "SCALP_M5"
        assert position.setup_type == "M5_PULLBACK_RETEST"
        assert position.strategy_parameters["max_hold_minutes"] == 60
    finally:
        session.close()
        transaction.rollback()
        connection.close()
