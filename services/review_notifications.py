"""Retired Telegram formatting for stored reviews; delivery is disabled."""

import logging
from datetime import datetime, timezone
from html import escape

from sqlalchemy import select

from database.connection import SessionLocal
from database.models.decision_review import DecisionReviewORM
from services.telegram_notifications import TelegramBotClient, TelegramConfig, TelegramDeliveryError


logger = logging.getLogger(__name__)


def _safe(value: object, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return escape(text)


def format_review_notification(row: DecisionReviewORM) -> str:
    lines = [
        f"<b>Audyt działań PAPER BTC/USD</b> | {row.window_start:%Y-%m-%d %H:%M}–{row.window_end:%H:%M} UTC",
        f"<b>Ocena:</b> {_safe(row.assessment)} | decyzje: {len(row.decision_ids)}",
        f"<b>Podsumowanie:</b> {_safe(row.summary, 900)}",
        "",
        f"<b>Analiza działań ({len(row.action_reviews or [])}):</b>",
    ]
    for action in row.action_reviews or []:
        short_id = action["action_id"].split(":")[1][:8]
        lines.extend([
            f"• <b>{_safe(action['action_type'])} | {_safe(action['verdict'])}</b> | {_safe(short_id)}",
            f"  <b>Ocena:</b> {_safe(action['reason'], 330)}",
            f"  <b>Dowód:</b> {_safe(action['evidence'], 330)}",
        ])
        if action.get("lesson"):
            lines.append(f"  <b>Wniosek:</b> {_safe(action['lesson'], 200)}")
    lines.extend([
        "",
        f"<b>Wskazane błędy ({len(row.findings)}):</b>",
    ])
    for finding in row.findings:
        lines.extend([
            f"• <b>{_safe(finding['severity'])} / {_safe(finding['category'])}</b> "
            f"| decyzja {_safe(finding['decision_id'][:8])}: {_safe(finding['issue'], 350)}",
        ])
        if finding.get("claim"):
            lines.append(f"  <b>Teza:</b> {_safe(finding['claim'], 300)}")
        lines.extend([
            f"  <b>Dowód:</b> {_safe(finding['evidence'], 500)}",
            f"  <b>Wniosek:</b> {_safe(finding['lesson'], 350)}",
        ])
    if not row.findings:
        lines.append("Brak potwierdzonych błędów")
    if row.data_gaps:
        lines.extend(["", "<b>Braki danych:</b>"])
        lines.extend(f"• {_safe(gap, 300)}" for gap in row.data_gaps)
    return "\n".join(lines)


def process_pending_review_notifications(
    limit: int = 3, client: TelegramBotClient | None = None,
) -> dict:
    return {"sent": 0, "failed": 0, "disabled": True}
    config = TelegramConfig.from_env()
    if config is None and client is None:
        return {"sent": 0, "failed": 0, "disabled": True}
    client = client or TelegramBotClient(config)
    with SessionLocal() as session:
        ids = list(session.scalars(select(DecisionReviewORM.id).where(
            DecisionReviewORM.notification_status == "PENDING",
        ).order_by(DecisionReviewORM.notification_attempts, DecisionReviewORM.created_at).limit(limit)))
    sent = failed = 0
    for review_id in ids:
        with SessionLocal.begin() as session:
            row = session.scalar(select(DecisionReviewORM).where(
                DecisionReviewORM.id == review_id,
                DecisionReviewORM.notification_status == "PENDING",
            ).with_for_update(skip_locked=True))
            if row is None:
                continue
            row.notification_attempts += 1
            try:
                client.send_message(format_review_notification(row))
            except TelegramDeliveryError as exc:
                row.notification_error = str(exc)
                failed += 1
                logger.warning("Review %s Telegram delivery failed: %s", review_id, exc)
            else:
                row.notification_status = "SENT"
                row.notified_at = datetime.now(timezone.utc)
                row.notification_error = None
                sent += 1
    return {"sent": sent, "failed": failed, "disabled": False}
