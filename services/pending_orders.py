"""Deterministic PAPER orders evaluated on complete Kraken M1 OHLC bars.

LIMIT fills at the limit or a better opening price. STOP fills at its trigger
or a worse opening price after a gap. There is no intrabar path or CFD quote
history: each fill is an explicit closed_1m_ohlc assumption.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models.candle import CandleORM
from database.models.agent_decision import AgentDecisionORM
from database.models.paper import PaperOrderEventORM, PaperPendingOrderORM, PaperPositionORM
from models.agent_decision import OpenProposal, PendingActionType, PendingOrderAction, PendingInvalidation, TriggerCondition
from models.market import Instrument, Timeframe
from models.paper import CFD_FIXED_20_USD, MIN_NET_RISK_REWARD, FeeRates, PaperSide, RiskEngine, ZERO, dec, estimated_target_net_pnl, risk_reward_for_rates
from models.strategy import SCALP_PARAMETERS_V1, Strategy, validate_strategy_setup
from services.paper_trading import MINUTE, PaperTradingService, iso_utc, require_utc, utc_now


def order_side(order_type: str) -> PaperSide:
    return PaperSide.LONG if order_type.startswith("BUY_") else PaperSide.SHORT


def executable_range(candle: CandleORM, order_type: str, spread: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    offset = spread / 2 if order_type.startswith("BUY_") else -spread / 2
    return candle.low + offset, candle.high + offset, candle.open + offset


def evaluate_order_candle(order_type: str, level: Decimal, candle: CandleORM,
                          spread: Decimal) -> tuple[Decimal | None, str | None]:
    low, high, opened = executable_range(candle, order_type, spread)
    if order_type == "BUY_LIMIT" and low <= level:
        return min(opened, level), "LIMIT_BETTER_OPEN" if opened < level else "LIMIT_TOUCH_OHLC"
    if order_type == "SELL_LIMIT" and high >= level:
        return max(opened, level), "LIMIT_BETTER_OPEN" if opened > level else "LIMIT_TOUCH_OHLC"
    if order_type == "BUY_STOP" and high >= level:
        return max(opened, level), "STOP_GAP_OPEN" if opened > level else "STOP_TOUCH_OHLC"
    if order_type == "SELL_STOP" and low <= level:
        return min(opened, level), "STOP_GAP_OPEN" if opened < level else "STOP_TOUCH_OHLC"
    return None, None


class PendingOrderManager:
    def __init__(self, session: Session, paper: PaperTradingService | None = None):
        self.session = session
        self.paper = paper or PaperTradingService(session)

    def snapshot(self, now: datetime | None = None) -> list[dict]:
        account = self.paper._account()
        if account is None:
            return []
        now = now or utc_now()
        rows = self.session.scalars(select(PaperPendingOrderORM).where(
            PaperPendingOrderORM.account_id == account.id,
            PaperPendingOrderORM.status == "PENDING",
        ).order_by(PaperPendingOrderORM.created_at, PaperPendingOrderORM.id)).all()
        return [{
            "id": str(row.id), "status": row.status, "revision": row.revision,
            "side": row.side, "order_type": row.order_type,
            "entry_level_usd": str(row.entry_level_usd), "stop_loss": str(row.stop_loss),
            "take_profit": str(row.take_profit), "scenario_key": row.scenario_key,
            "setup_type": row.setup_type, "strategy": row.strategy or Strategy.INTRADAY_M15.value,
            "reason": row.reason,
            "invalidating_condition": row.invalidating_condition,
            "quantity_policy": row.quantity_policy,
            "created_at": iso_utc(row.created_at), "updated_at": iso_utc(row.updated_at),
            "activated_at": iso_utc(row.activated_at),
            "expires_at": iso_utc(row.expires_at), "fee_profile_version": row.fee_snapshot["version"],
            "ttl_seconds": max(0, int((row.expires_at-now).total_seconds())),
            "execution_model": row.fee_snapshot["execution_model"],
        } for row in rows]

    def recent_history(self, limit: int = 20) -> list[dict]:
        account = self.paper._account()
        if account is None:
            return []
        rows = self.session.scalars(select(PaperPendingOrderORM).where(
            PaperPendingOrderORM.account_id == account.id,
            PaperPendingOrderORM.status != "PENDING",
        ).order_by(PaperPendingOrderORM.updated_at.desc()).limit(limit)).all()
        return [{"id": str(row.id), "order_type": row.order_type,
                 "status": row.status, "entry_level_usd": str(row.entry_level_usd),
                 "position_id": str(row.position_id) if row.position_id else None,
                 "updated_at": iso_utc(row.updated_at), "reason": row.cancel_reason,
                 "trigger_trace": row.trigger_trace} for row in rows]

    def _event(self, row: PaperPendingOrderORM, type_: str, now: datetime,
               payload: dict, decision_id: UUID | None = None) -> None:
        self.session.execute(insert(PaperOrderEventORM).values(
            id=uuid4(), account_id=row.account_id, order_id=row.id,
            decision_id=decision_id, type=type_, event_time=now,
            payload=payload, dedupe_key=f"pending:{row.id}:{row.revision}:{type_}:{decision_id if type_ == 'HOLD' else ''}",
            notification_status="DISABLED",
            notification_attempts=0,
        ).on_conflict_do_nothing(constraint="uq_paper_order_event_dedupe"))

    @staticmethod
    def _level_error(order_type: str, entry: Decimal, stop: Decimal, target: Decimal,
                     current_executable: Decimal, rates: FeeRates) -> str | None:
        side = order_side(order_type)
        if side is PaperSide.LONG and not stop < entry < target:
            return "LONG requires SL < entry < TP"
        if side is PaperSide.SHORT and not target < entry < stop:
            return "SHORT requires TP < entry < SL"
        if order_type == "BUY_LIMIT" and not entry < current_executable:
            return "REJECTED_INVALID_SIDE: BUY_LIMIT must be below current CFD ask"
        if order_type == "BUY_STOP" and not entry > current_executable:
            return "REJECTED_INVALID_SIDE: BUY_STOP must be above current CFD ask"
        if order_type == "SELL_LIMIT" and not entry > current_executable:
            return "REJECTED_INVALID_SIDE: SELL_LIMIT must be above current CFD bid"
        if order_type == "SELL_STOP" and not entry < current_executable:
            return "REJECTED_INVALID_SIDE: SELL_STOP must be below current CFD bid"
        net = estimated_target_net_pnl(rates, side, entry, target, Decimal("1"), stop, 0)
        if net <= ZERO:
            return "Target has no positive executable net PnL"
        rr = risk_reward_for_rates(rates, side, entry, stop, target, Decimal("1"))
        if rr["rr_ratio"] is None or rr["rr_ratio"] < MIN_NET_RISK_REWARD:
            return "INSUFFICIENT_RISK_REWARD"
        return None

    @staticmethod
    def _invalidation_level_error(side: PaperSide, stop: Decimal, condition) -> str | None:
        if condition is None:
            return None
        if side is PaperSide.LONG and condition.operator is TriggerCondition.CLOSE_BELOW and stop > condition.level:
            return "INVALID_SL_STRUCTURE: LONG stop must protect CLOSE_BELOW invalidation"
        if side is PaperSide.SHORT and condition.operator is TriggerCondition.CLOSE_ABOVE and stop < condition.level:
            return "INVALID_SL_STRUCTURE: SHORT stop must protect CLOSE_ABOVE invalidation"
        return None

    def _reject_rr(self, account, decision_id: UUID, key: str, now: datetime,
                   source: str, side: PaperSide, entry: Decimal, stop: Decimal,
                   target: Decimal, rates: FeeRates) -> dict:
        rr = risk_reward_for_rates(rates, side, entry, stop, target, Decimal("1"))
        return self.paper._reject(account, key, "INSUFFICIENT_RISK_REWARD", now, decision_id, {
            "code": "INSUFFICIENT_RISK_REWARD", "source": source,
            "entry": str(entry), "SL": str(stop), "TP": str(target),
            "quantity_btc": "1", "quantity_basis": "per_1_btc_preflight",
            "risk_net_usd": str(rr["risk_net_usd"]), "reward_net_usd": str(rr["reward_net_usd"]),
            "rr_ratio": str(rr["rr_ratio"]) if rr["rr_ratio"] is not None else None,
            "min_rr": str(MIN_NET_RISK_REWARD), "fee_profile_version": rates.version,
        })

    def _capacity_error(self, account, active: list[PaperPendingOrderORM], order_type: str,
                        entry: Decimal, stop: Decimal, target: Decimal, rates: FeeRates,
                        mid: Decimal, now: datetime,
                        strategy: Strategy = Strategy.INTRADAY_M15) -> str | None:
        if not self.paper._feed_is_contiguous(now):
            return "Closed 1m market data is stale or gapped"
        side = order_side(order_type)
        positions = self.paper._positions(account.id, lock=True)
        if len(positions) + len(active) + 1 > self.paper.risk.max_positions:
            return "Potential position count exceeds PAPER limit"
        if any(position.side != side.value for position in positions):
            return "Opposing open position exists"
        if any(position.scenario_key == existing.scenario_key for position in positions for existing in active):
            return "An active pending scenario already has an open position"
        structural = self.paper.validate_stop_structure(side, entry, stop, now, strategy)
        if structural:
            return structural
        values = self.paper.mark_to_market(mid)
        equity = values["equity_estimate"]
        if strategy is Strategy.SCALP_M5:
            scalp_error = self.paper.scalp_risk_error(account, equity, now)
            if scalp_error:
                return scalp_error
        reserved = values["reserved_margin"]
        current_risk = sum((position.quantity_btc * abs(position.entry_price - position.stop_loss)
                            for position in positions if position.stop_loss is not None), ZERO)
        for existing in active:
            past = FeeRates.from_dict(existing.fee_snapshot)
            quantity, reason = RiskEngine(self.paper.risk).size(
                side=PaperSide(existing.side), entry=existing.entry_level_usd,
                stop_loss=existing.stop_loss, take_profit=existing.take_profit,
                equity=equity, cash=account.cash_balance, reserved=reserved,
                current_risk=current_risk, position_count=len(positions), rates=past,
                risk_fraction_override=(self.paper.risk.scalp_max_risk_per_position
                                        if existing.strategy == Strategy.SCALP_M5.value else None))
            if quantity is None:
                return f"Potential exposure of existing order {existing.id} cannot be sized: {reason}"
            reserved += quantity * existing.entry_level_usd
            current_risk += quantity * abs(existing.entry_level_usd - existing.stop_loss)
        quantity, reason = RiskEngine(self.paper.risk).size(
            side=side, entry=entry, stop_loss=stop, take_profit=target,
            equity=equity, cash=account.cash_balance, reserved=reserved,
            current_risk=current_risk, position_count=len(positions), rates=rates,
            risk_fraction_override=(self.paper.risk.scalp_max_risk_per_position
                                    if strategy is Strategy.SCALP_M5 else None))
        return None if quantity is not None else f"Potential exposure cannot be sized: {reason}"

    @staticmethod
    def _quote_mid(ticker: dict, now: datetime, decision_at: datetime) -> Decimal:
        quote_at = datetime.fromisoformat(ticker["retrieved_at"])
        require_utc(quote_at)
        if quote_at <= decision_at or now - quote_at > timedelta(seconds=30) or quote_at > now + timedelta(seconds=5):
            raise ValueError("Fresh post-analysis quote required")
        if ("bid" in ticker) != ("ask" in ticker):
            raise ValueError("Bid and ask must be supplied together")
        if "bid" in ticker:
            bid, ask = dec(ticker["bid"]), dec(ticker["ask"])
            if bid <= ZERO or ask < bid or not bid.is_finite() or not ask.is_finite():
                raise ValueError("Invalid bid/ask quote")
            return (bid + ask) / 2
        mid = dec(ticker["price"])
        if mid <= ZERO or not mid.is_finite():
            raise ValueError("Invalid last trade price")
        return mid

    def _invalidation_status(self, row: PaperPendingOrderORM, event_at: datetime) -> tuple[bool | None, dict]:
        condition = row.invalidating_condition
        if condition is None:
            return False, {}
        timeframe = Timeframe(condition["timeframe"])
        step = timedelta(minutes=timeframe.minutes)
        latest = self.session.scalar(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == timeframe,
            CandleORM.timestamp + step <= event_at,
        ).order_by(CandleORM.timestamp.desc()).limit(1))
        if latest is None or event_at - (latest.timestamp + step) > step * 2:
            return None, {"reason": f"Invalidation {timeframe.value} candle unavailable or stale"}
        closed_at = latest.timestamp + step
        if closed_at <= row.activated_at:
            return False, {}
        level = dec(condition["level"])
        triggered = (latest.close > level if condition["operator"] == TriggerCondition.CLOSE_ABOVE.value
                     else latest.close < level)
        return triggered, {"condition": condition, "candle_timestamp": iso_utc(latest.timestamp),
                           "candle_close": str(latest.close)}

    def apply_action(self, action: PendingOrderAction, decision_id: UUID, ticker: dict | None,
                     now: datetime | None = None, *, action_index: int = 0) -> dict:
        now = now or utc_now()
        require_utc(now)
        account = self.paper._account(lock=True)
        if account is None or account.status != "ACTIVE":
            return {"status": "REJECTED", "reason": "Paper account inactive or missing"}
        if action.action is PendingActionType.CREATE:
            setup_error = validate_strategy_setup(action.strategy, action.setup_type)
            if setup_error:
                return {"status": "REJECTED", "reason": setup_error}
            existing = self.session.scalar(select(PaperPendingOrderORM).where(
                PaperPendingOrderORM.execution_key == f"{decision_id}:pending:{action_index}"))
            if existing:
                return {"status": existing.status, "order_id": str(existing.id), "reason": "Already created"}
            if ticker is None:
                return {"status": "REJECTED", "reason": "Fresh quote required for CREATE"}
            decision_row = self.session.get(AgentDecisionORM, decision_id)
            if decision_row is None:
                return {"status": "REJECTED", "reason": "Unknown decision"}
            try:
                mid = self._quote_mid(ticker, now, decision_row.created_at)
            except (ValueError, KeyError):
                return {"status": "REJECTED", "reason": "Fresh valid post-analysis quote required"}
            profile = self.paper._fee_profile(account.id)
            if profile is None:
                return {"status": "REJECTED", "reason": "CFD20 profile missing"}
            rates = self.paper._rates(profile)
            if rates.execution_model != CFD_FIXED_20_USD:
                return {"status": "REJECTED", "reason": "CFD20 profile not active"}
            active = list(self.session.scalars(select(PaperPendingOrderORM).where(
                PaperPendingOrderORM.account_id == account.id,
                PaperPendingOrderORM.status == "PENDING").with_for_update()))
            if len(active) >= self.paper.risk.pending_max_active:
                return {"status": "REJECTED", "reason": "Maximum active pending orders reached"}
            if any(row.scenario_key == action.scenario_key for row in active):
                return {"status": "REJECTED", "reason": "Scenario already has an active order"}
            if any(row.side != order_side(action.order_type.value).value for row in active):
                return {"status": "REJECTED", "reason": "Opposing pending orders disabled"}
            if action.expires_at <= now or action.expires_at > now + timedelta(minutes=self.paper.risk.pending_max_ttl_minutes):
                return {"status": "REJECTED", "reason": "TTL exceeds configured maximum"}
            current = mid + (rates.spread_usd_per_btc / 2 if action.order_type.value.startswith("BUY_")
                             else -rates.spread_usd_per_btc / 2)
            if abs(action.entry_level_usd-current) > self.paper.risk.pending_max_distance_usd:
                return {"status": "REJECTED", "reason": "Entry level exceeds configured distance from quote"}
            error = self._level_error(action.order_type.value, action.entry_level_usd,
                                      action.stop_loss, action.take_profit, current, rates)
            error = error or self._invalidation_level_error(order_side(action.order_type.value),
                                                             action.stop_loss, action.invalidating_condition)
            if error:
                if error == "INSUFFICIENT_RISK_REWARD":
                    return self._reject_rr(account, decision_id, f"{decision_id}:pending:{action_index}", now,
                        "pending_create", order_side(action.order_type.value), action.entry_level_usd,
                        action.stop_loss, action.take_profit, rates)
                return {"status": "REJECTED", "reason": error}
            error = self._capacity_error(account, active, action.order_type.value,
                                         action.entry_level_usd, action.stop_loss, action.take_profit,
                                         rates, mid, now, action.strategy)
            if error:
                return {"status": "REJECTED", "reason": error}
            if any(position.scenario_key == action.scenario_key for position in self.paper._positions(account.id)):
                return {"status": "REJECTED", "reason": "Scenario already has an open position"}
            row = PaperPendingOrderORM(
                id=uuid4(), account_id=account.id, instrument=Instrument.BTC_USD.value,
                side=order_side(action.order_type.value).value,
                order_type=action.order_type.value, status="PENDING",
                entry_level_usd=action.entry_level_usd, stop_loss=action.stop_loss,
                take_profit=action.take_profit, scenario_key=action.scenario_key,
                setup_type=action.setup_type, strategy=action.strategy.value,
                strategy_parameters=(SCALP_PARAMETERS_V1.copy() if action.strategy is Strategy.SCALP_M5 else
                                     {"version": "INTRADAY_M15_V1", "swing_candles": self.paper.risk.stop_swing_m15_candles,
                                      "stop_buffer_atr": str(self.paper.risk.stop_swing_atr_buffer),
                                      "minimum_stop_atr": str(self.paper.risk.min_stop_m15_atr_multiple),
                                      "max_hold_hours": self.paper.risk.max_hold_hours}),
                reason=action.reason,
                invalidating_condition=action.invalidating_condition.model_dump(mode="json") if action.invalidating_condition else None,
                quantity_policy="AT_FILL_RISK_SIZED",
                created_by_decision_id=decision_id, last_updated_by_decision_id=decision_id,
                created_at=now, updated_at=now, activated_at=now, expires_at=action.expires_at,
                revision=1, fee_profile_id=profile.id, fee_snapshot=rates.as_dict(),
                execution_key=f"{decision_id}:pending:{action_index}",
                last_evaluated_candle_timestamp=None,
            )
            self.session.add(row)
            self.session.flush()
            self._event(row, "CREATED", now, {"market_mid": str(mid),
                "cfd_executable_price": str(current), "entry_level_usd": str(row.entry_level_usd),
                "quote_mode": "kraken_bid_ask_mid" if "bid" in ticker else "last_trade_mid_assumption",
                "execution_model": rates.execution_model,
                "order_type": row.order_type, "side": row.side,
                "stop_loss": str(row.stop_loss), "take_profit": str(row.take_profit),
                "expires_at": iso_utc(row.expires_at), "scenario_key": row.scenario_key,
                "setup_type": row.setup_type, "strategy": row.strategy,
                "strategy_parameters": row.strategy_parameters, "reason": row.reason,
                "invalidating_condition": row.invalidating_condition,
                "quantity_policy": row.quantity_policy,
                "fee_snapshot": row.fee_snapshot}, decision_id)
            return {"status": "PENDING", "order_id": str(row.id), "revision": row.revision}
        row = self.session.scalar(select(PaperPendingOrderORM).where(
            PaperPendingOrderORM.id == action.order_id,
            PaperPendingOrderORM.account_id == account.id).with_for_update())
        if row is None:
            return {"status": "REJECTED", "reason": "Unknown order ID"}
        if row.status != "PENDING":
            if row.status == "CANCELLED" and row.last_updated_by_decision_id == decision_id and action.action is PendingActionType.CANCEL:
                return {"status": "CANCELLED", "order_id": str(row.id), "reason": "Already cancelled"}
            return {"status": "ORDER_ALREADY_FILLED" if row.status == "FILLED" else "REJECTED",
                    "reason": f"Order is {row.status}"}
        if row.revision != action.expected_revision:
            return {"status": "REJECTED", "reason": "Revision conflict"}
        decision_row = self.session.get(AgentDecisionORM, decision_id)
        received = ((decision_row.analysis_trace or {}).get("paper_context") or {}).get("pending_orders") if decision_row else None
        if received is not None and not any(
            item.get("id") == str(row.id) and item.get("revision") == action.expected_revision
            for item in received):
            return {"status": "REJECTED", "reason": "Order ID/revision absent from analyst snapshot"}
        if action.action is PendingActionType.HOLD:
            self._event(row, "HOLD", now, {"reason": action.reason}, decision_id)
            return {"status": "PENDING", "order_id": str(row.id), "revision": row.revision}
        if action.action is PendingActionType.CANCEL:
            row.revision += 1
            row.updated_at = now
            row.last_updated_by_decision_id = decision_id
            row.status = "CANCELLED"
            row.cancelled_at = now
            row.cancel_reason = action.reason
            self._event(row, "CANCELLED", now, {"reason": action.reason}, decision_id)
            return {"status": row.status, "order_id": str(row.id)}
        # AMEND updates supplied fields only; a new complete set is validated.
        if action.strategy is None:
            return {"status": "REJECTED", "reason": "STRATEGY_REQUIRED"}
        if action.strategy.value != (row.strategy or Strategy.INTRADAY_M15.value):
            return {"status": "REJECTED", "reason": "Cannot change order strategy"}
        setup_error = validate_strategy_setup(action.strategy, action.setup_type or row.setup_type)
        if setup_error:
            return {"status": "REJECTED", "reason": setup_error}
        order_type = action.order_type.value if action.order_type else row.order_type
        level = action.entry_level_usd if action.entry_level_usd is not None else row.entry_level_usd
        stop = action.stop_loss if action.stop_loss is not None else row.stop_loss
        target = action.take_profit if action.take_profit is not None else row.take_profit
        expiry = action.expires_at or row.expires_at
        if expiry <= now or expiry > now + timedelta(minutes=self.paper.risk.pending_max_ttl_minutes) or expiry > row.created_at + timedelta(hours=4):
            return {"status": "REJECTED", "reason": "Invalid amended TTL"}
        if action.scenario_key is not None and action.scenario_key != row.scenario_key:
            return {"status": "REJECTED", "reason": "Scenario key is immutable"}
        if order_side(order_type).value != row.side:
            return {"status": "REJECTED", "reason": "Cannot change order side"}
        if ticker is None:
            return {"status": "REJECTED", "reason": "Fresh quote required for AMEND"}
        rates = FeeRates.from_dict(row.fee_snapshot)
        decision_row = self.session.get(AgentDecisionORM, decision_id)
        try:
            mid = self._quote_mid(ticker, now, decision_row.created_at)
        except (ValueError, KeyError, AttributeError):
            return {"status": "REJECTED", "reason": "Fresh valid post-analysis quote required"}
        current = mid + (rates.spread_usd_per_btc / 2 if order_type.startswith("BUY_") else -rates.spread_usd_per_btc / 2)
        if abs(level-current) > self.paper.risk.pending_max_distance_usd:
            return {"status": "REJECTED", "reason": "Entry level exceeds configured distance from quote"}
        error = self._level_error(order_type, level, stop, target, current, rates)
        error = error or self._invalidation_level_error(order_side(order_type), stop,
            action.invalidating_condition or (PendingInvalidation.model_validate(row.invalidating_condition)
                                               if row.invalidating_condition else None))
        if error:
            if error == "INSUFFICIENT_RISK_REWARD":
                return self._reject_rr(account, decision_id, f"{decision_id}:pending-amend:{row.id}", now,
                    "pending_amend", order_side(order_type), level, stop, target, rates)
            return {"status": "REJECTED", "reason": error}
        active = list(self.session.scalars(select(PaperPendingOrderORM).where(
            PaperPendingOrderORM.account_id == account.id,
            PaperPendingOrderORM.status == "PENDING",
            PaperPendingOrderORM.id != row.id).with_for_update()))
        error = self._capacity_error(account, active, order_type, level, stop, target, rates, mid, now,
                                     action.strategy)
        if error:
            return {"status": "REJECTED", "reason": error}
        row.revision += 1
        row.updated_at = now
        row.activated_at = now
        row.last_updated_by_decision_id = decision_id
        row.order_type, row.entry_level_usd, row.stop_loss, row.take_profit, row.expires_at = order_type, level, stop, target, expiry
        row.reason = action.reason
        if action.setup_type is not None:
            row.setup_type = action.setup_type
        if action.invalidating_condition is not None:
            row.invalidating_condition = action.invalidating_condition.model_dump(mode="json")
        row.last_evaluated_candle_timestamp = None
        self._event(row, "AMENDED", now, {"reason": action.reason,
            "entry_level_usd": str(level), "order_type": row.order_type, "side": row.side,
            "stop_loss": str(row.stop_loss), "take_profit": str(row.take_profit),
            "expires_at": iso_utc(row.expires_at), "scenario_key": row.scenario_key,
            "setup_type": row.setup_type, "invalidating_condition": row.invalidating_condition,
            "quantity_policy": row.quantity_policy, "fee_snapshot": row.fee_snapshot}, decision_id)
        return {"status": row.status, "order_id": str(row.id), "revision": row.revision}

    def process(self, now: datetime | None = None) -> dict:
        now = now or utc_now()
        require_utc(now)
        account = self.paper._account(lock=True)
        if account is None:
            return {"evaluated": 0, "filled": 0, "expired": 0}
        rows = list(self.session.scalars(select(PaperPendingOrderORM).where(
            PaperPendingOrderORM.account_id == account.id,
            PaperPendingOrderORM.status == "PENDING",
        ).order_by(PaperPendingOrderORM.created_at, PaperPendingOrderORM.id).with_for_update()))
        result = {"evaluated": 0, "filled": 0, "expired": 0}
        feed_ready = self.paper._feed_is_contiguous(now)
        for row in rows:
            if not feed_ready or account.status != "ACTIVE":
                if now >= row.expires_at:
                    row.revision += 1
                    row.status = "EXPIRED"
                    row.updated_at = now
                    row.cancel_reason = "TTL elapsed while market feed unavailable"
                    self._event(row, "EXPIRED", now, {"reason": row.cancel_reason}, row.created_by_decision_id)
                    result["expired"] += 1
                elif not self.session.scalar(select(PaperOrderEventORM.id).where(
                    PaperOrderEventORM.dedupe_key == f"pending:{row.id}:{row.revision}:SKIPPED_STALE_DATA:")):
                    self._event(row, "SKIPPED_STALE_DATA", now,
                                {"reason": "Closed M1 feed stale/gapped or account inactive"}, row.created_by_decision_id)
                continue
            start = row.activated_at.replace(second=0, microsecond=0)
            if start < row.activated_at:
                start += MINUTE
            if row.last_evaluated_candle_timestamp is not None:
                start = max(start, row.last_evaluated_candle_timestamp + MINUTE)
            candles = list(self.session.scalars(select(CandleORM).where(
                CandleORM.symbol == Instrument.BTC_USD,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
                CandleORM.timestamp >= start,
                CandleORM.timestamp + MINUTE <= min(now, row.expires_at),
            ).order_by(CandleORM.timestamp)))
            for index, candle in enumerate(candles):
                if candle.timestamp != start + index * MINUTE:
                    if not self.session.scalar(select(PaperOrderEventORM.id).where(
                        PaperOrderEventORM.dedupe_key == f"pending:{row.id}:{row.revision}:SKIPPED_STALE_DATA:")):
                        self._event(row, "SKIPPED_STALE_DATA", now,
                                    {"reason": "Missing historical M1 candle", "expected_at": iso_utc(start + index * MINUTE)},
                                    row.created_by_decision_id)
                    break  # data gap: do not infer an OHLC path
                row.last_evaluated_candle_timestamp = candle.timestamp
                result["evaluated"] += 1
                invalidated, invalidation_trace = self._invalidation_status(row, candle.timestamp + MINUTE)
                if invalidated is None:
                    row.last_evaluated_candle_timestamp = candle.timestamp - MINUTE
                    self._event(row, "SKIPPED_STALE_DATA", now, invalidation_trace, row.created_by_decision_id)
                    break
                if invalidated:
                    row.revision += 1
                    row.status = "CANCELLED"
                    row.updated_at = now
                    row.cancelled_at = now
                    row.cancel_reason = "Machine-checkable invalidation condition met"
                    self._event(row, "CANCELLED", now,
                                {"reason": row.cancel_reason, **invalidation_trace}, row.created_by_decision_id)
                    break
                rates = FeeRates.from_dict(row.fee_snapshot)
                fill_price, assumption = evaluate_order_candle(
                    row.order_type, row.entry_level_usd, candle, rates.spread_usd_per_btc)
                if fill_price is None:
                    continue
                side = PaperSide(row.side)
                mid = fill_price - rates.spread_usd_per_btc / 2 if side is PaperSide.LONG else fill_price + rates.spread_usd_per_btc / 2
                event_at = (candle.timestamp + MINUTE).astimezone(timezone.utc)
                proposal = OpenProposal(side=side.value, reason=row.reason,
                    stop_loss=row.stop_loss, take_profit=row.take_profit,
                    valid_until=row.expires_at.astimezone(timezone.utc),
                    strategy=Strategy(row.strategy or Strategy.INTRADAY_M15.value),
                    setup_type=row.setup_type)
                opened = self.paper.open_position(
                    proposal, mid, event_at, execution_key=f"pending-fill:{row.id}",
                    source="pending_closed_1m_ohlc", now=now, pending_order_id=row.id)
                row.revision += 1
                row.updated_at = now
                row.trigger_trace = {"candle_timestamp": iso_utc(candle.timestamp),
                    "market_mid": str(mid), "cfd_executable_price": str(fill_price),
                    "expected_entry_price": str(row.entry_level_usd),
                    "executed_entry_price": str(fill_price),
                    "spread_usd_per_btc": str(rates.spread_usd_per_btc),
                    "execution_model": rates.execution_model,
                    "gap_fill": assumption == "STOP_GAP_OPEN",
                    "fill_assumption": assumption, "processed_at": iso_utc(now)}
                if opened["status"] == "ACCEPTED":
                    row.status = "FILLED"
                    row.position_id = UUID(opened["position_id"])
                    row.filled_at = event_at
                    position = self.session.get(PaperPositionORM, row.position_id)
                    position.scenario_key = row.scenario_key
                    if row.strategy_parameters:
                        position.strategy_parameters = row.strategy_parameters
                    rr = risk_reward_for_rates(rates, side, position.entry_price,
                                               position.stop_loss, position.take_profit,
                                               position.quantity_btc)
                    row.trigger_trace = {**row.trigger_trace, "quantity_btc": str(position.quantity_btc),
                        "gross_loss_usd": str(abs(position.entry_price-position.stop_loss)*position.quantity_btc),
                        "gross_gain_usd": str(abs(position.take_profit-position.entry_price)*position.quantity_btc),
                        "net_risk_usd": str(rr["risk_net_usd"]),
                        "net_reward_usd": str(rr["reward_net_usd"]),
                        "rr_ratio": str(rr["rr_ratio"])}
                    self._event(row, "FILLED", now, {**row.trigger_trace,
                        "position_id": str(row.position_id), "quantity_btc": str(position.quantity_btc),
                        "entry_price": str(position.entry_price)}, row.created_by_decision_id)
                    result["filled"] += 1
                else:
                    row.status = "REJECTED"
                    row.cancel_reason = opened["reason"]
                    row.trigger_trace = {**row.trigger_trace, "rejected_reason": opened["reason"]}
                    self._event(row, "REJECTED_RISK_AT_TRIGGER", now,
                                {**row.trigger_trace, "reason": opened["reason"]}, row.created_by_decision_id)
                break
            if row.status == "PENDING" and now >= row.expires_at:
                row.revision += 1
                row.status = "EXPIRED"
                row.updated_at = now
                row.cancel_reason = "TTL elapsed"
                self._event(row, "EXPIRED", now, {"reason": "TTL elapsed"}, row.created_by_decision_id)
                result["expired"] += 1
        if result["filled"]:
            # A restart may replay several stored bars. Apply later protective
            # bars now; the entry bar itself remains ineligible.
            self.paper.process_new_candles(processed_at=now)
        self.session.flush()
        return result
