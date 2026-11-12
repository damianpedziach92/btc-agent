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
from database.models.paper import PaperPositionEventORM, PaperPositionORM, PaperPendingOrderORM, PaperOrderEventORM
from models.decision_review import DecisionReview, ReviewAssessment
from models.market import Instrument, Timeframe
from repositories.candle_repository import CandleRepository
from services.indicators import calculate_indicators
from services.market_structure import calculate_market_structure
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
    closed_in_window = row.close_at is not None and row.close_at < window_end
    return {
        "id": str(row.id),
        "agent_decision_id": str(row.agent_decision_id) if row.agent_decision_id else None,
        "side": row.side, "entry_at": row.entry_at.isoformat(),
        "entry_price": str(row.entry_price), "quantity_btc": str(row.quantity_btc),
        "entry_reason": row.entry_reason[:800],
        "entry_notional": str(row.entry_notional),
        "stop_loss": str(row.stop_loss) if row.stop_loss is not None else None,
        "take_profit": str(row.take_profit) if row.take_profit is not None else None,
        "initial_costs": str(row.initial_costs),
        "fee_snapshot": row.fee_snapshot,
        "status_at_window_end": row.status if closed_in_window else "OPEN_AT_WINDOW_END",
        "close_at": row.close_at.isoformat() if closed_in_window else None,
        "close_reason": row.close_reason if closed_in_window else None,
        "close_reason_text": row.close_reason_text if closed_in_window else None,
        "close_source": row.close_source if closed_in_window else None,
        "close_price": str(row.close_price) if closed_in_window else None,
        "realized_gross_pnl": str(row.realized_gross_pnl) if closed_in_window else None,
        "realized_net_pnl": str(row.realized_net_pnl) if closed_in_window else None,
        "fee_total": str(row.fee_total) if closed_in_window else None,
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
            "known_at_decision excludes still-open candles. Historical decisions lack tool "
            "traces; new decisions store tool-call metadata and observed ticker quotes. "
            "Reconstructed market data may not exactly match what the analyst fetched. "
            "Candle and position outcomes "
            "stop at window_end; PAPER execution records reflect the currently stored result."
        ),
        "decisions": [], "positions": [], "paper_events": [], "orders": [], "order_events": [], "actions": [],
    }
    earliest = decisions[0].created_at if decisions else window_start
    candles_by_timeframe = {}
    for timeframe in (Timeframe.ONE_MINUTE, Timeframe.FIVE_MINUTES,
                      Timeframe.FIFTEEN_MINUTES, Timeframe.ONE_HOUR):
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
                "market_structure": calculate_market_structure(source),
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
            "pending_order_actions": decision.pending_order_actions or [],
            "paper_status": decision.paper_status,
            "paper_result": decision.paper_result,
            "analysis_trace": decision.analysis_trace,
            "known_at_decision": known,
            "observed_after_decision": {
                "next_closed_15m": _candle_summary(after_15m) if after_15m else None,
                "first_1m_close_at_or_after_60m": _candle_summary(after_60m) if after_60m else None,
            },
        })

    decision_ids = [row.id for row in decisions]
    action_position_ids = {UUID(action["position_id"]) for row in decisions
                           for action in row.position_actions or [] if action.get("position_id")}
    conditions = [PaperPositionORM.agent_decision_id.in_(decision_ids),
                  and_(PaperPositionORM.close_at >= window_start,
                       PaperPositionORM.close_at < window_end)]
    if action_position_ids:
        conditions.append(PaperPositionORM.id.in_(action_position_ids))
    positions = session.scalars(select(PaperPositionORM).where(
        or_(*conditions), PaperPositionORM.entry_at < window_end,
    ).order_by(PaperPositionORM.entry_at)).all()
    position_ids = [row.id for row in positions]
    events = list(session.scalars(select(PaperPositionEventORM).where(
        or_(PaperPositionEventORM.position_id.in_(position_ids),
            PaperPositionEventORM.decision_id.in_(decision_ids)),
        PaperPositionEventORM.event_time < window_end,
    ).order_by(PaperPositionEventORM.event_time)))
    event_summaries = [{
        "type": event.type,
        "event_time": event.event_time.isoformat(),
        "position_id": str(event.position_id) if event.position_id else None,
        "decision_id": str(event.decision_id) if event.decision_id else None,
        "payload": event.payload,
    } for event in events]
    context["paper_events"] = event_summaries
    context["positions"] = [_position_summary(row, window_end) for row in positions]
    orders = list(session.scalars(select(PaperPendingOrderORM).where(
        or_(PaperPendingOrderORM.created_by_decision_id.in_(decision_ids),
            and_(PaperPendingOrderORM.filled_at >= window_start, PaperPendingOrderORM.filled_at < window_end),
            and_(PaperPendingOrderORM.updated_at >= window_start, PaperPendingOrderORM.updated_at < window_end)),
    ).order_by(PaperPendingOrderORM.created_at, PaperPendingOrderORM.id)))
    linked_position_ids = [row.position_id for row in orders if row.position_id]
    missing_position_ids = [position_id for position_id in linked_position_ids if position_id not in position_ids]
    if missing_position_ids:
        linked_positions = list(session.scalars(select(PaperPositionORM).where(
            PaperPositionORM.id.in_(missing_position_ids),
            PaperPositionORM.entry_at < window_end,
        )))
        positions.extend(linked_positions)
        context["positions"] = [_position_summary(row, window_end) for row in positions]
        linked_events = list(session.scalars(select(PaperPositionEventORM).where(
            PaperPositionEventORM.position_id.in_(missing_position_ids),
            PaperPositionEventORM.event_time < window_end,
        ).order_by(PaperPositionEventORM.event_time)))
        linked_summaries = [{
            "type": event.type, "event_time": event.event_time.isoformat(),
            "position_id": str(event.position_id),
            "decision_id": str(event.decision_id) if event.decision_id else None,
            "payload": event.payload,
        } for event in linked_events]
        event_summaries.extend(linked_summaries)
        context["paper_events"].extend(linked_summaries)
    order_ids = [row.id for row in orders]
    all_order_events = list(session.scalars(select(PaperOrderEventORM).where(
        PaperOrderEventORM.order_id.in_(order_ids),
        PaperOrderEventORM.event_time < window_end,
    ).order_by(PaperOrderEventORM.event_time, PaperOrderEventORM.id)))
    order_events = [event for event in all_order_events if event.event_time >= window_start]
    events_by_order = {}
    for event in all_order_events:
        events_by_order.setdefault(event.order_id, []).append(event)
    context["orders"] = [{
        "id": str(row.id),
        "status_at_window_end": ({"FILLED": "FILLED", "EXPIRED": "EXPIRED",
                                  "CANCELLED": "CANCELLED", "REJECTED_RISK_AT_TRIGGER": "REJECTED",
                                  "MISSED": "MISSED"}.get(events_by_order[row.id][-1].type, "PENDING")
                                 if events_by_order.get(row.id) else "PENDING"),
        "order_type": next((event.payload["order_type"] for event in reversed(events_by_order.get(row.id, []))
                            if "order_type" in event.payload), row.order_type),
        "side": row.side,
        "entry_level_usd": next((event.payload["entry_level_usd"] for event in reversed(events_by_order.get(row.id, []))
                                 if "entry_level_usd" in event.payload), str(row.entry_level_usd)),
        "stop_loss": next((event.payload["stop_loss"] for event in reversed(events_by_order.get(row.id, []))
                           if "stop_loss" in event.payload), str(row.stop_loss)),
        "take_profit": next((event.payload["take_profit"] for event in reversed(events_by_order.get(row.id, []))
                             if "take_profit" in event.payload), str(row.take_profit)),
        "scenario_key": row.scenario_key, "setup_type": row.setup_type,
        "reason": next((event.payload["reason"] for event in reversed(events_by_order.get(row.id, []))
                        if event.type in {"CREATED", "AMENDED"} and "reason" in event.payload), row.reason[:800]),
        "created_at": row.created_at.isoformat(),
        "expires_at": next((event.payload["expires_at"] for event in reversed(events_by_order.get(row.id, []))
                            if "expires_at" in event.payload), row.expires_at.isoformat()),
        "position_id": str(row.position_id) if row.position_id and row.filled_at and row.filled_at < window_end else None,
        "fee_snapshot": row.fee_snapshot,
    } for row in orders]
    context["order_events"] = [{
        "id": str(event.id), "order_id": str(event.order_id), "type": event.type,
        "event_time": event.event_time.isoformat(), "payload": event.payload,
        "decision_id": str(event.decision_id) if event.decision_id else None,
    } for event in order_events]
    positions_by_decision = {str(row.agent_decision_id): row for row in positions if row.agent_decision_id}
    for decision in decisions:
        decision_id = str(decision.id)
        results = (decision.paper_result or {}).get("results", [])
        if decision.open_proposal:
            opened = positions_by_decision.get(decision_id)
            context["actions"].append({
                "id": f"decision:{decision_id}:open",
                "type": "OPEN_LONG" if decision.open_proposal["side"] == "LONG" else "OPEN_SHORT",
                "at": decision.created_at.isoformat(), "decision_id": decision_id,
                "position_id": str(opened.id) if opened else None,
                "execution_result": results[-1] if results else decision.paper_result,
            })
        for item in decision.position_actions or []:
            position_id = item["position_id"]
            result = next((result for result in results if result.get("position_id") == position_id), None)
            context["actions"].append({
                "id": f"decision:{decision_id}:{item['action'].lower()}:{position_id}",
                "type": item["action"], "at": decision.created_at.isoformat(),
                "decision_id": decision_id, "position_id": position_id,
                "reason": item.get("reason"),
                "execution_result": result or ({"status": "HELD"} if item["action"] == "HOLD" else decision.paper_result),
            })
        pending_result_offset = sum(item["action"] != "HOLD" for item in decision.position_actions or []) + bool(decision.open_proposal)
        for index, item in enumerate(decision.pending_order_actions or []):
            action_name = item["action"]
            result = results[pending_result_offset + index] if len(results) > pending_result_offset + index else None
            order_id = item.get("order_id") or (result or {}).get("order_id")
            context["actions"].append({
                "id": f"decision:{decision_id}:pending:{index}",
                "type": f"ORDER_{action_name}", "at": decision.created_at.isoformat(),
                "decision_id": decision_id, "order_id": order_id,
                "reason": item.get("reason"), "levels": item,
                "execution_result": result,
            })
    for event in context["order_events"]:
        action_type = {"FILLED": "ORDER_FILL", "EXPIRED": "ORDER_EXPIRE",
                       "REJECTED_RISK_AT_TRIGGER": "ORDER_REJECT", "MISSED": "ORDER_MISSED"}.get(event["type"])
        if action_type:
            context["actions"].append({
                "id": f"order:{event['order_id']}:{event['id']}", "type": action_type,
                "at": event["event_time"], "decision_id": event["decision_id"],
                "order_id": event["order_id"], "execution_result": event,
            })
    for position in positions:
        if (position.close_at is None or not window_start <= position.close_at < window_end
            or position.close_reason == "AGENT"):
            continue
        close_event = next((event for event in reversed(event_summaries)
                            if event["position_id"] == str(position.id)
                            and event["type"] == "CLOSED"), None)
        trigger_candle = None
        if close_event and close_event["payload"].get("theoretical_touch_at"):
            touch_at = datetime.fromisoformat(close_event["payload"]["theoretical_touch_at"])
            trigger_candle = session.scalar(select(CandleORM).where(
                CandleORM.symbol == Instrument.BTC_USD,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
                CandleORM.timestamp == touch_at - timedelta(minutes=1),
            ))
        context["actions"].append({
            "id": f"position:{position.id}:auto_close", "type": "AUTO_CLOSE",
            "at": position.close_at.isoformat(),
            "decision_id": str(position.agent_decision_id) if position.agent_decision_id else None,
            "position_id": str(position.id),
            "execution_result": close_event,
            "trigger_candle": _candle_summary(trigger_candle) if trigger_candle else None,
        })
    context["actions"].sort(key=lambda item: (item["at"], item["id"]))
    return context


def _result(row: DecisionReviewORM, status: str) -> dict:
    return {
        "status": status, "id": str(row.id),
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "reviewer_model": row.reviewer_model,
        "decision_count": len(row.decision_ids),
        "assessment": row.assessment, "summary": row.summary,
        "action_reviews": row.action_reviews or [],
        "findings": row.findings, "data_gaps": row.data_gaps,
        "notification_status": row.notification_status,
        "review_metadata": (row.input_snapshot or {}).get("review_metadata"),
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

    if context["actions"]:
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
        review_metadata = {
            "context_truncated": reviewer.context_truncated,
            "skipped_fields": reviewer.skipped_fields,
            "batch_count": reviewer.batch_count,
            "covered_action_ids": reviewer.covered_action_ids,
        }
    else:
        review = DecisionReview(
            assessment=ReviewAssessment.NO_ACTIONS if context["decisions"] else ReviewAssessment.NO_DECISIONS,
            summary=("Brak propozycji wejścia, akcji pozycji i automatycznych zamknięć w oknie."
                     if context["decisions"] else "Brak zapisanych decyzji i działań PAPER w oknie."),
            action_reviews=[], findings=[], data_gaps=[],
        )
        reviewer_model = "none"
        review_metadata = {"context_truncated": False, "skipped_fields": [],
                           "batch_count": 0, "covered_action_ids": []}

    if preview:
        return {"status": "PREVIEW", "window_end": window_end.isoformat(),
                "decision_count": len(context["decisions"]),
                "action_count": len(context["actions"]),
                "reviewer_model": reviewer_model,
                "review_metadata": review_metadata,
                "review": review.model_dump(mode="json")}

    context["review_metadata"] = review_metadata
    with SessionLocal.begin() as session:
        review_id = session.execute(insert(DecisionReviewORM).values(
            id=uuid4(), instrument=Instrument.BTC_USD.value,
            window_start=window_end - WINDOW, window_end=window_end,
            created_at=now, reviewer_model=reviewer_model,
            instructions_hash=INSTRUCTIONS_HASH,
            assessment=review.assessment.value, summary=review.summary,
            action_reviews=[item.model_dump(mode="json") for item in review.action_reviews],
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
