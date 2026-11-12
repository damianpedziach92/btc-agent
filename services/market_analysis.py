import os
from datetime import datetime, timedelta, timezone

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
from services.telegram_notifications import enqueue_decision_notification, process_pending_notifications


DEFAULT_MODEL = "gpt-6-luna"
MAX_CANDLE_AGE = timedelta(minutes=5)


def run_market_analysis(force: bool = False, preview: bool = False) -> AgentDecision | None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY in .env to enable the market analyst")

    instrument = Instrument.BTC_USD
    with SessionLocal() as session:
        latest = CandleRepository(session).get_latest(
            instrument, Timeframe.FIFTEEN_MINUTES, 1
        )
        if not latest:
            return None
        candle_timestamp = latest[0].timestamp
        candle_end = candle_timestamp.astimezone(timezone.utc) + timedelta(minutes=15)
        if not (force or preview) and not timedelta(0) <= datetime.now(timezone.utc) - candle_end <= MAX_CANDLE_AGE:
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
                            "market_context_sha256": market_context["sha256"],
                            "instructions_hash": INSTRUCTIONS_HASH,
                            "tool_calls": analyst.tool_trace},
        )
        if saved:
            decision_id = session.scalar(select(AgentDecisionORM.id).where(
                AgentDecisionORM.instrument == instrument.value,
                AgentDecisionORM.candle_timestamp == candle_timestamp,
            ))
            if decision_id is None:
                raise RuntimeError("Saved agent decision could not be reloaded")
            enqueue_decision_notification(session, decision_id)
    if saved:
        process_pending_decisions()
        process_pending_notifications()
    return decision if saved else None
