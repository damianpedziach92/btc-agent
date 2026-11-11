import json

from database.connection import SessionLocal
from services.paper_trading import PaperTradingService


def main() -> None:
    with SessionLocal() as session:
        paper = PaperTradingService(session)
        result = {
            "account": paper.account_snapshot(),
            "positions": paper.open_positions_snapshot(),
            "recent_closed_positions": paper.recent_closed_positions(),
            "recent_events": paper.recent_events(10),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
