import logging

from sqlalchemy import select

from agents.market_tools import MarketTools
from database.connection import SessionLocal
from database.models.agent_decision import AgentDecisionORM
from models.market import Instrument
from services.paper_trading import PaperTradingService


logger = logging.getLogger(__name__)


def process_pending_decisions() -> list[dict]:
    """Replay saved paper commands after a crash; each decision is idempotent."""
    with SessionLocal() as session:
        pending = list(session.scalars(select(AgentDecisionORM).where(
            AgentDecisionORM.paper_status == "PENDING",
        ).order_by(AgentDecisionORM.created_at).limit(20)))
    results = []
    for decision in pending:
        needs_price = decision.open_proposal is not None or any(
            item.get("action") == "CLOSE" for item in decision.position_actions or []
        )
        ticker = MarketTools().get_price(Instrument.BTC_USD) if needs_price else None
        with SessionLocal.begin() as session:
            result = PaperTradingService(session).process_decision(decision.id, ticker)
        logger.info("Paper decision %s: %s", decision.id, result)
        results.append(result)
    return results


def process_new_paper_candles() -> dict:
    # TP/SL barrier fills require no ticker, so process them before any HTTP I/O.
    with SessionLocal.begin() as session:
        result = PaperTradingService(session).process_new_candles()
    if result["pending_price"]:
        try:
            ticker = MarketTools().get_price(Instrument.BTC_USD)
        except Exception as exc:
            logger.warning("Post-close ticker unavailable for other paper exits: %s", type(exc).__name__)
        else:
            with SessionLocal.begin() as session:
                retried = PaperTradingService(session).process_new_candles(ticker=ticker)
            result = {"evaluated": result["evaluated"] + retried["evaluated"],
                      "closed": result["closed"] + retried["closed"],
                      "pending_price": retried["pending_price"]}
    if result["evaluated"] or result["closed"] or result["pending_price"]:
        logger.info("Paper 1m processing: %s", result)
    return result
