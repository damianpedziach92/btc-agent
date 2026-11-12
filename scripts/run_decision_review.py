"""Run or preview a completed two-hour decision audit."""

import argparse
import json
import time
from datetime import datetime, timedelta

from openai import OpenAIError, RateLimitError

from services.decision_review import run_decision_review


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit previous BTC/USD agent decisions")
    parser.add_argument("--window-end", help="Even UTC hour ending the two-hour window, ISO 8601")
    parser.add_argument("--preview", action="store_true", help="Call reviewer without saving or notifying")
    parser.add_argument("--backfill-from", help="First missing window end (even UTC hour)")
    parser.add_argument("--backfill-to", help="Last missing window end (even UTC hour), inclusive")
    args = parser.parse_args()
    def audit_window(window_end):
        for retry in range(4):
            try:
                return run_decision_review(window_end=window_end, preview=args.preview)
            except RateLimitError:
                if retry == 3:
                    raise
                time.sleep(5 * (2 ** retry))
    try:
        window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00")) if args.window_end else None
        if bool(args.backfill_from) != bool(args.backfill_to):
            parser.error("Both --backfill-from and --backfill-to are required")
        if args.backfill_from:
            if args.window_end:
                parser.error("Choose --window-end or bounded backfill")
            start = datetime.fromisoformat(args.backfill_from.replace("Z", "+00:00"))
            end = datetime.fromisoformat(args.backfill_to.replace("Z", "+00:00"))
            if (start.tzinfo is None or end.tzinfo is None or start.utcoffset() != timedelta(0)
                or end.utcoffset() != timedelta(0) or end < start or end - start > timedelta(days=7)):
                parser.error("Backfill requires UTC ordered endpoints within 7 days")
            results = []
            cursor = start
            while cursor <= end:
                results.append(audit_window(cursor))
                cursor += timedelta(hours=2)
            result = {"status": "BACKFILL", "windows": results}
        else:
            result = audit_window(window_end)
    except (RuntimeError, ValueError, OpenAIError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
