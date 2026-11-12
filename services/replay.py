"""Read-only, deterministic PAPER replay on stored closed candles.

Entry is assumed at the first M1 open after a signal. Protective exits use
the same CFD20 executable-price and OHLC barrier functions as PAPER runtime.
No LLM, ledger write or broker call is made.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.agent_decision import AgentDecisionORM
from database.models.candle import CandleORM
from database.models.paper import PaperPositionORM
from models.market import Instrument, Timeframe
from models.paper import (ZERO, FeeRates, PaperSide, RiskConfig, RiskEngine,
                          barrier_touch, cfd20_rates, dec, execution_price, gross_pnl,
                          risk_reward_for_rates, MIN_NET_RISK_REWARD)
from repositories.candle_repository import CandleRepository
from services.market_structure import calculate_market_structure
from services.scalp_candidates import build_scalp_candidates
from services.pending_orders import evaluate_order_candle
from models.strategy import SCALP_PARAMETERS_V1


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, ZERO) / len(values)


def _std(values: list[Decimal]) -> Decimal:
    avg = _mean(values)
    return (_mean([(value-avg)**2 for value in values])).sqrt()


class Replay:
    def __init__(self, session: Session, start: datetime, end: datetime,
                 risk: RiskConfig | None = None):
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("Replay needs an ordered UTC range")
        self.start, self.end = _utc(start), _utc(end)
        if self.end - self.start > timedelta(days=90):
            raise ValueError("Replay range is limited to 90 days")
        self.risk = risk or RiskConfig.from_env()
        self.rates = cfd20_rates()
        self.rows = {}
        self.ends = {}
        for timeframe in (Timeframe.ONE_MINUTE, Timeframe.FIVE_MINUTES,
                          Timeframe.FIFTEEN_MINUTES, Timeframe.ONE_HOUR):
            step = timedelta(minutes=timeframe.minutes)
            rows = list(session.scalars(select(CandleORM).where(
                CandleORM.symbol == Instrument.BTC_USD,
                CandleORM.timeframe == timeframe,
                CandleORM.timestamp >= self.start - timedelta(days=15),
                CandleORM.timestamp + step <= self.end,
            ).order_by(CandleORM.timestamp)))
            self.rows[timeframe] = rows
            self.ends[timeframe] = [_utc(row.timestamp) + step for row in rows]
        self.decisions = list(session.execute(select(
            AgentDecisionORM.id, AgentDecisionORM.created_at,
            AgentDecisionORM.decision, AgentDecisionORM.open_proposal,
            AgentDecisionORM.position_actions,
        ).where(
            AgentDecisionORM.instrument == Instrument.BTC_USD.value,
            AgentDecisionORM.created_at >= self.start,
            AgentDecisionORM.created_at < self.end,
        ).order_by(AgentDecisionORM.created_at)))
        decision_ids = [row.id for row in self.decisions]
        self.original_position_by_decision = {
            row.agent_decision_id: str(row.id)
            for row in session.execute(select(PaperPositionORM.id, PaperPositionORM.agent_decision_id).where(
                PaperPositionORM.agent_decision_id.in_(decision_ids)))
            if row.agent_decision_id is not None
        }

    def _available(self, timeframe: Timeframe, at: datetime, count: int) -> list[CandleORM]:
        end_index = bisect_right(self.ends[timeframe], at)
        return self.rows[timeframe][max(0, end_index-count):end_index]

    def _scalp_stop_ok(self, at: datetime, side: PaperSide, entry: Decimal,
                       stop: Decimal) -> bool:
        rows = self._available(Timeframe.FIVE_MINUTES, at, 100)
        if not rows or at - (_utc(rows[-1].timestamp) + timedelta(minutes=5)) > timedelta(minutes=10):
            return False
        structure = calculate_market_structure([CandleRepository._to_candle(row) for row in rows],
                                                swing_candles=SCALP_PARAMETERS_V1["swing_candles"])
        if structure["atr14"] is None or structure["swing_low"] is None:
            return False
        atr = dec(structure["atr14"])
        buffer = atr * dec(SCALP_PARAMETERS_V1["stop_buffer_atr"])
        minimum = atr * dec(SCALP_PARAMETERS_V1["minimum_stop_atr"])
        return (stop <= min(entry-minimum, dec(structure["swing_low"])-buffer)
                if side is PaperSide.LONG else
                stop >= max(entry+minimum, dec(structure["swing_high"])+buffer))

    def _m15_stop_ok(self, at: datetime, side: PaperSide, entry: Decimal,
                     stop: Decimal) -> bool:
        if self.risk.min_stop_m15_atr_multiple <= ZERO:
            return True
        rows = self._available(Timeframe.FIFTEEN_MINUTES, at, 100)
        if not rows or at - (_utc(rows[-1].timestamp) + timedelta(minutes=15)) > timedelta(minutes=30):
            return False
        structure = calculate_market_structure([CandleRepository._to_candle(row) for row in rows],
                                                swing_candles=self.risk.stop_swing_m15_candles)
        if structure["atr14"] is None or structure["swing_low"] is None:
            return False
        atr = dec(structure["atr14"])
        buffer = atr*self.risk.stop_swing_atr_buffer
        minimum = atr*self.risk.min_stop_m15_atr_multiple
        return (stop <= min(entry-minimum, dec(structure["swing_low"])-buffer)
                if side is PaperSide.LONG else
                stop >= max(entry+minimum, dec(structure["swing_high"])+buffer))

    def _scalp_risk_ok(self, trades: list[dict], cash: Decimal,
                       at: datetime, openings: list[datetime]) -> bool:
        today = [trade for trade in trades if datetime.fromisoformat(trade["exit_at"]).date() == at.date()]
        if sum((dec(trade["net_pnl"]) for trade in today), ZERO) <= -cash*self.risk.scalp_daily_loss_stop:
            return False
        streak = 0
        for trade in reversed(trades):
            if dec(trade["net_pnl"]) >= ZERO:
                break
            streak += 1
        if streak >= self.risk.scalp_max_consecutive_losses:
            return False
        if trades and dec(trades[-1]["net_pnl"]) < ZERO and (
            at-datetime.fromisoformat(trades[-1]["exit_at"]) <
            timedelta(minutes=self.risk.scalp_loss_cooldown_minutes)):
            return False
        if sum(at-timedelta(hours=1) < opened <= at for opened in openings) >= self.risk.scalp_max_openings_per_hour:
            return False
        return True

    def _signal(self, strategy: str, at: datetime, state: dict) -> tuple[PaperSide, Decimal, Decimal, str] | None:
        if strategy.startswith("M5_"):
            if at.minute % 5 or at.second:
                return None
            timeframes = {}
            for timeframe in (Timeframe.FIVE_MINUTES, Timeframe.FIFTEEN_MINUTES, Timeframe.ONE_HOUR):
                available = self._available(timeframe, at, 201)
                candles = [CandleRepository._to_candle(row) for row in available]
                structure = calculate_market_structure(candles)
                timeframes[timeframe.value] = {
                    "last_closed_at": (self.ends[timeframe][bisect_right(self.ends[timeframe], at)-1].isoformat()
                                       if available else None),
                    "consecutive_count": structure["consecutive_count"],
                    "market_structure": structure,
                    "recent_closed_candles": [c.model_dump(mode="json", exclude={"instrument", "timeframe"})
                                              for c in candles[-10:]],
                }
            candidates = build_scalp_candidates({"as_of": at.isoformat(), "timeframes": timeframes})["candidates"]
            selected = next((c for c in candidates if c["setup_type"] == strategy and
                             c["reason_no_trade"] is None), None)
            if selected is None:
                return None
            side = PaperSide(selected["direction"])
            level_mid = dec(selected["level"])
            level = level_mid + (Decimal("10") if side is PaperSide.LONG else -Decimal("10"))
            entry = dec(selected["reference_entry"])
            if (side is PaperSide.LONG and level >= entry) or (side is PaperSide.SHORT and level <= entry):
                return None
            state["pending_plan"] = {"side": side, "level": level, "at": at,
                                     "expires_at": at + timedelta(minutes=15)}
            return side, dec(selected["stop_loss"]), dec(selected["take_profit"]), strategy
        if strategy == "agent_baseline":
            for decision in state.get("due_decisions", []):
                proposal = decision.open_proposal
                if proposal and decision.decision in {"BUY", "SELL"}:
                    valid_until = datetime.fromisoformat(proposal["valid_until"])
                    if at <= _utc(valid_until) and proposal.get("stop_loss") and proposal.get("take_profit"):
                        state["opening_decision_id"] = decision.id
                        return (PaperSide.LONG if decision.decision == "BUY" else PaperSide.SHORT,
                                dec(proposal["stop_loss"]), dec(proposal["take_profit"]),
                                f"agent:{decision.id}")
            return None
        if at.minute % 15 or at.second:
            return None
        m15_full = self._available(Timeframe.FIFTEEN_MINUTES, at, 201)
        m15 = m15_full[-23:]
        h1 = self._available(Timeframe.ONE_HOUR, at, 3)
        if len(m15) < 22:
            return None
        structure = calculate_market_structure([CandleRepository._to_candle(row) for row in m15_full],
                                               swing_candles=self.risk.stop_swing_m15_candles)
        atr = dec(structure["atr14"]) if structure["atr14"] is not None else None
        if atr is None or atr <= ZERO:
            return None
        previous = m15[-2]
        current = m15[-1]
        closes = [row.close for row in m15]
        side = None
        label = ""
        if strategy == "trend":
            if len(h1) < 2:
                return None
            avg = _mean(closes[-20:])
            if current.close > avg and h1[-1].close > h1[-2].close:
                side, label = PaperSide.LONG, "h1_m15_trend"
            elif current.close < avg and h1[-1].close < h1[-2].close:
                side, label = PaperSide.SHORT, "h1_m15_trend"
        elif strategy == "breakout_retest":
            breakout = state.get("breakout")
            if breakout and at - breakout["at"] <= timedelta(hours=1):
                level = breakout["level"]
                if breakout["side"] is PaperSide.LONG and current.low <= level < current.close:
                    side, label = PaperSide.LONG, "breakout_retest"
                elif breakout["side"] is PaperSide.SHORT and current.high >= level > current.close:
                    side, label = PaperSide.SHORT, "breakout_retest"
                if side:
                    state.pop("breakout", None)
            else:
                state.pop("breakout", None)
            prior20 = m15[-21:-1]
            high = max(row.high for row in prior20)
            low = min(row.low for row in prior20)
            if current.close > high:
                state["breakout"] = {"side": PaperSide.LONG, "level": high, "at": at}
            elif current.close < low:
                state["breakout"] = {"side": PaperSide.SHORT, "level": low, "at": at}
        elif strategy == "mean_reversion":
            old_band = m15[-22:-2]
            new_band = m15[-21:-1]
            old_avg, new_avg = _mean([row.close for row in old_band]), _mean([row.close for row in new_band])
            old_dev = _std([row.close for row in old_band])
            new_dev = _std([row.close for row in new_band])
            if previous.close < old_avg - 2*old_dev and current.close >= new_avg - 2*new_dev:
                side, label = PaperSide.LONG, "m15_bb_reentry"
            elif previous.close > old_avg + 2*old_dev and current.close <= new_avg + 2*new_dev:
                side, label = PaperSide.SHORT, "m15_bb_reentry"
        else:
            raise ValueError(f"Unknown strategy {strategy}")
        if side is None:
            return None
        swing = m15[-self.risk.stop_swing_m15_candles:]
        if side is PaperSide.LONG:
            stop = min(min(row.low for row in swing) - atr*self.risk.stop_swing_atr_buffer,
                       current.close - atr*self.risk.min_stop_m15_atr_multiple)
            if stop >= current.close:
                return None
            target = current.close + (current.close - stop) * 2
        else:
            stop = max(max(row.high for row in swing) + atr*self.risk.stop_swing_atr_buffer,
                       current.close + atr*self.risk.min_stop_m15_atr_multiple)
            if stop <= current.close:
                return None
            target = current.close - (stop - current.close) * 2
        return side, stop, target, label

    def run(self, strategy: str, split: str, start: datetime, end: datetime) -> dict:
        bars = [row for row in self.rows[Timeframe.ONE_MINUTE]
                if start <= _utc(row.timestamp) and _utc(row.timestamp) + timedelta(minutes=1) <= end]
        state = {"decisions": [row for row in self.decisions if start <= _utc(row.created_at) < end]}
        cash = Decimal("1000")
        position = None
        trades = []
        accepted = rejected = signals = stale = errors = exposure_bars = 0
        pending_sent = pending_filled = 0
        scalp_openings = []
        equity_peak = cash
        max_drawdown = ZERO
        max_drawdown_fraction = ZERO
        prev_at = None
        for bar in bars:
            at = _utc(bar.timestamp)
            if strategy == "agent_baseline":
                due = []
                while state["decisions"] and _utc(state["decisions"][0].created_at) <= at:
                    due.append(state["decisions"].pop(0))
                state["due_decisions"] = due
            if prev_at is not None and at != prev_at + timedelta(minutes=1):
                stale += 1
                state.pop("pending", None)
                if position:
                    exit_price = execution_price(bar.open, position["side"], False, self.rates)
                    cash = self._close(position, exit_price, at, "DATA_GAP", cash, trades)
                    position = None
            prev_at = at
            closed_this_bar = False
            if strategy == "agent_baseline" and position:
                closing = next((decision for decision in state["due_decisions"]
                                for action in decision.position_actions or []
                                if action.get("action") == "CLOSE" and
                                action.get("position_id") == position.get("original_position_id")), None)
                if closing:
                    cash = self._close(position, execution_price(bar.open, position["side"], False, self.rates),
                                       at, "AGENT", cash, trades, "next_1m_open_after_decision")
                    position = None
            if position:
                exposure_bars += 1
                favorable = (execution_price(bar.high if position["side"] is PaperSide.LONG else bar.low,
                                             position["side"], False, self.rates))
                adverse = (execution_price(bar.low if position["side"] is PaperSide.LONG else bar.high,
                                           position["side"], False, self.rates))
                position["mfe_usd"] = max(position.get("mfe_usd", ZERO),
                                          gross_pnl(position["side"], position["entry"], favorable,
                                                    position["quantity"]))
                position["mae_usd"] = min(position.get("mae_usd", ZERO),
                                          gross_pnl(position["side"], position["entry"], adverse,
                                                    position["quantity"]))
                reason, both, fill, assumption = barrier_touch(
                    position["side"], bar, position["stop"], position["target"], self.rates)
                if reason:
                    cash = self._close(position, fill, at + timedelta(minutes=1), reason, cash,
                                       trades, assumption)
                    position = None
                    closed_this_bar = True
                elif strategy.startswith("M5_") and at + timedelta(minutes=1) >= position["entry_at"] + timedelta(minutes=60):
                    cash = self._close(position, execution_price(bar.close, position["side"], False, self.rates),
                                       at + timedelta(minutes=1), "TIMEOUT", cash, trades)
                    position = None
                    closed_this_bar = True
            pending = state.get("pending")
            if pending and not position and at > pending["at"]:
                if at >= pending["expires_at"]:
                    state.pop("pending", None)
                else:
                    fill, assumption = evaluate_order_candle(pending["order_type"], pending["level"],
                                                              bar, self.rates.spread_usd_per_btc)
                    if fill is not None:
                        rr = risk_reward_for_rates(self.rates, pending["side"], fill,
                                                   pending["stop"], pending["target"], Decimal("1"))["rr_ratio"]
                        quantity, reject_reason = RiskEngine(self.risk).size(
                            side=pending["side"], entry=fill, stop_loss=pending["stop"],
                            take_profit=pending["target"], equity=cash, cash=cash,
                            reserved=ZERO, current_risk=ZERO, position_count=0, rates=self.rates,
                            risk_fraction_override=self.risk.scalp_max_risk_per_position)
                        if (rr is None or rr < MIN_NET_RISK_REWARD or quantity is None or
                            not self._scalp_stop_ok(at, pending["side"], fill, pending["stop"]) or
                            not self._scalp_risk_ok(trades, cash, at, scalp_openings)):
                            rejected += 1
                        else:
                            pending_filled += 1
                            accepted += 1
                            scalp_openings.append(at)
                            h1_context = self._available(Timeframe.ONE_HOUR, at, 2)
                            regime = ("UP" if len(h1_context) == 2 and h1_context[-1].close > h1_context[-2].close
                                      else "DOWN" if len(h1_context) == 2 and h1_context[-1].close < h1_context[-2].close
                                      else "RANGE_OR_UNKNOWN")
                            position = {"side": pending["side"], "entry": fill, "quantity": quantity,
                                        "stop": pending["stop"], "target": pending["target"],
                                        "entry_at": at + timedelta(minutes=1), "setup": pending["setup"],
                                        "regime": regime, "signal_at": pending["at"],
                                        "fill_assumption": assumption}
                        state.pop("pending", None)
            if not closed_this_bar:
                signal = self._signal(strategy, at, state)
                if signal:
                    signals += 1
                    if position or state.get("pending") or (strategy.startswith("M5_") and
                        not self._scalp_risk_ok(trades, cash, at, scalp_openings)):
                        rejected += 1
                        state.pop("pending_plan", None)
                    else:
                        side, stop, target, label = signal
                        if strategy.startswith("M5_"):
                            plan = state.pop("pending_plan")
                            state["pending"] = {**plan, "stop": stop, "target": target,
                                                "setup": label,
                                                "order_type": "BUY_LIMIT" if side is PaperSide.LONG else "SELL_LIMIT"}
                            pending_sent += 1
                        else:
                            entry = execution_price(bar.open, side, True, self.rates)
                            quantity, reject_reason = RiskEngine(self.risk).size(
                                side=side, entry=entry, stop_loss=stop, take_profit=target,
                                equity=cash, cash=cash, reserved=ZERO, current_risk=ZERO,
                                position_count=0, rates=self.rates)
                            rr = risk_reward_for_rates(self.rates, side, entry, stop, target,
                                                       Decimal("1"))["rr_ratio"]
                            if (quantity is None or rr is None or rr < MIN_NET_RISK_REWARD or
                                not self._m15_stop_ok(at, side, entry, stop)):
                                rejected += 1
                            else:
                                accepted += 1
                                h1_context = self._available(Timeframe.ONE_HOUR, at, 2)
                                regime = ("UP" if len(h1_context) == 2 and h1_context[-1].close > h1_context[-2].close
                                          else "DOWN" if len(h1_context) == 2 and h1_context[-1].close < h1_context[-2].close
                                          else "RANGE_OR_UNKNOWN")
                                position = {"side": side, "entry": entry, "quantity": quantity,
                                            "stop": stop, "target": target, "entry_at": at,
                                            "setup": label, "regime": regime, "signal_at": at,
                                            "original_position_id": self.original_position_by_decision.get(
                                                state.get("opening_decision_id")) if strategy == "agent_baseline" else None}
            marked = cash + (gross_pnl(position["side"], position["entry"],
                          execution_price(bar.close, position["side"], False, self.rates),
                          position["quantity"]) if position else ZERO)
            equity_peak = max(equity_peak, marked)
            max_drawdown = max(max_drawdown, equity_peak - marked)
            max_drawdown_fraction = max(max_drawdown_fraction, (equity_peak-marked)/equity_peak if equity_peak else ZERO)
        if position and bars:
            last = bars[-1]
            cash = self._close(position, execution_price(last.close, position["side"], False, self.rates),
                               _utc(last.timestamp) + timedelta(minutes=1), "END_OF_RANGE", cash, trades)
        wins = [trade for trade in trades if dec(trade["net_pnl"]) > ZERO]
        losses = [trade for trade in trades if dec(trade["net_pnl"]) < ZERO]
        gross_profit = sum((dec(trade["net_pnl"]) for trade in wins), ZERO)
        gross_loss = -sum((dec(trade["net_pnl"]) for trade in losses), ZERO)
        pnl = cash - Decimal("1000")
        gross_before_spread = sum((dec(trade["gross_pnl"]) for trade in trades), ZERO)
        n = Decimal(len(trades))
        p = Decimal(len(wins))/n if n else None
        z = Decimal("1.96")
        denominator = 1 + z*z/n if n else None
        win_ci = ([str((p + z*z/(2*n) - z*(p*(1-p)/n + z*z/(4*n*n)).sqrt())/denominator),
                   str((p + z*z/(2*n) + z*(p*(1-p)/n + z*z/(4*n*n)).sqrt())/denominator)]
                  if n else None)
        return {"strategy": strategy, "split": split, "start": start.isoformat(), "end": end.isoformat(),
                "signals": signals, "accepted": accepted, "rejected": rejected,
                "pending_sent": pending_sent, "pending_filled": pending_filled,
                "pending_fill_rate": str(Decimal(pending_filled)/pending_sent) if pending_sent else None,
                "trade_count": len(trades), "win_rate": str(Decimal(len(wins))/len(trades)) if trades else None,
                "win_rate_ci95_wilson": win_ci,
                "gross_pnl": str(gross_before_spread), "net_pnl": str(pnl), "profit_factor": str(gross_profit/gross_loss) if gross_loss else None,
                "expectancy_per_trade": str(pnl/len(trades)) if trades else None,
                "average_win": str(gross_profit/len(wins)) if wins else None,
                "average_loss": str(-gross_loss/len(losses)) if losses else None,
                "max_drawdown_usd": str(max_drawdown),
                "max_drawdown_fraction": str(max_drawdown_fraction),
                "exposure_fraction": str(Decimal(exposure_bars)/len(bars)) if bars else None,
                "fee_usd": "0", "spread_cost_usd": str(sum((dec(trade["spread_cost_usd"]) for trade in trades), ZERO)),
                "average_r_multiple": str(_mean([dec(trade["r_multiple"]) for trade in trades])) if trades else None,
                "average_duration_minutes": str(_mean([dec(trade["duration_minutes"]) for trade in trades])) if trades else None,
                "average_mfe_usd": str(_mean([dec(trade["mfe_usd"]) for trade in trades])) if trades else None,
                "average_mae_usd": str(_mean([dec(trade["mae_usd"]) for trade in trades])) if trades else None,
                "worst_loss_streak": max((sum(1 for _ in values) for is_loss, values in groupby(
                    [dec(trade["net_pnl"]) < ZERO for trade in trades]) if is_loss), default=0),
                "by_exit_reason": dict(Counter(trade["exit_reason"] for trade in trades)),
                "by_entry_hour_utc": dict(Counter(trade["entry_at"][11:13] for trade in trades)),
                "by_setup": dict(Counter(trade["setup"] for trade in trades)),
                "by_regime": dict(Counter(trade["regime"] for trade in trades)),
                "stale_data_gaps": stale,
                "stale_data_rate": str(Decimal(stale)/(len(bars)+stale)) if bars or stale else None,
                "errors": errors, "llm_calls": 0, "llm_cost_usd": "0",
                "trades": trades}

    @staticmethod
    def _close(position: dict, exit_price: Decimal, at: datetime, reason: str,
               cash: Decimal, trades: list, assumption: str = "closed_1m_ohlc") -> Decimal:
        gross = gross_pnl(position["side"], position["entry"], exit_price, position["quantity"])
        offset = Decimal("10")
        entry_mid = position["entry"] - offset if position["side"] is PaperSide.LONG else position["entry"] + offset
        exit_mid = exit_price + offset if position["side"] is PaperSide.LONG else exit_price - offset
        before_spread = gross_pnl(position["side"], entry_mid, exit_mid, position["quantity"])
        risk = abs(position["entry"] - position["stop"]) * position["quantity"]
        trades.append({"side": position["side"].value, "setup": position["setup"],
            "regime": position["regime"],
            "entry_at": position["entry_at"].isoformat(), "exit_at": at.isoformat(),
            "entry_price": str(position["entry"]), "exit_price": str(exit_price),
            "quantity_btc": str(position["quantity"]), "exit_reason": reason,
            "fill_assumption": assumption, "gross_pnl": str(before_spread), "net_pnl": str(gross),
            "fee_usd": "0", "spread_cost_usd": str(position["quantity"] * Decimal("20")),
            "r_multiple": str(gross/risk) if risk else None,
            "mfe_usd": str(position.get("mfe_usd", ZERO)),
            "mae_usd": str(position.get("mae_usd", ZERO)),
            "duration_minutes": str((at-position["entry_at"]).total_seconds()/60)})
        return cash + gross

    def compare(self) -> dict:
        split_at = self.start + (self.end-self.start)/2
        split_at = split_at.replace(second=0, microsecond=0)
        runs = []
        for split, start, end in (("in_sample", self.start, split_at),
                                  ("out_of_sample", split_at, self.end)):
            for strategy in ("agent_baseline", "trend", "breakout_retest", "mean_reversion",
                             "M5_PULLBACK_RETEST", "M5_RANGE_REJECTION", "M5_BREAKOUT_RETEST"):
                runs.append(self.run(strategy, split, start, end))
        return {"model": "CFD20 | spread $20/BTC | fee 0 | simulation only",
                "data_policy": "Closed OHLC only; baseline entries at next M1 open; M5 LIMIT orders activate on a later M1 bar; no tick sequence, no account writes. In/out-of-sample halves are descriptive, not proof of edge.",
                "scalp_parameters_version": "SCALP_M5_V1",
                "runs": runs}
