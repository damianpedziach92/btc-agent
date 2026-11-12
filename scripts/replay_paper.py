"""Export a read-only CFD20 strategy comparison from stored closed candles."""
import argparse
import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from database.connection import SessionLocal
from services.replay import Replay


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only PAPER CFD20 replay")
    parser.add_argument("--start", required=True, help="UTC ISO 8601 start")
    parser.add_argument("--end", required=True, help="UTC ISO 8601 end")
    parser.add_argument("--output-prefix", required=True, help="Path prefix for JSON, CSV and Markdown")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    with SessionLocal() as session:
        report = Replay(session, start, end).compare()
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trade_rows = []
    for run in report["runs"]:
        for trade in run["trades"]:
            trade_rows.append({"strategy": run["strategy"], "split": run["split"], **trade})
    with prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["strategy", "split", "side", "setup", "regime", "entry_at", "exit_at", "entry_price",
                  "exit_price", "quantity_btc", "exit_reason", "fill_assumption", "gross_pnl",
                  "net_pnl", "fee_usd", "spread_cost_usd", "r_multiple", "duration_minutes"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(trade_rows)
    lines = ["# PAPER CFD20 replay", "", report["model"], "", report["data_policy"], "",
             "| Split | Strategy | Signals | Trades | Win rate | Gross USD | Net USD | Max DD USD | Spread USD |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for run in report["runs"]:
        percent = f"{Decimal(run['win_rate'])*100:.1f}%" if run["win_rate"] is not None else "n/a"
        lines.append(f"| {run['split']} | {run['strategy']} | {run['signals']} | {run['trade_count']} | "
                     f"{percent} | {Decimal(run['gross_pnl']):.2f} | {Decimal(run['net_pnl']):.2f} | "
                     f"{Decimal(run['max_drawdown_usd']):.2f} | {Decimal(run['spread_cost_usd']):.2f} |")
    lines.extend(["", "Historical OHLC cannot establish intrabar order, broker fills or liquidity.", ""])
    prefix.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"json": str(prefix.with_suffix(".json")), "csv": str(prefix.with_suffix(".csv")),
                      "markdown": str(prefix.with_suffix(".md")), "runs": len(report["runs"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
