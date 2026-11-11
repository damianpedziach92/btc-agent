"""Auditably correct a historical PAPER close after a verified 1m TP/SL hit."""

import argparse
import json
from uuid import UUID

from database.connection import SessionLocal
from services.paper_trading import PaperTradingService


def main() -> None:
    parser = argparse.ArgumentParser(description="Correct a PAPER position to its first verified TP/SL fill")
    parser.add_argument("position_id", type=UUID)
    parser.add_argument("--expected-reason", required=True, choices=["TAKE_PROFIT", "STOP_LOSS"])
    args = parser.parse_args()
    with SessionLocal.begin() as session:
        result = PaperTradingService(session).correct_closed_barrier_fill(
            args.position_id, args.expected_reason
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
