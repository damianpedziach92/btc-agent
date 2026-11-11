"""Run or preview a completed two-hour decision audit."""

import argparse
import json
from datetime import datetime

from openai import OpenAIError

from services.decision_review import run_decision_review
from services.review_notifications import process_pending_review_notifications


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit previous BTC/USD agent decisions")
    parser.add_argument("--window-end", help="Even UTC hour ending the two-hour window, ISO 8601")
    parser.add_argument("--preview", action="store_true", help="Call reviewer without saving or notifying")
    args = parser.parse_args()
    try:
        window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00")) if args.window_end else None
        result = run_decision_review(window_end=window_end, preview=args.preview)
    except (RuntimeError, ValueError, OpenAIError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "CREATED":
        print(json.dumps({"telegram": process_pending_review_notifications()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
