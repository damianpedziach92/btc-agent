"""Read-only CFD20 what-if for recorded legacy PAPER positions.

Cost-only keeps the original exit event. Protection replay separately checks
CFD-side TP/SL on closed M1; unresolved paths are never counted as realized.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from database.connection import SessionLocal
from database.models.candle import CandleORM
from database.models.paper import PaperPositionORM
from models.market import Instrument, Timeframe
from models.paper import (CFD_FIXED_20_USD, FeeRates, PaperSide, barrier_touch,
                          cfd20_rates, execution_price, gross_pnl)


def legacy_mid(position: PaperPositionORM, opening: bool) -> Decimal:
    rates = FeeRates.from_dict(position.fee_snapshot)
    price = position.entry_price if opening else position.close_price
    if rates.execution_model == CFD_FIXED_20_USD:
        return price - rates.spread_usd_per_btc/2 if (position.side == "LONG") == opening else price + rates.spread_usd_per_btc/2
    if not opening and position.close_reason in {"STOP_LOSS", "TAKE_PROFIT"}:
        return price  # historical barrier was compared with raw Kraken OHLC
    impact = (rates.spread_bps/2 + rates.slippage_bps) / Decimal("10000")
    buying = (position.side == "LONG") == opening
    return price / (Decimal("1") + impact if buying else Decimal("1") - impact)


def simulate(session, positions: list[PaperPositionORM]) -> dict:
    cfd = cfd20_rates()
    result = []
    for position in positions:
        if position.status == "OPEN" or position.close_at is None:
            continue
        side = PaperSide(position.side)
        entry_mid = legacy_mid(position, True)
        original_exit_mid = legacy_mid(position, False)
        new_entry = execution_price(entry_mid, side, True, cfd)
        same_exit = execution_price(original_exit_mid, side, False, cfd)
        cost_only = gross_pnl(side, new_entry, same_exit, position.quantity_btc)
        start = position.entry_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        if start < position.entry_at.astimezone(timezone.utc):
            start += timedelta(minutes=1)
        protective_original = position.close_reason in {"STOP_LOSS", "TAKE_PROFIT"}
        horizon = position.entry_at + timedelta(hours=24) if protective_original else position.close_at
        candles = session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == Timeframe.ONE_MINUTE,
            CandleORM.timestamp >= start,
            CandleORM.timestamp + timedelta(minutes=1) <= horizon,
        ).order_by(CandleORM.timestamp)).all()
        replay = None
        expected = start
        for candle in candles:
            if candle.timestamp.astimezone(timezone.utc) != expected:
                replay = {"status": "UNRESOLVED_DATA_GAP", "expected_at": expected.isoformat()}
                break
            expected += timedelta(minutes=1)
            reason, both, fill, assumption = barrier_touch(
                side, candle, position.stop_loss, position.take_profit, cfd)
            if reason:
                replay = {"status": "CLOSED", "reason": reason,
                          "exit_at": (candle.timestamp + timedelta(minutes=1)).astimezone(timezone.utc).isoformat(),
                          "exit_price": str(fill), "net_pnl": str(gross_pnl(side, new_entry, fill, position.quantity_btc)),
                          "fill_assumption": assumption, "both_barriers_hit": both}
                break
        if replay is None:
            if protective_original:
                replay = {"status": "UNRESOLVED_NO_CFD_BARRIER", "checked_until": expected.isoformat()}
            else:
                replay = {"status": "CLOSED", "reason": position.close_reason,
                          "exit_at": position.close_at.astimezone(timezone.utc).isoformat(),
                          "exit_price": str(same_exit), "net_pnl": str(cost_only),
                          "fill_assumption": "original_discretionary_exit_time"}
        result.append({
            "position_id": str(position.id), "side": position.side,
            "quantity_btc": str(position.quantity_btc),
            "original_execution_model": FeeRates.from_dict(position.fee_snapshot).execution_model,
            "original_entry_price": str(position.entry_price), "original_exit_price": str(position.close_price),
            "original_exit_reason": position.close_reason,
            "original_net_pnl": str(position.realized_net_pnl),
            "same_exit_cost_only": {"entry_mid": str(entry_mid), "exit_mid": str(original_exit_mid),
                                    "cfd_entry_price": str(new_entry), "cfd_exit_price": str(same_exit),
                                    "net_pnl": str(cost_only), "spread_cost_usd": str(position.quantity_btc*Decimal("20"))},
            "rerun_protection": replay,
        })
    return {"model": "CFD20 | spread $20/BTC | fee 0 | simulation only",
            "status": "COUNTERFACTUAL_READ_ONLY", "sample_size": len(result),
            "original_ledger_unchanged": True,
            "caveat": "Ceny odniesienia odtworzono z założeń dawnego modelu; OHLC nie pokazuje kolejności ticków ani płynności CFD. Próba nie dowodzi skuteczności strategii.",
            "positions": result,
            "original_net_total": str(sum((Decimal(row["original_net_pnl"]) for row in result), Decimal("0"))),
            "cost_only_net_total": str(sum((Decimal(row["same_exit_cost_only"]["net_pnl"]) for row in result), Decimal("0"))),
            "protection_replay_resolved": sum(row["rerun_protection"]["status"] == "CLOSED" for row in result)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only historical CFD20 counterfactual")
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()
    with SessionLocal() as session:
        positions = list(session.scalars(select(PaperPositionORM).where(
            PaperPositionORM.status != "OPEN",
        ).order_by(PaperPositionORM.entry_at)))
        report = simulate(session, positions)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# CFD20 — osobna symulacja kontrfaktyczna", "", report["model"], "",
             f"Próba: {report['sample_size']} pozycji. Oryginalny ledger pozostaje bez zmian.", "",
             f"Oryginalny wynik netto: {Decimal(report['original_net_total']):.2f} USD. Wynik przy tych samych godzinach wyjścia i kosztach CFD20: {Decimal(report['cost_only_net_total']):.2f} USD.", "",
             "Powtórna analiza TP/SL jest osobna: zmiana semantyki bariery może zmienić czas wyjścia, a nie tylko koszt.", "",
             "| ID | Strona | Oryginał netto | CFD20 te same wyjścia | CFD20 TP/SL |",
             "| --- | --- | ---: | ---: | --- |"]
    for row in report["positions"]:
        replay = row["rerun_protection"]
        replay_text = (f"{replay['reason']} {Decimal(replay['net_pnl']):.2f} USD" if replay["status"] == "CLOSED" else replay["status"])
        lines.append(f"| {row['position_id'][:8]} | {row['side']} | {Decimal(row['original_net_pnl']):.2f} | "
                     f"{Decimal(row['same_exit_cost_only']['net_pnl']):.2f} | {replay_text} |")
    lines.extend(["", report["caveat"], ""])
    prefix.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"sample_size": report["sample_size"],
                      "json": str(prefix.with_suffix(".json")), "markdown": str(prefix.with_suffix(".md"))}))


if __name__ == "__main__":
    main()
