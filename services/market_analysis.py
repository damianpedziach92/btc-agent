import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import select

from agents.market_analyst import MarketAnalyst
from agents.market_tools import MarketTools
from database.connection import SessionLocal
from database.models.agent_decision import AgentDecisionORM
from models.agent_decision import AgentDecision
from models.market import Instrument, Timeframe
from repositories.agent_memory_repository import AgentMemoryRepository
from repositories.candle_repository import CandleRepository
from services.paper_orchestration import process_pending_decisions
from services.paper_trading import PaperTradingService
from services.pending_orders import PendingOrderManager
from services.market_context import build_market_context
from agents.market_analyst import INSTRUCTIONS_HASH


DEFAULT_MODEL = "gpt-6-luna"
MAX_CANDLE_AGE = timedelta(minutes=2)


def run_market_analysis(force: bool = False, preview: bool = False) -> AgentDecision | None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY in .env to enable the market analyst")

    instrument = Instrument.BTC_USD
    with SessionLocal() as session:
        latest = CandleRepository(session).get_latest(
            instrument, Timeframe.FIVE_MINUTES, 1
        )
        if not latest:
            return None
        candle_timestamp = latest[0].timestamp
        candle_end = candle_timestamp.astimezone(timezone.utc) + timedelta(minutes=5)
        candle_age = datetime.now(timezone.utc) - candle_end
        if candle_age < timedelta(0) or (not (force or preview) and candle_age > MAX_CANDLE_AGE):
            return None
        memory = AgentMemoryRepository(session)
        if not preview and memory.exists(instrument, candle_timestamp):
            return None
        active = memory.load_active(instrument)
        paper = PaperTradingService(session)
        paper_context = {
            "account": paper.account_snapshot(),
            "positions": paper.open_positions_snapshot(),
            "pending_orders": PendingOrderManager(session, paper).snapshot(),
            "recent_events": paper.recent_events(),
        }
        if not preview:
            session.commit()  # persist deterministic expiry before model I/O

    tools = MarketTools()
    try:
        ticker = tools.get_price(instrument)
    except Exception as exc:
        ticker = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
    with SessionLocal() as session:
        market_context = build_market_context(session, datetime.now(timezone.utc), ticker)

    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    with OpenAI(api_key=api_key, timeout=45, max_retries=1) as client:
        analyst = MarketAnalyst(client, tools, model)
        decision = analyst.analyze(active, paper_context, market_context)

    if preview:
        return decision

    with SessionLocal.begin() as session:
        saved = AgentMemoryRepository(session).save(
            instrument, candle_timestamp, analyst.last_model, decision, active,
            analysis_trace={"paper_context": paper_context, "market_context": market_context,
                            "strategy_diagnostics": {
                                "scalp_m5": market_context.get("scalp_m5", {}).get("reason_counts", {}),
                                "scalp_m5_candidates": len(market_context.get("scalp_m5", {}).get("candidates", [])),
                                "scalp_m5_pending_eligible": sum(1 for item in
                                    market_context.get("scalp_m5", {}).get("candidates", [])
                                    if item.get("pending_eligible")),
                                "intraday_m15_submitted_candidates": (
                                    int(bool(decision.open_proposal and decision.open_proposal.strategy and
                                             decision.open_proposal.strategy.value == "INTRADAY_M15")) +
                                    sum(1 for action in decision.pending_order_actions if
                                        action.action.value == "CREATE" and action.strategy and
                                        action.strategy.value == "INTRADAY_M15")),
                                "decision_strategy": (decision.open_proposal.strategy.value
                                    if decision.open_proposal and decision.open_proposal.strategy else None),
                                "pending_by_strategy": {
                                    strategy: sum(1 for action in decision.pending_order_actions
                                                  if action.action.value == "CREATE" and action.strategy and
                                                  action.strategy.value == strategy)
                                    for strategy in ("INTRADAY_M15", "SCALP_M5")},
                                "wait_reason_codes": ([key for key, count in
                                    market_context.get("scalp_m5", {}).get("reason_counts", {}).items()
                                    if key != "ELIGIBLE" and count] +
                                    (["MODEL_WAIT_WITH_ELIGIBLE_CANDIDATE"] if
                                     market_context.get("scalp_m5", {}).get("reason_counts", {}).get("ELIGIBLE") else []) +
                                    (["PENDING_ELIGIBLE_NOT_SELECTED"] if any(item.get("pending_eligible") for item in
                                      market_context.get("scalp_m5", {}).get("candidates", [])) and
                                      not any(action.action.value == "CREATE" for action in decision.pending_order_actions) else []) +
                                    (["MARGIN_CAP"] if paper_context.get("account") and
                                     Decimal(str(paper_context["account"].get("available_margin", "999999"))) < Decimal("10") else []) +
                                    (["DUPLICATE_SCENARIO"] if paper_context.get("pending_orders") else [])
                                    if decision.decision.value == "WAIT" else []),
                            },
                            "market_context_sha256": market_context["sha256"],
                            "trigger_timeframe": Timeframe.FIVE_MINUTES.value,
                            "trigger_candle_end": candle_end.isoformat(),
                            "instructions_hash": INSTRUCTIONS_HASH,
                            "input_chars": analyst.input_chars,
                            "token_usage": analyst.token_usage,
                            "tool_budget_exceeded": any(call.get("status") == "BUDGET_EXCEEDED"
                                                        for call in analyst.tool_trace),
                            "validation_repair_count": analyst.validation_repair_count,
                            "tool_calls": analyst.tool_trace},
        )
        if saved:
            decision_id = session.scalar(select(AgentDecisionORM.id).where(
                AgentDecisionORM.instrument == instrument.value,
                AgentDecisionORM.candle_timestamp == candle_timestamp,
            ))
            if decision_id is None:
                raise RuntimeError("Saved agent decision could not be reloaded")
    if saved:
        process_pending_decisions()
    return decision if saved else None
