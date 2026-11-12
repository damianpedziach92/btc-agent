"""Deterministic, closed-candle M5 candidates for the PAPER analyst.

Candidates are research observations. They never execute orders by themselves.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from models.paper import MIN_NET_RISK_REWARD, PaperSide, cfd20_rates, risk_reward_for_rates
from models.strategy import SCALP_PARAMETERS_V1, ScalpSetup, Strategy


def _d(value):
    return Decimal(str(value)) if value is not None else None


def _protective_cent(value: Decimal, side: PaperSide) -> Decimal:
    """Round a stop away from entry so model decimal truncation cannot tighten it."""
    return value.quantize(Decimal("0.01"), rounding=(ROUND_FLOOR if side is PaperSide.LONG
                                                     else ROUND_CEILING))


def build_scalp_candidates(context: dict) -> dict:
    frame = context["timeframes"]["5m"]
    rows = frame["recent_closed_candles"]
    structure = frame["market_structure"]
    as_of = datetime.fromisoformat(context["as_of"])
    atr = _d(structure["atr14"])
    last_close = datetime.fromisoformat(frame["last_closed_at"]) if frame["last_closed_at"] else None
    fresh = (last_close is not None and timedelta(0) <= as_of-last_close <= timedelta(minutes=10)
             and frame["consecutive_count"] >= 15 and atr is not None and atr > 0)
    h1_rows = context["timeframes"]["1h"]["recent_closed_candles"]
    h1_closes = [_d(row["close"]) for row in h1_rows[-3:]]
    regime = ("UP" if len(h1_closes) == 3 and h1_closes[0] < h1_closes[1] < h1_closes[2] else
              "DOWN" if len(h1_closes) == 3 and h1_closes[0] > h1_closes[1] > h1_closes[2] else
              "RANGE_OR_UNKNOWN")
    results = []
    counts = {}
    for family in ScalpSetup:
        for side in PaperSide:
            item = {"strategy": Strategy.SCALP_M5.value, "setup_type": family.value,
                    "setup_family": family.value, "regime": regime,
                    "direction": side.value, "timeframe": "5m", "as_of": context["as_of"],
                    "reference_entry": None, "stop_loss": None, "take_profit": None,
                    "expected_rr_after_costs": None,
                    "valid_until": (as_of + timedelta(minutes=15)).isoformat(),
                    "trigger": None, "level": None, "invalidation": None,
                    "reason_no_trade": None, "suggested_order_type": None,
                    "pending_eligible": False, "pending_entry_level": None,
                    "pending_stop_loss": None, "pending_rr_after_costs": None}
            reason = None
            if not fresh or len(rows) < 6:
                reason = "ATR_DATA_GAP"
            else:
                latest, previous = rows[-1], rows[-2]
                close, low, high = _d(latest["close"]), _d(latest["low"]), _d(latest["high"])
                prev_close = _d(previous["close"])
                prior = rows[-6:-1]
                range_high = max(_d(row["high"]) for row in prior)
                range_low = min(_d(row["low"]) for row in prior)
                swing = rows[-SCALP_PARAMETERS_V1["swing_candles"]:]
                buffer = Decimal(SCALP_PARAMETERS_V1["stop_buffer_atr"])
                minimum = Decimal(SCALP_PARAMETERS_V1["minimum_stop_atr"])
                invalidation = (min(_d(row["low"]) for row in swing) - atr*buffer
                                if side is PaperSide.LONG else
                                max(_d(row["high"]) for row in swing) + atr*buffer)
                # Levels are executable CFD prices; the reference entry is the
                # last closed midpoint plus the opening half-spread.
                entry = close + (Decimal("10") if side is PaperSide.LONG else Decimal("-10"))
                stop = _protective_cent(
                    min(invalidation, entry-atr*minimum) if side is PaperSide.LONG else
                    max(invalidation, entry+atr*minimum), side)
                item["reference_entry"] = str(entry)
                item["stop_loss"] = str(stop)
                item["invalidation"] = str(invalidation)
                trigger = False
                formed = False
                target = None
                if family is ScalpSetup.M5_RANGE_REJECTION:
                    quiet = range_high-range_low <= atr*Decimal("4")
                    formed = quiet
                    trigger = quiet and ((low <= range_low + atr*Decimal("0.35") and close > prev_close)
                                         if side is PaperSide.LONG else
                                         (high >= range_high - atr*Decimal("0.35") and close < prev_close))
                    item["level"] = str(range_low if side is PaperSide.LONG else range_high)
                    target = (range_high - Decimal("10") if side is PaperSide.LONG else
                              range_low + Decimal("10"))
                    item["suggested_order_type"] = "BUY_LIMIT" if side is PaperSide.LONG else "SELL_LIMIT"
                elif family is ScalpSetup.M5_BREAKOUT_RETEST:
                    breakout = rows[-2]
                    older = rows[-6:-2]
                    boundary = (max(_d(row["high"]) for row in older) if side is PaperSide.LONG else
                                min(_d(row["low"]) for row in older))
                    formed = (_d(breakout["close"]) > boundary if side is PaperSide.LONG else
                              _d(breakout["close"]) < boundary)
                    trigger = ((_d(breakout["close"]) > boundary and low <= boundary and close > boundary)
                               if side is PaperSide.LONG else
                               (_d(breakout["close"]) < boundary and high >= boundary and close < boundary))
                    item["level"] = str(boundary)
                    item["suggested_order_type"] = "BUY_LIMIT" if side is PaperSide.LONG else "SELL_LIMIT"
                else:
                    boundary = (max(_d(row["high"]) for row in rows[-6:-3]) if side is PaperSide.LONG else
                                min(_d(row["low"]) for row in rows[-6:-3]))
                    formed = (prev_close > boundary if side is PaperSide.LONG else prev_close < boundary)
                    trigger = ((prev_close > boundary and low <= boundary and close > boundary)
                               if side is PaperSide.LONG else
                               (prev_close < boundary and high >= boundary and close < boundary))
                    item["level"] = str(boundary)
                    item["suggested_order_type"] = "BUY_LIMIT" if side is PaperSide.LONG else "SELL_LIMIT"
                item["trigger"] = "closed M5 retest/rejection" if trigger else None
                blocked_regime = family is ScalpSetup.M5_RANGE_REJECTION and (
                    (regime == "UP" and side is PaperSide.SHORT) or
                    (regime == "DOWN" and side is PaperSide.LONG))
                structural = (stop <= entry - atr*minimum if side is PaperSide.LONG else
                              stop >= entry + atr*minimum)
                if target is None:
                    higher = [context["timeframes"][key]["market_structure"] for key in ("15m", "1h")]
                    levels = [_d(s["channel_high_20" if side is PaperSide.LONG else "channel_low_20"])
                              for s in higher]
                    eligible = [level for level in levels if level is not None and
                                (level > entry if side is PaperSide.LONG else level < entry)]
                    if eligible:
                        target = (min(eligible) - Decimal("10") if side is PaperSide.LONG else
                                  max(eligible) + Decimal("10"))
                rr = None
                if target is not None:
                    item["take_profit"] = str(target)
                if target is not None and (target > entry if side is PaperSide.LONG else target < entry):
                    rr = risk_reward_for_rates(cfd20_rates(), side, entry, stop, target, Decimal("1"))["rr_ratio"]
                    item["expected_rr_after_costs"] = str(rr) if rr is not None else None
                pending_entry = _d(item["level"]) + (Decimal("10") if side is PaperSide.LONG else -Decimal("10"))
                better_limit = (pending_entry < entry if side is PaperSide.LONG else pending_entry > entry)
                pending_stop = _protective_cent(
                    min(invalidation, pending_entry-atr*minimum) if side is PaperSide.LONG else
                    max(invalidation, pending_entry+atr*minimum), side)
                pending_rr = (risk_reward_for_rates(cfd20_rates(), side, pending_entry, pending_stop, target,
                                                   Decimal("1"))["rr_ratio"]
                              if target is not None and pending_stop > 0 else None)
                item["pending_entry_level"] = str(pending_entry) if better_limit else None
                item["pending_stop_loss"] = str(pending_stop) if better_limit else None
                item["pending_rr_after_costs"] = str(pending_rr) if pending_rr is not None else None
                item["pending_eligible"] = bool(formed and not blocked_regime and better_limit and
                                                pending_rr is not None and pending_rr >= MIN_NET_RISK_REWARD)
                if blocked_regime:
                    reason = "VOLATILITY_FILTER"
                elif not structural:
                    reason = "INVALID_SL_STRUCTURE"
                elif target is None or rr is None:
                    reason = "NO_STRUCTURAL_TARGET"
                elif rr < MIN_NET_RISK_REWARD:
                    reason = "RR_LOW"
                elif not trigger:
                    reason = "INVALID_TRIGGER"
            item["reason_no_trade"] = reason
            counts[reason or "ELIGIBLE"] = counts.get(reason or "ELIGIBLE", 0) + 1
            results.append(item)
    return {"candidates": results, "reason_counts": counts}
