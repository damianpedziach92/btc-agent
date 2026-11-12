"""Send one concise Telegram alert for each actual PAPER OPENED/CLOSED event."""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from database.connection import SessionLocal
from database.models.paper import PaperPositionEventORM, PaperPositionORM
from database.models.position_notification import PaperPositionNotificationORM
from services.telegram_notifications import TelegramBotClient, TelegramConfig, TelegramDeliveryError


logger = logging.getLogger(__name__)


def _pl_amount(value: object, signed: bool = False) -> str:
    number = Decimal(str(value))
    result = f"{number:+,.2f}" if signed else f"{number:,.2f}"
    return result.replace(",", " ").replace(".", ",")


def format_position_notification(event: PaperPositionEventORM, position: PaperPositionORM) -> str:
    short_id = str(position.id)[:8]
    if event.type == "OPENED":
        source = "ZLECENIE" if event.payload.get("price_source") == "pending_closed_1m_ohlc" else "AGENT"
        return "\n".join((
            f"🟢 PAPER BTC/USD — OTWARTA {position.side} [{short_id}]",
            f"Kurs: {_pl_amount(event.payload['execution_price'])} USD",
            f"TP: {_pl_amount(position.take_profit)} USD",
            f"SL: {_pl_amount(position.stop_loss)} USD",
            f"Źródło: {source}",
        ))
    if event.type == "CLOSED":
        return "\n".join((
            f"🔴 PAPER BTC/USD — ZAMKNIĘTA {position.side} [{short_id}]",
            f"Kurs zamknięcia: {_pl_amount(event.payload['execution_price'])} USD",
            f"Wynik netto: {_pl_amount(event.payload['paper_net_estimate_usd'], signed=True)} USD",
            f"Powód: {event.payload['reason']}",
        ))
    raise ValueError("Only OPENED and CLOSED position events may be sent")


def process_position_notifications(limit: int = 10, client: TelegramBotClient | None = None) -> dict:
    config = TelegramConfig.from_env()
    if config is None and client is None:
        return {"sent": 0, "failed": 0, "pending": 0, "disabled": True}
    client = client or TelegramBotClient(config)
    with SessionLocal() as session:
        ids = list(session.scalars(select(PaperPositionNotificationORM.event_id).where(
            PaperPositionNotificationORM.status == "PENDING",
        ).order_by(PaperPositionNotificationORM.created_at,
                   PaperPositionNotificationORM.event_id).limit(limit)))
    sent = failed = pending = 0
    for event_id in ids:
        with SessionLocal.begin() as session:
            row = session.scalar(select(PaperPositionNotificationORM).where(
                PaperPositionNotificationORM.event_id == event_id,
                PaperPositionNotificationORM.status == "PENDING",
            ).with_for_update(skip_locked=True))
            if row is None:
                continue
            event = session.get(PaperPositionEventORM, event_id)
            if event is None or event.type not in {"OPENED", "CLOSED"} or event.position_id is None:
                row.status = "SENT"  # malformed historical event; never send arbitrary content
                row.last_error = "Invalid position event"
                continue
            if event.type == "CLOSED" and session.scalar(select(PaperPositionNotificationORM.event_id)
                .join(PaperPositionEventORM, PaperPositionEventORM.id == PaperPositionNotificationORM.event_id)
                .where(PaperPositionEventORM.position_id == event.position_id,
                       PaperPositionEventORM.type == "OPENED",
                       PaperPositionNotificationORM.status == "PENDING")):
                pending += 1
                continue
            position = session.get(PaperPositionORM, event.position_id)
            row.attempts += 1
            try:
                client.send_message(format_position_notification(event, position))
            except TelegramDeliveryError as exc:
                row.last_error = str(exc)
                failed += 1
                logger.warning("Position event %s Telegram delivery failed: %s", event_id, exc)
            else:
                row.status = "SENT"
                row.sent_at = datetime.now(timezone.utc)
                row.last_error = None
                sent += 1
    return {"sent": sent, "failed": failed, "pending": pending, "disabled": False}
