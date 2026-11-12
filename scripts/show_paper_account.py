import json

from database.connection import SessionLocal
from services.paper_trading import PaperTradingService
from services.pending_orders import PendingOrderManager


def main() -> None:
    with SessionLocal() as session:
        paper = PaperTradingService(session)
        result = {
            "model_label": "CFD20 | spread $20/BTC | fee 0 | simulation only",
            "account": paper.account_snapshot(),
            "positions": paper.open_positions_snapshot(),
            "pending_orders": PendingOrderManager(session, paper).snapshot(),
            "recent_orders": PendingOrderManager(session, paper).recent_history(),
            "recent_closed_positions": paper.recent_closed_positions(),
            "recent_events": paper.recent_events(10),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
