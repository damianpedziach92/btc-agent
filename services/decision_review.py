"""Two-hour, read-only review of saved market decisions and PAPER outcomes."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from agents.decision_reviewer import DecisionReviewer, INSTRUCTIONS_HASH
from database.connection import SessionLocal
from database.models.agent_decision import AgentDecisionORM
from database.models.candle import CandleORM
from database.models.decision_review import DecisionReviewORM
from database.models.paper import PaperPositionORM
from models.decision_review import DecisionReview, ReviewAssessment
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository
from services.indicators import calculate_indicators
from services.telegram_notifications import TelegramConfig


WINDOW = timedelta(hours=2)
REVIEW_DELAY = timedelta(minutes=2)
INDICATOR_WARMUP = 200
DEFAULT_MODEL = "gpt-6-luna"


def latest_closed_window_end(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    return now.replace(hour=now.hour - now.hour % 2, minute=0, second=0, microsecond=0)


def _check_window_end(window_end: datetime) -> datetime:
    if window_end.tzinfo is None or window_end.utcoffset() != timedelta(0):
        raise ValueError("Window end must be timezone-aware UTC")
    if window_end.hour % 2 or window_end.minute or window_end.second or window_end.microsecond:
        raise ValueError("Window end must be aligned to an even UTC hour")
    return window_end


def _candle_summary(row: CandleORM) -> dict:
    return {
        "timestamp": row.timestamp.isoformat(),
        "open": str(row.open), "high": str(row.high),
        "low": str(row.low), "close": str(row.close),
        "volume": str(row.volume),
    }


def _position_summary(row: PaperPositionORM, window_end: datetime) -> dict:
    closed_in_window = row.close_at is not None and row.close_at <= window_end
    return {
        "id": str(row.id),
        "agent_decision_id": str(row.agent_decision_id) if row.agent_decision_id else None,
        "side": row.side, "entry_at": row.entry_at.isoformat(),
        "entry_price": str(row.entry_price), "quantity_btc": str(row.quantity_btc),
        "stop_loss": str(row.stop_loss) if row.stop_loss is not None else None,
        "take_profit": str(row.take_profit) if row.take_profit is not None else None,
        "initial_costs": str(row.initial_costs),
        "fee_snapshot": row.fee_snapshot,
        "status_at_window_end": row.status if closed_in_window else "OPEN_AT_WINDOW_END",
        "close_at": row.close_at.isoformat() if closed_in_window else None,
        "close_reason": row.close_reason if closed_in_window else None,
        "close_price": str(row.close_price) if closed_in_window else None,
        "realized_net_pnl": str(row.realized_net_pnl) if closed_in_window else None,
    }


def build_review_context(session: Session, window_end: datetime) -> dict:
    """Reconstruct information available at each decision without future candles."""
    window_end = _check_window_end(window_end)
    window_start = window_end - WINDOW
    decisions = list(session.scalars(select(AgentDecisionORM).where(
        AgentDecisionORM.instrument == Instrument.BTC_USD.value,
        AgentDecisionORM.created_at >= window_start,
        AgentDecisionORM.created_at < window_end,
    ).order_by(AgentDecisionORM.created_at, AgentDecisionORM.id)))
    context = {
        "instrument": Instrument.BTC_USD.value,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "data_policy": (
            "Candles and indicators are reconstructed from stored closed candles. "
            "known_at_decision excludes still-open candles; price ticker/tool transcripts "
            "were not stored and cannot be verified exactly. Candle and position outcomes "
            "stop at window_end; PAPER execution records reflect the currently stored result."
        ),
        "decisions": [], "positions": [],
    }
    if not decisions:
        return context

    earliest = decisions[0].created_at
    candles_by_timeframe = {}
    for timeframe in (Timeframe.ONE_MINUTE, Timeframe.FIFTEEN_MINUTES, Timeframe.ONE_HOUR):
        step = timedelta(minutes=timeframe.minutes)
        candles_by_timeframe[timeframe] = list(session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == timeframe,
            CandleORM.timestamp >= earliest - step * (INDICATOR_WARMUP + 1),
            CandleORM.timestamp <= window_end - step,
        ).order_by(CandleORM.timestamp)))

    for decision in decisions:
        known = {}
        for timeframe, rows in candles_by_timeframe.items():
            step = timedelta(minutes=timeframe.minutes)
            eligible = [row for row in rows if row.timestamp + step <= decision.created_at]
            source = [CandleRepository._to_candle(row) for row in eligible[-INDICATOR_WARMUP:]]
            indicator = calculate_indicators(source)
            known[timeframe.value] = {
                "recent_closed_candles": [_candle_summary(row) for row in eligible[-5:]],
                "indicators_at_last_close": indicator.model_dump(mode="json") if indicator else None,
            }
        after_15m = next((row for row in candles_by_timeframe[Timeframe.FIFTEEN_MINUTES]
                          if decision.created_at < row.timestamp + timedelta(minutes=15) <= window_end), None)
        after_60m = next((row for row in candles_by_timeframe[Timeframe.ONE_MINUTE]
                          if decision.created_at + timedelta(hours=1) <= row.timestamp + timedelta(minutes=1)
                          <= window_end), None)
        context["decisions"].append({
            "id": str(decision.id), "created_at": decision.created_at.isoformat(),
            "decision": decision.decision, "text": decision.text[:800],
            "open_proposal": decision.open_proposal,
            "position_actions": decision.position_actions or [],
            "paper_status": decision.paper_status,
            "paper_result": decision.paper_result,
            "known_at_decision": known,
            "observed_after_decision": {
                "next_closed_15m": _candle_summary(after_15m) if after_15m else None,
                "first_1m_close_at_or_after_60m": _candle_summary(after_60m) if after_60m else None,
            },
        })

    decision_ids = [row.id for row in decisions]
    action_ids = {UUID(action["position_id"]) for row in decisions
                  for action in row.position_actions or [] if action.get("position_id")}
    conditions = [PaperPositionORM.agent_decision_id.in_(decision_ids),
                  and_(PaperPositionORM.close_at >= window_start,
                       PaperPositionORM.close_at <= window_end)]
    if action_ids:
        conditions.append(PaperPositionORM.id.in_(action_ids))
    positions = session.scalars(select(PaperPositionORM).where(
        or_(*conditions), PaperPositionORM.entry_at < window_end,
    ).order_by(PaperPositionORM.entry_at)).all()
    context["positions"] = [_position_summary(row, window_end) for row in positions]
    return context


def _result(row: DecisionReviewORM, status: str) -> dict:
    return {
        "status": status, "id": str(row.id),
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "reviewer_model": row.reviewer_model,
        "decision_count": len(row.decision_ids),
        "assessment": row.assessment, "summary": row.summary,
        "findings": row.findings, "data_gaps": row.data_gaps,
        "notification_status": row.notification_status,
    }


def run_decision_review(
    *, window_end: datetime | None = None, preview: bool = False,
    now: datetime | None = None, client=None,
) -> dict:
    load_dotenv()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    window_end = _check_window_end(window_end or latest_closed_window_end(now))
    if now < window_end + REVIEW_DELAY:
        return {"status": "NOT_READY", "window_end": window_end.isoformat()}
    with SessionLocal() as session:
        if not preview:
            existing = session.scalar(select(DecisionReviewORM).where(
                DecisionReviewORM.instrument == Instrument.BTC_USD.value,
                DecisionReviewORM.window_end == window_end,
            ))
            if existing:
                return _result(existing, "EXISTS")
        context = build_review_context(session, window_end)

    if context["decisions"]:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key and client is None:
            raise RuntimeError("Set OPENAI_API_KEY to run the decision reviewer")
        model = os.getenv("DECISION_REVIEW_MODEL") or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        if client is None:
            with OpenAI(api_key=api_key, timeout=180, max_retries=0) as owned_client:
                reviewer = DecisionReviewer(owned_client, model)
                review = reviewer.analyze(context)
        else:
            reviewer = DecisionReviewer(client, model)
            review = reviewer.analyze(context)
        reviewer_model = reviewer.last_model
    else:
        review = DecisionReview(
            assessment=ReviewAssessment.NO_DECISIONS,
            summary="Brak zapisanych decyzji agenta w zakończonym oknie dwóch godzin.",
            findings=[], data_gaps=[],
        )
        reviewer_model = "none"

    if preview:
        return {"status": "PREVIEW", "window_end": window_end.isoformat(),
                "decision_count": len(context["decisions"]),
                "reviewer_model": reviewer_model,
                "review": review.model_dump(mode="json")}

    with SessionLocal.begin() as session:
        review_id = session.execute(insert(DecisionReviewORM).values(
            id=uuid4(), instrument=Instrument.BTC_USD.value,
            window_start=window_end - WINDOW, window_end=window_end,
            created_at=now, reviewer_model=reviewer_model,
            instructions_hash=INSTRUCTIONS_HASH,
            assessment=review.assessment.value, summary=review.summary,
            findings=[finding.model_dump(mode="json") for finding in review.findings],
            data_gaps=review.data_gaps,
            decision_ids=[item["id"] for item in context["decisions"]],
            input_snapshot=context,
            notification_status="PENDING" if TelegramConfig.from_env() else "DISABLED",
            notification_attempts=0, notified_at=None, notification_error=None,
        ).on_conflict_do_nothing(constraint="uq_decision_reviews_instrument_window")
            .returning(DecisionReviewORM.id)).scalar()
        row = session.get(DecisionReviewORM, review_id) if review_id else session.scalar(
            select(DecisionReviewORM).where(
                DecisionReviewORM.instrument == Instrument.BTC_USD.value,
                DecisionReviewORM.window_end == window_end,
            )
        )
        return _result(row, "CREATED" if review_id else "EXISTS")
