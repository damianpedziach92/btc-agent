"""Find a bot chat ID or send an explicit test notification."""

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from services.telegram_notifications import TelegramBotClient, TelegramConfig, TelegramDeliveryError


def list_chats(token: str) -> list[dict]:
    request = Request(f"https://api.telegram.org/bot{token}/getUpdates?limit=20")
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise TelegramDeliveryError(f"Telegram HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise TelegramDeliveryError(f"Telegram transport error ({type(exc).__name__})") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramDeliveryError("Telegram getUpdates failed; check the bot token and webhook")
    chats = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            chats[str(chat["id"])] = {
                "chat_id": str(chat["id"]),
                "type": chat.get("type"),
                "name": chat.get("title") or chat.get("username") or chat.get("first_name"),
            }
    return list(chats.values())


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Set up BTC Agent Telegram notifications")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list-chats", action="store_true", help="Show chat IDs after you message your bot")
    group.add_argument("--send-test", action="store_true", help="Send one explicit test message")
    args = parser.parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        parser.error("Set TELEGRAM_BOT_TOKEN in .env first")
    try:
        if args.list_chats:
            chats = list_chats(token)
            if chats:
                print(json.dumps(chats, ensure_ascii=False, indent=2))
            else:
                print("Brak czatów. Wyślij /start do swojego bota i spróbuj ponownie.")
        else:
            config = TelegramConfig.from_env()
            if config is None:
                parser.error("Set TELEGRAM_CHAT_ID in .env first")
            TelegramBotClient(config).send_message(
                "BTC Agent PAPER: wiadomość testowa. Raporty będą przychodzić po zapisanych decyzjach 15m."
            )
            print("Wysłano wiadomość testową.")
    except TelegramDeliveryError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
