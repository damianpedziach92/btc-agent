"""Show the tools actually called by recently saved market decisions."""

import argparse
import json

from dotenv import load_dotenv
from sqlalchemy import select

from database.connection import SessionLocal
from database.models.agent_decision import AgentDecisionORM


def main() -> None:
    parser = argparse.ArgumentParser(description="Show actual tool usage per market decision")
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    load_dotenv()
    with SessionLocal() as session:
        rows = session.scalars(select(AgentDecisionORM).order_by(
            AgentDecisionORM.created_at.desc(), AgentDecisionORM.id.desc(),
        ).limit(args.limit)).all()
        result = []
        for row in rows:
            trace = row.analysis_trace
            calls = (trace or {}).get("tool_calls", [])
            result.append({
                "decision_id": str(row.id), "created_at": row.created_at.isoformat(),
                "decision": row.decision,
                "trace_available": trace is not None,
                "tools": [{
                    "name": item["name"],
                    "timeframe": item.get("arguments", {}).get("timeframe"),
                    "limit": item.get("arguments", {}).get("limit"),
                    "status": item["status"],
                    "count": item.get("count"),
                    "latest_timestamp": item.get("latest_timestamp"),
                    "ticker_at": (item.get("result") or {}).get("retrieved_at"),
                } for item in calls],
            })
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
