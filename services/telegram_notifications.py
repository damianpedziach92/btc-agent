"""Telegram transport and retired decision/order notification readers.

Only services.position_notifications may send production alerts.
"""

import json
import logging
import os
import re
from html import escape
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from database.connection import SessionLocal
from database.models.agent_decision import AgentDecisionORM
from database.models.agent_monitor_item import AgentMonitorItemORM
from database.models.telegram_notification import TelegramNotificationORM
from database.models.paper import PaperOrderEventORM, PaperPendingOrderORM
from services.paper_trading import PaperTradingService, iso_utc
from services.pending_orders import PendingOrderManager


logger = logging.getLogger(__name__)
TELEGRAM_MESSAGE_LIMIT = 4096
TEXT_PRICE_PATTERN = re.compile(r"(?<![\w.])\d{5,8}(?:[.,]\d+)?(?!\w)")


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> "TelegramConfig | None":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        return cls(token, chat_id) if token and chat_id else None


class TelegramDeliveryError(RuntimeError):
    pass


class TelegramBotClient:
    def __init__(self, config: TelegramConfig, opener=urlopen):
        self.config = config
        self.opener = opener

    def send_message(self, text: str) -> None:
        for chunk in _message_chunks(text):
            request = Request(
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                data=json.dumps({"chat_id": self.config.chat_id, "text": chunk,
                                 "parse_mode": "HTML"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self.opener(request, timeout=5) as response:
                    payload = json.load(response)
            except HTTPError as exc:
                raise TelegramDeliveryError(f"Telegram HTTP {exc.code}") from None
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                # Exception strings can contain the URL, including the bot token.
                raise TelegramDeliveryError(f"Telegram transport error ({type(exc).__name__})") from None
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                code = payload.get("error_code", "unknown") if isinstance(payload, dict) else "unknown"
                raise TelegramDeliveryError(f"Telegram API error {code}")


def _message_chunks(text: str) -> list[str]:
    if not text:
        raise ValueError("Telegram message is empty")
    result = []
    current = ""
    for line in text.split("\n"):
        if len(line) > TELEGRAM_MESSAGE_LIMIT:
            raise ValueError("Telegram message line is too long")
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            result.append(current)
            current = line
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _short(value: object, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _usd(value: object) -> str:
    return f"{Decimal(str(value)):.2f} USD"


def _price(value: object) -> str:
    return "brak" if value is None else f"{Decimal(str(value)):.2f}"


def _safe(value: object, limit: int = 260, *, prices: bool = False) -> str:
    text = str(value or "")
    if prices:
        text = TEXT_PRICE_PATTERN.sub(
            lambda match: f"{Decimal(match.group().replace(',', '.')):.2f}", text
        )
    return escape(_short(text, limit))


def format_decision_notification(
    decision: AgentDecisionORM, account: dict, positions: list[dict],
    monitors: list[AgentMonitorItemORM] | None = None,
    closed_positions: list[dict] | None = None,
    pending_orders: list[dict] | None = None,
) -> str:
    lines = [
        f"BTC/USD | <b>Decyzja:</b> {_safe(decision.decision)} | {iso_utc(decision.created_at)}",
        f"<b>Agent:</b> {_safe(decision.text, 350, prices=True)}",
    ]
    proposal = decision.open_proposal
    if proposal:
        lines.append(
            f"<b>Propozycja:</b> {_safe(proposal['side'])}, "
            f"SL {_price(proposal.get('stop_loss'))}, TP {_price(proposal.get('take_profit'))}"
        )
        lines.append(f"<b>Powód wejścia:</b> {_safe(proposal.get('reason'), 120, prices=True)}")
    for action in decision.position_actions or []:
        lines.append(
            f"<b>Akcja:</b> {_safe(action['action'])} {_safe(str(action['position_id'])[:8])}; "
            f"{_safe(action.get('reason'), 80, prices=True)}"
        )
    if decision.paper_status == "DONE":
        result = decision.paper_result or {}
        for item in result.get("results", []):
            lines.append(
                f"<b>PAPER:</b> {_safe(item.get('status', '?'))} "
                f"{_safe(str(item.get('position_id') or '')[:8])} "
                f"{_safe(item.get('reason'), 100)}".strip()
            )
        if result.get("reason"):
            lines.append(f"<b>PAPER:</b> {_safe(result['reason'], 100)}")
        elif not result.get("results"):
            lines.append("<b>PAPER:</b> brak zmian pozycji")
    else:
        lines.append("<b>PAPER:</b> wykonanie decyzji oczekuje")

    lines.append("")
    if account.get("status") == "UNSEEDED":
        lines.append("<b>Rachunek PAPER:</b> nieutworzony")
    else:
        lines.extend([
            f"<b>Rachunek PAPER:</b> {_safe(account.get('status', '?'))} | equity {_usd(account['equity_estimate'])}",
            f"<b>Gotówka:</b> {_usd(account['cash_balance'])} | dostępne {_usd(account['available_margin'])}",
            f"<b>Margin:</b> {_usd(account['reserved_margin'])} | zrealizowany PnL {_usd(account['realized_pnl'])}",
            f"<b>Opłaty:</b> {_usd(account['fees_paid'])} | wycena {_safe(account.get('pnl_quality', 'UNCONFIGURED'))}",
        ])
        rates = account.get("fee_rates") or {}
        if rates.get("execution_model") == "CFD_FIXED_20_USD":
            lines.append("<b>Model:</b> CFD20 | spread $20/BTC | fee 0 | simulation only")
        if account.get("stale_data"):
            lines.append("<b>Uwaga:</b> ostatnia świeca 1m jest nieaktualna")

    lines.append(f"<b>Otwarte pozycje ({len(positions)}):</b>")
    for position in positions:
        net = position.get("unrealized_net_estimate")
        lines.extend([
            f"• <b>{_safe(position['side'])} {_safe(position['id'][:8])}</b> | {_safe(position['quantity_btc'])} BTC "
            f"| wejście {_price(position['entry_price'])} | kurs {_price(position.get('current_mark_price'))}",
            f"  <b>SL:</b> {_price(position.get('stop_loss'))} | <b>TP:</b> {_price(position.get('take_profit'))} "
            f"| wynik netto szac. {_usd(net) if net is not None else 'brak wyceny'}",
            f"  <b>Powód:</b> {_safe(position.get('entry_reason'), 90, prices=True)}",
        ])
    if not positions:
        lines.append("Brak")
    pending_orders = pending_orders or []
    lines.append("")
    lines.append(f"<b>Zlecenia oczekujące ({len(pending_orders)}):</b>")
    for order in pending_orders:
        lines.append(f"• <b>{_safe(order['order_type'])} {_safe(order['id'][:8])}</b> | "
                     f"poziom {_price(order['entry_level_usd'])} | ważne do {_safe(order['expires_at'], 30)}")
    if not pending_orders:
        lines.append("Brak")
    closed_positions = closed_positions or []
    if closed_positions:
        lines.append("")
        lines.append(f"<b>Zamknięte od poprzedniej analizy ({len(closed_positions)}):</b>")
        for position in closed_positions:
            lines.append(
                f"• <b>{_safe(position['side'])} {_safe(position['id'][:8])}</b> | "
                f"{_safe(position['close_reason'])} {_price(position['close_price'])} "
                f"| netto {_usd(position['realized_net_estimate'])}"
            )
    monitors = monitors or []
    lines.append("")
    lines.append(f"<b>Monitorowane ({len(monitors)}):</b>")
    for monitor in monitors:
        key = monitor["key"] if isinstance(monitor, dict) else monitor.key
        description = monitor["description"] if isinstance(monitor, dict) else monitor.description
        lines.append(f"• <b>{_safe(key, 80)}:</b> {_safe(description, 110, prices=True)}")
    if not monitors:
        lines.append("Brak aktywnych obserwacji")
    return "\n".join(lines)


def enqueue_decision_notification(session, decision_id: UUID, now: datetime | None = None) -> bool:
    """Legacy decision alerts are disabled; position events own Telegram."""
    return False
    if TelegramConfig.from_env() is None:
        return False
    session.execute(insert(TelegramNotificationORM).values(
        decision_id=decision_id, status="PENDING",
        created_at=now or datetime.now(timezone.utc), sent_at=None,
        attempts=0, last_error=None,
    ).on_conflict_do_nothing(index_elements=[TelegramNotificationORM.decision_id]))
    return True


def process_pending_notifications(limit: int = 3, client: TelegramBotClient | None = None) -> dict:
    return {"sent": 0, "failed": 0, "pending": 0, "disabled": True}
    config = TelegramConfig.from_env()
    if config is None:
        return {"sent": 0, "failed": 0, "pending": 0, "disabled": True}
    client = client or TelegramBotClient(config)
    with SessionLocal() as session:
        ids = list(session.scalars(select(TelegramNotificationORM.decision_id).where(
            TelegramNotificationORM.status == "PENDING"
        ).order_by(TelegramNotificationORM.attempts, TelegramNotificationORM.created_at).limit(limit)))
    sent = failed = pending = 0
    for decision_id in ids:
        with SessionLocal.begin() as session:
            row = session.scalar(select(TelegramNotificationORM).where(
                TelegramNotificationORM.decision_id == decision_id,
                TelegramNotificationORM.status == "PENDING",
            ).with_for_update(skip_locked=True))
            if row is None:
                continue
            decision = session.get(AgentDecisionORM, decision_id)
            if decision.paper_status != "DONE" and datetime.now(timezone.utc) - decision.created_at < timedelta(minutes=2):
                pending += 1
                continue
            paper = PaperTradingService(session)
            previous_at = session.scalar(select(AgentDecisionORM.created_at).where(
                AgentDecisionORM.instrument == decision.instrument,
                AgentDecisionORM.created_at < decision.created_at,
            ).order_by(AgentDecisionORM.created_at.desc()).limit(1))
            since = max(previous_at or decision.created_at - timedelta(minutes=20),
                        decision.created_at - timedelta(minutes=20))
            closed_positions = [position for position in paper.recent_closed_positions(20)
                                if datetime.fromisoformat(position["close_at"].replace("Z", "+00:00")) >= since]
            monitors = list(session.scalars(select(AgentMonitorItemORM).where(
                AgentMonitorItemORM.instrument == decision.instrument,
                AgentMonitorItemORM.status == "ACTIVE",
                AgentMonitorItemORM.expires_at > datetime.now(timezone.utc),
            ).order_by(AgentMonitorItemORM.key)))
            message = format_decision_notification(
                decision, paper.account_snapshot(), paper.open_positions_snapshot(),
                monitors, closed_positions,
                PendingOrderManager(session, paper).snapshot(),
            )
            row.attempts += 1
            try:
                client.send_message(message)
            except TelegramDeliveryError as exc:
                row.last_error = str(exc)
                failed += 1
                logger.warning("Telegram decision %s delivery failed: %s", decision_id, exc)
            else:
                row.status = "SENT"
                row.sent_at = datetime.now(timezone.utc)
                row.last_error = None
                sent += 1
    return {"sent": sent, "failed": failed, "pending": pending, "disabled": False}


def process_pending_order_notifications(limit: int = 10, client: TelegramBotClient | None = None) -> dict:
    return {"sent": 0, "failed": 0, "disabled": True}
    config = TelegramConfig.from_env()
    if config is None:
        return {"sent": 0, "failed": 0, "disabled": True}
    client = client or TelegramBotClient(config)
    with SessionLocal() as session:
        ids = list(session.scalars(select(PaperOrderEventORM.id).where(
            PaperOrderEventORM.notification_status == "PENDING",
        ).order_by(PaperOrderEventORM.event_time, PaperOrderEventORM.id).limit(limit)))
    sent = failed = 0
    for event_id in ids:
        with SessionLocal.begin() as session:
            event = session.scalar(select(PaperOrderEventORM).where(
                PaperOrderEventORM.id == event_id,
                PaperOrderEventORM.notification_status == "PENDING",
            ).with_for_update(skip_locked=True))
            if event is None:
                continue
            order = session.get(PaperPendingOrderORM, event.order_id)
            account = PaperTradingService(session).account_snapshot()
            message = (f"BTC/USD | <b>Zlecenie PAPER {escape(event.type)}</b> | {iso_utc(event.event_time)}\n"
                       f"<b>ID:</b> {escape(str(order.id))} | <b>Typ:</b> {escape(order.order_type)}\n"
                       f"<b>Poziom:</b> {_price(order.entry_level_usd)} | <b>Pozycja:</b> "
                       f"{escape(str(order.position_id)) if order.position_id else 'brak'}\n"
                       f"<b>Powód:</b> {_safe(event.payload.get('reason') or order.reason, 180, prices=True)}\n"
                       f"<b>Model:</b> CFD20 | spread $20/BTC | fee 0 | simulation only\n"
                       f"<b>Equity:</b> {_usd(account['equity_estimate']) if account.get('equity_estimate') else 'brak'}")
            event.notification_attempts += 1
            try:
                client.send_message(message)
            except TelegramDeliveryError as exc:
                failed += 1
                logger.warning("Telegram pending order event %s delivery failed: %s", event.id, exc)
            else:
                event.notification_status = "SENT"
                event.notified_at = datetime.now(timezone.utc)
                sent += 1
    return {"sent": sent, "failed": failed, "disabled": False}
