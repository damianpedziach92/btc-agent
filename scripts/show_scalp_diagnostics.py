"""Summarize saved M5 candidate diagnostics without touching PAPER state."""
import argparse
import json
from collections import Counter
from datetime import datetime

from sqlalchemy import select

from database.connection import SessionLocal
from database.models.agent_decision import AgentDecisionORM
from database.models.paper import PaperPendingOrderORM


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only M5 analyst diagnostics")
    parser.add_argument("--since", help="UTC ISO 8601 decision creation time")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
    if since and (since.tzinfo is None or since.utcoffset().total_seconds() != 0):
        parser.error("--since must be timezone-aware UTC")
    with SessionLocal() as session:
        statement = select(AgentDecisionORM).order_by(AgentDecisionORM.created_at.desc()).limit(args.limit)
        if since:
            statement = statement.where(AgentDecisionORM.created_at >= since)
        rows = list(session.scalars(statement))
        order_statuses = Counter(session.scalars(select(PaperPendingOrderORM.status).where(
            PaperPendingOrderORM.created_by_decision_id.in_([row.id for row in rows])))) if rows else Counter()
    decisions = Counter(row.decision for row in rows)
    scalp_rejections = Counter()
    backend_rejections = Counter()
    backend_results = Counter()
    pending_eligible = submitted_m15 = submitted_m5 = 0
    diagnosed = 0
    for row in rows:
        for result in (row.paper_result or {}).get("results", []):
            backend_results[result.get("status", "UNKNOWN")] += 1
            if result.get("status") == "REJECTED":
                backend_rejections[str(result.get("reason", "UNKNOWN")).split(":", 1)[0]] += 1
        info = (row.analysis_trace or {}).get("strategy_diagnostics") or {}
        if not info:
            continue
        diagnosed += 1
        scalp_rejections.update(info.get("scalp_m5") or {})
        pending_eligible += info.get("scalp_m5_pending_eligible", 0)
        pending = info.get("pending_by_strategy") or {}
        submitted_m15 += pending.get("INTRADAY_M15", 0)
        submitted_m5 += pending.get("SCALP_M5", 0)
    print(json.dumps({"decisions": len(rows), "by_decision": dict(decisions),
                      "wait_ratio": decisions["WAIT"]/len(rows) if rows else None,
                      "diagnosed": diagnosed, "scalp_m5_rejections": dict(scalp_rejections),
                      "backend_rejections": dict(backend_rejections),
                      "backend_results": dict(backend_results),
                      "accepted_pending_order_statuses": dict(order_statuses),
                      "scalp_m5_pending_eligible": pending_eligible,
                      "submitted_pending": {"INTRADAY_M15": submitted_m15,
                                            "SCALP_M5": submitted_m5}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
