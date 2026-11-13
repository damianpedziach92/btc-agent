from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models.agent_decision import AgentDecisionORM
from database.models.candle import CandleORM
from database.models.paper import (
    PaperAccountLedgerORM, PaperAccountORM, PaperFeeProfileORM,
    PaperFundingRateORM, PaperPositionEventORM, PaperPositionORM, PaperPendingOrderORM,
)
from database.models.position_notification import PaperPositionNotificationORM
from models.agent_decision import AgentDecision, OpenProposal, PositionActionType, TriggerCondition
from models.market import Instrument, Timeframe
from models.strategy import SCALP_PARAMETERS_V1, Strategy, validate_strategy_setup
from models.paper import (
    ZERO, BTC_STEP, CFD_FIXED_20_USD, CFD_SPREAD_USD_PER_BTC, MIN_NET_RISK_REWARD, FeeRates, MarketType, PaperSide, RiskConfig, RiskEngine,
    barrier_touch, dec, entry_costs, estimated_target_net_pnl, execution_price, funding_cost, gross_pnl, money, rollover_cost,
    risk_reward_for_rates,
)
from repositories.candle_repository import CandleRepository
from services.market_structure import calculate_market_structure


ACCOUNT_NAME = "default"
STARTING_BALANCE = Decimal("1000.00")
MINUTE = timedelta(minutes=1)
ROLLOVER_PERIOD = timedelta(hours=4)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must be timezone-aware UTC")


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PaperTradingService:
    """Deterministic paper execution; the caller owns the database transaction."""

    def __init__(self, session: Session, risk: RiskConfig | None = None,
                 account_name: str = ACCOUNT_NAME, *, allow_legacy_test_entries: bool = False):
        self.session = session
        self.risk = risk or RiskConfig.from_env()
        self.account_name = account_name
        self._allow_legacy_test_entries = allow_legacy_test_entries

    def seed_account(self, now: datetime | None = None) -> PaperAccountORM:
        now = now or utc_now()
        require_utc(now)
        account_id = uuid4()
        self.session.execute(insert(PaperAccountORM).values(
            id=account_id, name=self.account_name, currency="USD",
            initial_balance=STARTING_BALANCE, cash_balance=STARTING_BALANCE,
            status="ACTIVE", version=1, created_at=now, updated_at=now,
        ).on_conflict_do_nothing(constraint="uq_paper_accounts_name"))
        account = self.session.scalar(select(PaperAccountORM).where(PaperAccountORM.name == self.account_name))
        self.session.execute(insert(PaperAccountLedgerORM).values(
            id=uuid4(), account_id=account.id, position_id=None,
            event_type="SEED", amount_usd=STARTING_BALANCE,
            margin_delta_usd=ZERO, event_time=now, event_key=f"paper:seed:{self.account_name}",
        ).on_conflict_do_nothing(constraint="uq_paper_ledger_event_key"))
        return account

    def _account(self, lock: bool = False) -> PaperAccountORM | None:
        statement = select(PaperAccountORM).where(PaperAccountORM.name == self.account_name)
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def configure_fee_profile(self, rates: FeeRates, now: datetime | None = None) -> PaperFeeProfileORM:
        now = now or utc_now()
        account = self._account(lock=True)
        if account is None:
            raise ValueError("Seed the paper account first")
        if rates.market_type is not MarketType.SPOT_MARGIN:
            raise ValueError("MVP account accepts SPOT_MARGIN fee profiles only")
        if rates.status not in {"ESTIMATED", "OBSERVED"}:
            raise ValueError("Explicit fee profile status required")
        if not rates.source.strip():
            raise ValueError("Fee profile needs a source label")
        if any(value < ZERO or value >= Decimal("1") for value in (
            rates.trade_entry_fee_rate, rates.trade_exit_fee_rate,
            rates.margin_opening_fee_rate, rates.rollover_rate_per_4h,
        )) or rates.slippage_bps < ZERO or rates.spread_bps < ZERO or rates.spread_usd_per_btc < ZERO:
            raise ValueError("Invalid fee rates")
        if rates.execution_model not in {"LEGACY_BPS", CFD_FIXED_20_USD}:
            raise ValueError("Unknown PAPER execution model")
        if rates.execution_model == CFD_FIXED_20_USD and (
            rates.status != "ESTIMATED" or rates.source != "user_requested_cfd_fixed_20_usd_spread" or
            rates.spread_usd_per_btc != CFD_SPREAD_USD_PER_BTC or
            any(getattr(rates, field) != ZERO for field in (
                "trade_entry_fee_rate", "trade_exit_fee_rate", "margin_opening_fee_rate",
                "rollover_rate_per_4h", "slippage_bps", "spread_bps"))
        ):
            raise ValueError("CFD20 requires exactly 20 USD/BTC spread and zero additional charges")
        previous = self._fee_profile(account.id)
        if previous:
            previous_rates = self._rates(previous)
            if previous_rates.execution_model == CFD_FIXED_20_USD and rates.execution_model != CFD_FIXED_20_USD:
                raise ValueError("Legacy profiles cannot be reactivated after CFD20")
            fields = (
                "market_type", "status", "source", "trade_entry_fee_rate",
                "trade_exit_fee_rate", "margin_opening_fee_rate",
                "rollover_rate_per_4h", "slippage_bps", "spread_bps",
                "execution_model", "spread_usd_per_btc",
            )
            if all(getattr(previous_rates, field) == getattr(rates, field) for field in fields):
                return previous
        version = previous.version + 1 if previous else 1
        profile = PaperFeeProfileORM(
            id=uuid4(), account_id=account.id, version=version,
            market_type=rates.market_type.value, status=rates.status,
            source=rates.source, trade_entry_fee_rate=rates.trade_entry_fee_rate,
            trade_exit_fee_rate=rates.trade_exit_fee_rate,
            margin_opening_fee_rate=rates.margin_opening_fee_rate,
            rollover_rate_per_4h=rates.rollover_rate_per_4h,
            slippage_bps=rates.slippage_bps, spread_bps=rates.spread_bps,
            execution_model=rates.execution_model, spread_usd_per_btc=rates.spread_usd_per_btc,
            created_at=now,
        )
        self.session.add(profile)
        self.session.flush()
        return profile

    def _fee_profile(self, account_id: UUID) -> PaperFeeProfileORM | None:
        return self.session.scalar(select(PaperFeeProfileORM).where(
            PaperFeeProfileORM.account_id == account_id,
        ).order_by(PaperFeeProfileORM.version.desc()).limit(1))

    @staticmethod
    def _rates(profile: PaperFeeProfileORM) -> FeeRates:
        return FeeRates(
            market_type=MarketType(profile.market_type), status=profile.status,
            source=profile.source, version=profile.version,
            trade_entry_fee_rate=profile.trade_entry_fee_rate,
            trade_exit_fee_rate=profile.trade_exit_fee_rate,
            margin_opening_fee_rate=profile.margin_opening_fee_rate,
            rollover_rate_per_4h=profile.rollover_rate_per_4h,
            slippage_bps=profile.slippage_bps, spread_bps=profile.spread_bps,
            execution_model=profile.execution_model, spread_usd_per_btc=profile.spread_usd_per_btc,
        )

    def _positions(self, account_id: UUID, lock: bool = False) -> list[PaperPositionORM]:
        statement = select(PaperPositionORM).where(
            PaperPositionORM.account_id == account_id,
            PaperPositionORM.status == "OPEN",
        ).order_by(PaperPositionORM.entry_at)
        if lock:
            statement = statement.with_for_update()
        return list(self.session.scalars(statement))

    def _event(self, account_id: UUID, type_: str, key: str, payload: dict,
               now: datetime, position_id: UUID | None = None,
               decision_id: UUID | None = None) -> None:
        event_id = uuid4()
        inserted = self.session.execute(insert(PaperPositionEventORM).values(
            id=event_id, account_id=account_id, position_id=position_id,
            decision_id=decision_id, type=type_, event_time=now,
            payload=payload, dedupe_key=key,
        ).on_conflict_do_nothing(constraint="uq_paper_position_event_dedupe")
          .returning(PaperPositionEventORM.id)).scalar()
        if inserted is not None and type_ in {"OPENED", "CLOSED"}:
            self.session.add(PaperPositionNotificationORM(
                event_id=inserted, status="PENDING", created_at=utc_now(),
                sent_at=None, attempts=0, last_error=None,
            ))

    def _ledger(self, account: PaperAccountORM, type_: str, key: str,
                cash_delta: Decimal, margin_delta: Decimal, now: datetime,
                position_id: UUID | None = None) -> None:
        cash_delta = money(cash_delta)
        margin_delta = money(margin_delta)
        inserted = self.session.execute(insert(PaperAccountLedgerORM).values(
            id=uuid4(), account_id=account.id, position_id=position_id,
            event_type=type_, amount_usd=cash_delta,
            margin_delta_usd=margin_delta, event_time=now, event_key=key,
        ).on_conflict_do_nothing(constraint="uq_paper_ledger_event_key").returning(PaperAccountLedgerORM.id)).scalar()
        if inserted is not None:
            account.cash_balance = money(account.cash_balance + cash_delta)
            account.updated_at = now
            account.version += 1
            if account.cash_balance < ZERO:
                raise ValueError("Paper cash would become negative")

    def _latest_mark(self) -> tuple[Decimal | None, datetime | None]:
        candle = self.session.scalar(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == Timeframe.ONE_MINUTE,
        ).order_by(CandleORM.timestamp.desc()).limit(1))
        return (candle.close, candle.timestamp + MINUTE) if candle else (None, None)

    def _feed_is_contiguous(self, now: datetime) -> bool:
        rows = self.session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == Timeframe.ONE_MINUTE,
        ).order_by(CandleORM.timestamp.desc()).limit(self.risk.min_consecutive_1m)).all()
        return (
            len(rows) == self.risk.min_consecutive_1m and
            timedelta(0) <= now - (rows[0].timestamp + MINUTE) <= timedelta(seconds=self.risk.max_candle_age_seconds) and
            all(rows[index].timestamp - rows[index + 1].timestamp == MINUTE
                for index in range(len(rows) - 1))
        )

    def mark_to_market(self, latest_price: Decimal) -> dict:
        """Compute USD account values without posting to the ledger."""
        latest_price = dec(latest_price)
        if latest_price <= ZERO:
            raise ValueError("Mark price must be positive")
        account = self._account()
        if account is None:
            raise ValueError("Paper account does not exist")
        positions = self._positions(account.id)
        reserved = sum((p.reserved_margin for p in positions), ZERO)
        gross = sum((gross_pnl(PaperSide(p.side), p.entry_price, latest_price, p.quantity_btc)
                     for p in positions), ZERO)
        estimated_closes = [(
            p, execution_price(latest_price, PaperSide(p.side), False, rate), rate,
        ) for p in positions for rate in [FeeRates.from_dict(p.fee_snapshot)]]
        liquidation_gross = sum((gross_pnl(PaperSide(p.side), p.entry_price, exit_price, p.quantity_btc)
                                 for p, exit_price, _ in estimated_closes), ZERO)
        expected_exit = sum((p.quantity_btc * exit_price * rate.trade_exit_fee_rate
                             for p, exit_price, rate in estimated_closes), ZERO)
        paid_open_costs = sum((p.initial_costs + p.accrued_holding_costs for p in positions), ZERO)
        equity = account.cash_balance + liquidation_gross - expected_exit
        return {
            "reserved_margin": reserved,
            "available_margin": max(ZERO, min(account.cash_balance - reserved, equity - reserved)),
            "unrealized_gross_pnl": gross,
            "unrealized_net_estimate": liquidation_gross - paid_open_costs - expected_exit,
            "equity_estimate": equity,
            "open_exposure_notional": sum((p.quantity_btc * latest_price for p in positions), ZERO),
        }

    def account_snapshot(self, now: datetime | None = None) -> dict:
        now = now or utc_now()
        account = self._account()
        if account is None:
            return {"status": "UNSEEDED", "currency": "USD", "fee_status": "UNCONFIGURED",
                    "pnl_quality": "UNCONFIGURED", "fee_rates": None,
                    "risk_limits": self.risk.as_dict()}
        mark, mark_at = self._latest_mark()
        positions = self._positions(account.id)
        values = self.mark_to_market(mark) if mark is not None else {
            "reserved_margin": sum((p.reserved_margin for p in positions), ZERO),
            "available_margin": ZERO,
            "unrealized_gross_pnl": ZERO,
            "unrealized_net_estimate": ZERO,
            "equity_estimate": account.cash_balance,
            "open_exposure_notional": sum((p.entry_notional for p in positions), ZERO),
        }
        profile = self._fee_profile(account.id)
        profile_rates = self._rates(profile) if profile else None
        fees = self.session.scalar(select(func.coalesce(func.sum(PaperPositionORM.fee_total), ZERO)).where(
            PaperPositionORM.account_id == account.id)) or ZERO
        realized = self.session.scalar(select(func.coalesce(func.sum(PaperPositionORM.realized_net_pnl), ZERO)).where(
            PaperPositionORM.account_id == account.id,
            PaperPositionORM.status != "OPEN")) or ZERO
        return {
            "account_id": str(account.id), "status": account.status,
            "currency": account.currency, "initial_balance": str(account.initial_balance),
            "cash_balance": str(account.cash_balance), "reserved_margin": str(values["reserved_margin"]),
            "available_margin": str(values["available_margin"]),
            "unrealized_gross_pnl": str(values["unrealized_gross_pnl"]),
            "unrealized_net_estimate": str(values["unrealized_net_estimate"]),
            "realized_pnl": str(realized), "fees_paid": str(fees),
            "equity_estimate": str(values["equity_estimate"]),
            "open_exposure_notional": str(values["open_exposure_notional"]),
            "open_position_count": len(positions), "mark_price": str(mark) if mark is not None else None,
            "mark_at": iso_utc(mark_at) if mark_at else None,
            "mark_source": "closed_1m_kraken_spot" if mark else None,
            "stale_data": mark_at is None or now - mark_at > timedelta(seconds=self.risk.max_candle_age_seconds),
            "fee_status": profile.status if profile else "UNCONFIGURED",
            "pnl_quality": "ESTIMATED_PAPER_ONLY" if profile else "UNCONFIGURED",
            "fee_profile_version": profile.version if profile else None,
            "market_type": profile.market_type if profile else None,
            "execution_model": profile_rates.execution_model if profile_rates else None,
            "cfd_bid": str(mark - profile_rates.spread_usd_per_btc / 2) if mark is not None and profile_rates and profile_rates.execution_model == CFD_FIXED_20_USD else None,
            "cfd_ask": str(mark + profile_rates.spread_usd_per_btc / 2) if mark is not None and profile_rates and profile_rates.execution_model == CFD_FIXED_20_USD else None,
            "fee_rates": profile_rates.as_dict() if profile_rates else None,
            "risk_limits": self.risk.as_dict(),
        }

    def open_positions_snapshot(self, now: datetime | None = None) -> list[dict]:
        now = now or utc_now()
        account = self._account()
        if account is None:
            return []
        mark, mark_at = self._latest_mark()
        result = []
        for p in self._positions(account.id):
            rate = FeeRates.from_dict(p.fee_snapshot)
            gross = gross_pnl(PaperSide(p.side), p.entry_price, mark, p.quantity_btc) if mark else ZERO
            estimated_exit_price = execution_price(mark, PaperSide(p.side), False, rate) if mark else None
            estimated_liquidation_gross = gross_pnl(
                PaperSide(p.side), p.entry_price, estimated_exit_price, p.quantity_btc
            ) if estimated_exit_price is not None else ZERO
            exit_fee = p.quantity_btc * estimated_exit_price * rate.trade_exit_fee_rate if estimated_exit_price is not None else ZERO
            result.append({
                "id": str(p.id), "side": p.side, "instrument": p.instrument,
                "quantity_btc": str(p.quantity_btc), "allocated_margin": str(p.reserved_margin),
                "notional_usd": str(p.entry_notional), "entry_price": str(p.entry_price),
                "entry_at": iso_utc(p.entry_at), "entry_reason": p.entry_reason,
                "agent_decision_id": str(p.agent_decision_id) if p.agent_decision_id else None,
                "scenario_key": p.scenario_key, "strategy": p.strategy or "LEGACY_M15",
                "setup_type": p.setup_type, "strategy_parameters": p.strategy_parameters,
                "stop_loss": str(p.stop_loss) if p.stop_loss else None,
                "take_profit": str(p.take_profit) if p.take_profit else None,
                "current_mark_price": str(mark) if mark else None,
                "mark_at": iso_utc(mark_at) if mark_at else None,
                "mark_source": "closed_1m_kraken_spot" if mark else None,
                "stale_data": mark_at is None or now - mark_at > timedelta(seconds=self.risk.max_candle_age_seconds),
                "unrealized_gross_pnl": str(gross) if mark else None,
                "unrealized_net_estimate": str(estimated_liquidation_gross - p.initial_costs - p.accrued_holding_costs - exit_fee) if mark else None,
                "initial_costs": str(p.initial_costs), "accrued_holding_costs": str(p.accrued_holding_costs),
                "next_holding_fee_at": (iso_utc(p.entry_at + ROLLOVER_PERIOD * (p.last_holding_period + 1))
                                        if rate.rollover_rate_per_4h > ZERO else None),
                "fee_status": rate.status, "fee_source": rate.source,
                "pnl_quality": "ESTIMATED_PAPER_ONLY",
                "fee_profile_version": rate.version, "market_type": p.market_type,
                "execution_model": rate.execution_model,
                "spread_usd_per_btc": str(rate.spread_usd_per_btc),
                "roundtrip_spread_cost_usd": str(p.quantity_btc * rate.spread_usd_per_btc),
                "estimated_executable_exit_price": str(estimated_exit_price) if estimated_exit_price is not None else None,
            })
        return result

    def recent_events(self, limit: int = 5) -> list[dict]:
        account = self._account()
        if account is None:
            return []
        corrected_positions = set(self.session.scalars(select(PaperPositionEventORM.position_id).where(
            PaperPositionEventORM.account_id == account.id,
            PaperPositionEventORM.type == "CORRECTED",
        )))
        rows = self.session.scalars(select(PaperPositionEventORM).where(
            PaperPositionEventORM.account_id == account.id,
            PaperPositionEventORM.type.in_(["REJECTED", "CLOSED", "CORRECTED"]),
        ).order_by(PaperPositionEventORM.event_time.desc()).limit(limit * 2 + 10)).all()
        rows = [row for row in rows if not (
            row.type == "CLOSED" and row.position_id in corrected_positions
        )][:limit]
        return [{"type": row.type, "time": iso_utc(row.event_time),
                 "position_id": str(row.position_id) if row.position_id else None,
                 "decision_id": str(row.decision_id) if row.decision_id else None,
                 "payload": row.payload}
                for row in rows]

    def recent_closed_positions(self, limit: int = 10) -> list[dict]:
        account = self._account()
        if account is None:
            return []
        rows = self.session.scalars(select(PaperPositionORM).where(
            PaperPositionORM.account_id == account.id,
            PaperPositionORM.status != "OPEN",
        ).order_by(PaperPositionORM.close_at.desc()).limit(limit)).all()
        return [{
            "id": str(row.id), "side": row.side, "status": row.status,
            "entry_price": str(row.entry_price), "entry_at": iso_utc(row.entry_at),
            "close_price": str(row.close_price), "close_at": iso_utc(row.close_at),
            "close_reason": row.close_reason, "close_reason_text": row.close_reason_text,
            "realized_gross_pnl": str(row.realized_gross_pnl),
            "realized_net_estimate": str(row.realized_net_pnl),
            "fees_paid": str(row.fee_total), "unpaid_loss": str(row.unpaid_loss),
            "fee_status": row.fee_snapshot["status"],
            "execution_model": FeeRates.from_dict(row.fee_snapshot).execution_model,
            "quantity_btc": str(row.quantity_btc),
            "spread_usd_per_btc": str(FeeRates.from_dict(row.fee_snapshot).spread_usd_per_btc),
        } for row in rows]

    def _reject(self, account: PaperAccountORM, key: str, reason: str,
                now: datetime, decision_id: UUID | None = None,
                diagnostics: dict | None = None) -> dict:
        self._event(account.id, "REJECTED", f"reject:{key}",
                    {"reason": reason, "execution_key": key, **(diagnostics or {})}, now,
                    decision_id=decision_id)
        return {"status": "REJECTED", "reason": reason, **(diagnostics or {})}

    def propose_open(
        self, proposal: OpenProposal, mark_price: Decimal, price_at: datetime,
        *, execution_key: str, decision_id: UUID | None = None,
        source: str = "kraken_spot_ticker", now: datetime | None = None,
        bid_price: Decimal | None = None, ask_price: Decimal | None = None,
    ) -> dict:
        return self.open_position(proposal, mark_price, price_at,
                                  execution_key=execution_key, decision_id=decision_id,
                                  source=source, now=now,
                                  bid_price=bid_price, ask_price=ask_price)

    def validate_stop_structure(self, side: PaperSide, entry: Decimal,
                                stop_loss: Decimal | None, price_at: datetime,
                                strategy: Strategy = Strategy.INTRADAY_M15,
                                invalidation_level: Decimal | None = None) -> str | None:
        if strategy is Strategy.INTRADAY_M15 and self.risk.min_stop_m15_atr_multiple <= ZERO:
            return None
        timeframe = Timeframe.FIVE_MINUTES if strategy is Strategy.SCALP_M5 else Timeframe.FIFTEEN_MINUTES
        minutes = timeframe.minutes
        swing_candles = SCALP_PARAMETERS_V1["swing_candles"] if strategy is Strategy.SCALP_M5 else self.risk.stop_swing_m15_candles
        rows = self.session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == timeframe,
            CandleORM.timestamp + timedelta(minutes=minutes) <= price_at,
        ).order_by(CandleORM.timestamp.desc()).limit(100)).all()
        structure = calculate_market_structure(
            [CandleRepository._to_candle(row) for row in rows],
            swing_candles=swing_candles,
        )
        atr = dec(structure["atr14"]) if structure["atr14"] is not None else None
        latest_close = (datetime.fromisoformat(structure["timestamp"]) + timedelta(minutes=minutes)
                            if structure["timestamp"] else None)
        if (atr is None or stop_loss is None or structure["swing_low"] is None or
            latest_close is None or price_at - latest_close > timedelta(minutes=minutes * 2)):
            return ("Fresh M15 ATR/swing data and stop loss required by strategy"
                    if strategy is Strategy.INTRADAY_M15 else
                    "ATR_DATA_GAP: fresh M5 ATR14/swing and stop loss required")
        buffer = atr * (Decimal(SCALP_PARAMETERS_V1["stop_buffer_atr"])
                        if strategy is Strategy.SCALP_M5 else self.risk.stop_swing_atr_buffer)
        minimum_distance = atr * (Decimal(SCALP_PARAMETERS_V1["minimum_stop_atr"])
                                  if strategy is Strategy.SCALP_M5 else self.risk.min_stop_m15_atr_multiple)
        if side is PaperSide.LONG:
            highest_allowed_stop = min(entry - minimum_distance, dec(structure["swing_low"]) - buffer)
            if stop_loss > highest_allowed_stop:
                return (f"SL too close for M15 structure; LONG SL must be <= {highest_allowed_stop:.2f}"
                        if strategy is Strategy.INTRADAY_M15 else
                        f"INVALID_SL_STRUCTURE: M5 LONG SL must be <= {highest_allowed_stop:.2f}")
            if invalidation_level is not None and stop_loss > invalidation_level:
                return "INVALID_SL_STRUCTURE: LONG SL must protect invalidation"
        else:
            lowest_allowed_stop = max(entry + minimum_distance, dec(structure["swing_high"]) + buffer)
            if stop_loss < lowest_allowed_stop:
                return (f"SL too close for M15 structure; SHORT SL must be >= {lowest_allowed_stop:.2f}"
                        if strategy is Strategy.INTRADAY_M15 else
                        f"INVALID_SL_STRUCTURE: M5 SHORT SL must be >= {lowest_allowed_stop:.2f}")
            if invalidation_level is not None and stop_loss < invalidation_level:
                return "INVALID_SL_STRUCTURE: SHORT SL must protect invalidation"
        return None

    def validate_entry_trigger(self, proposal: OpenProposal, side: PaperSide,
                               entry: Decimal, decision: AgentDecisionORM, rates: FeeRates) -> str | None:
        required = ("setup_type", "market_regime", "signal_timeframe", "trigger_candle_timestamp",
                    "trigger_level", "trigger_condition", "invalidation_level",
                    "expected_rr_after_costs", "signal_age_seconds", "countertrend")
        if any(getattr(proposal, key) is None for key in required) or proposal.stop_loss is None or proposal.take_profit is None:
            return "Structured setup, closed trigger candle, SL and TP required for new CFD20 entries"
        step = timedelta(minutes=proposal.signal_timeframe.minutes)
        candle = self.session.scalar(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == proposal.signal_timeframe,
            CandleORM.timestamp == proposal.trigger_candle_timestamp,
        ))
        closed_at = proposal.trigger_candle_timestamp + step
        if candle is None or closed_at > decision.created_at:
            return "Trigger candle is missing or was not closed before analysis"
        age = int((decision.created_at - closed_at).total_seconds())
        if age < 0 or age > max(int(step.total_seconds()*2), 900):
            return "Trigger signal is stale"
        snapshot_at = ((decision.analysis_trace or {}).get("market_context") or {}).get("as_of")
        observed_at = datetime.fromisoformat(snapshot_at) if snapshot_at else decision.created_at
        if abs(int((observed_at - closed_at).total_seconds()) - proposal.signal_age_seconds) > 60:
            return "Declared trigger age disagrees with stored candle"
        if proposal.trigger_condition is TriggerCondition.CLOSE_ABOVE and candle.close <= proposal.trigger_level:
            return "Trigger candle did not close above declared level"
        if proposal.trigger_condition is TriggerCondition.CLOSE_BELOW and candle.close >= proposal.trigger_level:
            return "Trigger candle did not close below declared level"
        if side is PaperSide.LONG and proposal.stop_loss > proposal.invalidation_level:
            return "LONG SL must protect the declared invalidation level"
        if side is PaperSide.SHORT and proposal.stop_loss < proposal.invalidation_level:
            return "SHORT SL must protect the declared invalidation level"
        rr = risk_reward_for_rates(rates, side, entry, proposal.stop_loss, proposal.take_profit, Decimal("1"))["rr_ratio"]
        if rr is None or rr < MIN_NET_RISK_REWARD:
            return "INSUFFICIENT_RISK_REWARD"
        if proposal.expected_rr_after_costs > rr:
            return f"Claimed RR {proposal.expected_rr_after_costs} exceeds executable RR {rr:.3f}"
        return None

    def validate_chase(self, side: PaperSide, entry: Decimal, price_at: datetime,
                       strategy: Strategy = Strategy.INTRADAY_M15) -> str | None:
        multiple = self.risk.experimental_max_chase_atr
        if multiple <= ZERO:
            return None
        timeframe = Timeframe.FIVE_MINUTES if strategy is Strategy.SCALP_M5 else Timeframe.FIFTEEN_MINUTES
        rows = self.session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == timeframe,
            CandleORM.timestamp + timedelta(minutes=timeframe.minutes) <= price_at,
        ).order_by(CandleORM.timestamp.desc()).limit(100)).all()
        structure = calculate_market_structure([CandleRepository._to_candle(row) for row in rows])
        atr = dec(structure["atr14"]) if structure["atr14"] else None
        if not rows or atr is None:
            return f"Experimental chase filter needs closed {timeframe.value} ATR"
        reference = rows[0].close
        distance = entry-reference if side is PaperSide.LONG else reference-entry
        if distance > atr*multiple:
            return f"Experimental extended move: entry {distance:.2f} USD beyond {timeframe.value} close, limit {atr*multiple:.2f}"
        return None

    def scalp_risk_error(self, account, equity: Decimal, at: datetime) -> str | None:
        """A separate PAPER-only circuit breaker for new scalp entries."""
        day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        closed = list(self.session.scalars(select(PaperPositionORM).where(
            PaperPositionORM.account_id == account.id,
            PaperPositionORM.strategy == Strategy.SCALP_M5.value,
            PaperPositionORM.close_at >= day_start,
            PaperPositionORM.close_at <= at,
        ).order_by(PaperPositionORM.close_at.desc())))
        realized = sum((row.realized_net_pnl or ZERO for row in closed), ZERO)
        if realized <= -equity * self.risk.scalp_daily_loss_stop:
            return "SCALP_DAILY_LOSS_STOP"
        streak = 0
        for row in closed:
            if row.realized_net_pnl is None or row.realized_net_pnl >= ZERO:
                break
            streak += 1
        if streak >= self.risk.scalp_max_consecutive_losses:
            return "SCALP_LOSS_STREAK_STOP"
        if closed and closed[0].realized_net_pnl is not None and closed[0].realized_net_pnl < ZERO:
            if at - closed[0].close_at < timedelta(minutes=self.risk.scalp_loss_cooldown_minutes):
                return "SCALP_LOSS_COOLDOWN"
        openings = self.session.scalar(select(func.count()).select_from(PaperPositionORM).where(
            PaperPositionORM.account_id == account.id,
            PaperPositionORM.strategy == Strategy.SCALP_M5.value,
            PaperPositionORM.entry_at > at-timedelta(hours=1),
            PaperPositionORM.entry_at <= at,
        )) or 0
        if openings >= self.risk.scalp_max_openings_per_hour:
            return "SCALP_HOURLY_OPENING_LIMIT"
        return None

    def open_position(
        self, proposal: OpenProposal, mark_price: Decimal, price_at: datetime,
        *, execution_key: str, decision_id: UUID | None = None,
        source: str = "kraken_spot_ticker", now: datetime | None = None,
        bid_price: Decimal | None = None, ask_price: Decimal | None = None,
        pending_order_id: UUID | None = None,
    ) -> dict:
        now = now or utc_now()
        require_utc(now)
        require_utc(price_at)
        mark_price = dec(mark_price)
        account = self._account(lock=True)
        if account is None:
            raise ValueError("Seed the paper account first")
        prior = self.session.scalar(select(PaperPositionORM).where(
            PaperPositionORM.execution_key == execution_key))
        if prior:
            return {"status": "ACCEPTED", "position_id": str(prior.id), "reason": "Already executed"}
        rejected = self.session.scalar(select(PaperPositionEventORM).where(
            PaperPositionEventORM.dedupe_key == f"reject:{execution_key}"))
        if rejected:
            return {"status": "REJECTED", **rejected.payload}
        if account.status != "ACTIVE":
            return self._reject(account, execution_key, "Paper account is not ACTIVE", now, decision_id)
        if mark_price <= ZERO:
            return self._reject(account, execution_key, "Price must be positive", now, decision_id)
        pending_fill = pending_order_id is not None
        if source == "pending_closed_1m_ohlc" and not pending_fill:
            return self._reject(account, execution_key, "Pending fill requires a saved order", now, decision_id)
        if pending_fill:
            order = self.session.get(PaperPendingOrderORM, pending_order_id)
            if (order is None or order.account_id != account.id or order.status != "PENDING" or
                execution_key != f"pending-fill:{pending_order_id}"):
                return self._reject(account, execution_key, "Pending order is not active or mismatched", now, decision_id)
            if price_at <= order.updated_at or price_at > order.expires_at:
                return self._reject(account, execution_key, "Pending fill outside active interval", now, decision_id)
            if proposal.strategy != Strategy(order.strategy or Strategy.INTRADAY_M15.value) or proposal.setup_type != order.setup_type:
                return self._reject(account, execution_key, "Pending strategy/setup mismatch", now, decision_id)
        strategy = proposal.strategy or Strategy.INTRADAY_M15
        if (decision_id is not None or pending_fill) and proposal.strategy is None and not self._allow_legacy_test_entries:
            return self._reject(account, execution_key, "STRATEGY_REQUIRED", now, decision_id)
        setup_error = (validate_strategy_setup(strategy, proposal.setup_type)
                       if (proposal.strategy is not None or decision_id is not None or pending_fill) else None)
        if setup_error:
            return self._reject(account, execution_key, setup_error, now, decision_id)
        if (not pending_fill and price_at < now - timedelta(seconds=30)) or price_at > now + timedelta(seconds=5):
            return self._reject(account, execution_key, "Ticker price is stale or future-dated", now, decision_id)
        if price_at > proposal.valid_until or (now > proposal.valid_until and not pending_fill):
            return self._reject(account, execution_key, "Open proposal expired", now, decision_id)
        if decision_id:
            decision = self.session.get(AgentDecisionORM, decision_id)
            if decision is None or price_at <= decision.created_at:
                return self._reject(account, execution_key, "Price predates the completed analysis", now, decision_id)
        latest_mark, latest_mark_at = self._latest_mark()
        if not self._feed_is_contiguous(now):
            return self._reject(account, execution_key, "Closed 1m market data is stale or gapped", now, decision_id)
        profile = self._fee_profile(account.id)
        if profile is None or profile.status == "UNCONFIGURED":
            return self._reject(account, execution_key, "Fee profile is UNCONFIGURED", now, decision_id)
        rates = self._rates(profile)
        if pending_fill and order.fee_profile_id != profile.id:
            return self._reject(account, execution_key, "Pending fee profile changed; recreate order", now, decision_id)
        if rates.execution_model != CFD_FIXED_20_USD and not self._allow_legacy_test_entries:
            return self._reject(account, execution_key, "CFD20 profile must be activated for new entries", now, decision_id)
        if rates.market_type is not MarketType.SPOT_MARGIN:
            return self._reject(account, execution_key, "PERPETUAL requires explicit funding history", now, decision_id)
        positions = self._positions(account.id, lock=True)
        side = PaperSide(proposal.side.value)
        if any(p.side != side.value for p in positions):
            return self._reject(account, execution_key, "Opposing BTC/USD positions are disabled", now, decision_id)
        quote_mid = (dec(bid_price) + dec(ask_price)) / 2 if bid_price is not None and ask_price is not None else mark_price
        entry = money(execution_price(mark_price, side, True, rates,
                                      bid=bid_price, ask=ask_price))
        if not self._allow_legacy_test_entries or rates.execution_model == CFD_FIXED_20_USD:
            if proposal.stop_loss is None or proposal.take_profit is None:
                return self._reject(account, execution_key, "INSUFFICIENT_RISK_REWARD", now, decision_id,
                                    {"code": "INSUFFICIENT_RISK_REWARD", "source": source,
                                     "entry": str(entry), "SL": str(proposal.stop_loss),
                                     "TP": str(proposal.take_profit), "min_rr": str(MIN_NET_RISK_REWARD),
                                     "fee_profile_version": rates.version})
        if decision_id is not None and rates.execution_model == CFD_FIXED_20_USD:
            trigger_error = self.validate_entry_trigger(proposal, side, entry, decision, rates)
            if trigger_error:
                return self._reject(account, execution_key, trigger_error, now, decision_id)
        structure_at = price_at - MINUTE if pending_fill else price_at
        stop_error = self.validate_stop_structure(side, entry, proposal.stop_loss, structure_at,
                                                  strategy, proposal.invalidation_level)
        if stop_error:
            return self._reject(account, execution_key, stop_error, now, decision_id)
        chase_error = self.validate_chase(side, entry, structure_at, strategy)
        if chase_error:
            return self._reject(account, execution_key, chase_error, now, decision_id)
        reserved = sum((p.reserved_margin for p in positions), ZERO)
        equity = account.cash_balance + sum((
            gross_pnl(PaperSide(p.side), p.entry_price,
                      execution_price(quote_mid, PaperSide(p.side), False, FeeRates.from_dict(p.fee_snapshot)),
                      p.quantity_btc) - p.quantity_btc *
            execution_price(quote_mid, PaperSide(p.side), False, FeeRates.from_dict(p.fee_snapshot)) *
            FeeRates.from_dict(p.fee_snapshot).trade_exit_fee_rate
            for p in positions), ZERO)
        current_risk = sum((
            p.quantity_btc * abs(p.entry_price - p.stop_loss) +
            p.entry_notional * (
                past_rates.trade_entry_fee_rate + past_rates.trade_exit_fee_rate +
                past_rates.margin_opening_fee_rate + past_rates.rollover_rate_per_4h
            )
            for p in positions if p.stop_loss is not None
            for past_rates in [FeeRates.from_dict(p.fee_snapshot)]
        ), ZERO)
        if strategy is Strategy.SCALP_M5:
            scalp_error = self.scalp_risk_error(account, equity, price_at)
            if scalp_error:
                return self._reject(account, execution_key, scalp_error, now, decision_id)
        quantity, reason = RiskEngine(self.risk).size(
            side=side, entry=entry, stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit, equity=equity,
            cash=account.cash_balance, reserved=reserved,
            current_risk=current_risk, position_count=len(positions), rates=rates,
            risk_fraction_override=(self.risk.scalp_max_risk_per_position
                                    if strategy is Strategy.SCALP_M5 else None),
        )
        if quantity is None:
            return self._reject(account, execution_key, reason, now, decision_id)
        if not self._allow_legacy_test_entries or rates.execution_model == CFD_FIXED_20_USD:
            rr = risk_reward_for_rates(rates, side, entry, proposal.stop_loss, proposal.take_profit, quantity)
            if rr["rr_ratio"] is None or rr["rr_ratio"] < MIN_NET_RISK_REWARD:
                diagnostics = {"code": "INSUFFICIENT_RISK_REWARD", "source": source,
                    "entry": str(entry), "SL": str(proposal.stop_loss), "TP": str(proposal.take_profit),
                    "quantity_btc": str(quantity), "risk_net_usd": str(rr["risk_net_usd"]),
                    "reward_net_usd": str(rr["reward_net_usd"]),
                    "rr_ratio": str(rr["rr_ratio"]) if rr["rr_ratio"] is not None else None,
                    "min_rr": str(MIN_NET_RISK_REWARD), "fee_profile_version": rates.version}
                return self._reject(account, execution_key, "INSUFFICIENT_RISK_REWARD", now, decision_id, diagnostics)
        immediate_exit = execution_price(quote_mid, side, False, rates)
        single_limit = self.risk.no_sl_max_margin if proposal.stop_loss is None else self.risk.max_position_margin
        for _ in range(100):
            if quantity <= ZERO:
                break
            margin = money(entry * quantity)  # max leverage is exactly 1x
            initial_cost, cost_basis = entry_costs(rates, side, quantity, entry)
            initial_cost = money(initial_cost)
            post_equity = equity - initial_cost + gross_pnl(side, entry, immediate_exit, quantity) - (
                quantity * immediate_exit * rates.trade_exit_fee_rate)
            if (margin <= post_equity * single_limit and
                reserved + margin <= post_equity * self.risk.max_total_margin and
                account.cash_balance - reserved - margin - initial_cost >= post_equity * self.risk.min_free_margin):
                break
            quantity -= BTC_STEP
        if (quantity <= ZERO or margin < self.risk.min_notional or
            margin > post_equity * single_limit or
            reserved + margin > post_equity * self.risk.max_total_margin or
            account.cash_balance - reserved - margin - initial_cost < post_equity * self.risk.min_free_margin):
            return self._reject(account, execution_key,
                                f"Post-cost margin limit: required_margin={margin}, available_margin={account.cash_balance-reserved}, "
                                f"estimated_close_cost={equity-post_equity}, max_quantity={quantity}, "
                                f"min_notional={self.risk.min_notional}, risk_budget={equity*self.risk.max_risk_per_position}", now, decision_id)
        position_id = uuid4()
        position = PaperPositionORM(
            id=position_id, account_id=account.id, instrument=Instrument.BTC_USD.value,
            market_type=rates.market_type.value, side=side.value, status="OPEN",
            entry_price=entry, entry_at=price_at,
            entry_candle_timestamp=price_at - MINUTE if pending_fill else latest_mark_at - MINUTE,
            entry_source=(source if pending_fill else "kraken_spot_mid_fixed_spread_paper" if bid_price is not None else
                          "last_trade_fixed_spread_paper"),
            execution_key=execution_key, quantity_btc=quantity,
            entry_notional=margin, leverage=Decimal("1"), reserved_margin=margin,
            stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
            entry_reason=proposal.reason, agent_decision_id=decision_id,
            scenario_key=None, fee_profile_id=profile.id,
            strategy=strategy.value, setup_type=proposal.setup_type,
            strategy_parameters=(SCALP_PARAMETERS_V1.copy() if strategy is Strategy.SCALP_M5 else
                                 {"version": "INTRADAY_M15_V1", "swing_candles": self.risk.stop_swing_m15_candles,
                                  "stop_buffer_atr": str(self.risk.stop_swing_atr_buffer),
                                  "minimum_stop_atr": str(self.risk.min_stop_m15_atr_multiple),
                                  "max_hold_hours": self.risk.max_hold_hours}),
            fee_snapshot=rates.as_dict(), initial_costs=initial_cost,
            accrued_holding_costs=ZERO, close_price=None, close_at=None,
            close_source=None, close_reason=None, close_reason_text=None,
            realized_gross_pnl=None, realized_net_pnl=None, fee_total=initial_cost,
            unpaid_loss=ZERO, last_evaluated_candle_timestamp=None,
            last_holding_period=0,
        )
        self.session.add(position)
        self.session.flush()
        self._ledger(account, "MARGIN_RESERVED", f"reserve:{position_id}", ZERO, margin, now, position_id)
        self._ledger(account, "ENTRY_COST", f"entry-cost:{position_id}", -initial_cost, ZERO, now, position_id)
        self._event(account.id, "OPENED", f"open:{position_id}", {
            "side": side.value, "entry_price": str(entry), "mark_price": str(mark_price),
            "price_source": source, "quote_mode": ("pending_candle_mid" if pending_fill else
                                                   "fixed_spread_quote_mid" if bid_price is not None
                                                   else "fixed_spread_last_trade"),
            "bid_price": str(bid_price) if bid_price is not None else None,
            "ask_price": str(ask_price) if ask_price is not None else None,
            "quantity_btc": str(quantity),
            "reserved_margin": str(margin), "costs": cost_basis,
            "reference_mid": str(quote_mid),
            "cfd_spread_usd_per_btc": str(rates.spread_usd_per_btc),
            "execution_side": "BUY" if side is PaperSide.LONG else "SELL",
            "execution_price": str(entry), "execution_model": rates.execution_model,
            "strategy": strategy.value, "setup_type": proposal.setup_type,
            "source_kind": "PENDING" if pending_fill else "AGENT" if decision_id else "MANUAL_PAPER",
            "fill_assumption": "closed_1m_ohlc" if pending_fill else "quote_mid" if bid_price is not None else "last_trade_mid_assumption",
            "fee_profile_version": rates.version, "fee_status": rates.status,
        }, now, position_id, decision_id)
        self.session.flush()
        return {"status": "ACCEPTED", "position_id": str(position_id), "reason": "Paper position opened"}

    def apply_holding_costs(
        self, position: PaperPositionORM, account: PaperAccountORM,
        through_time: datetime, fallback_mark: Decimal,
    ) -> int:
        require_utc(through_time)
        rates = FeeRates.from_dict(position.fee_snapshot)
        if rates.market_type is MarketType.PERPETUAL:
            rows = self.session.scalars(select(PaperFundingRateORM).where(
                PaperFundingRateORM.instrument == position.instrument,
                PaperFundingRateORM.effective_at > position.entry_at,
                PaperFundingRateORM.effective_at <= through_time,
            ).order_by(PaperFundingRateORM.effective_at)).all()
            applied = 0
            for row in rows:
                key = f"funding:{position.id}:{row.effective_at.isoformat()}"
                if self.session.scalar(select(PaperAccountLedgerORM.id).where(PaperAccountLedgerORM.event_key == key)):
                    continue
                cost = funding_cost(PaperSide(position.side), position.quantity_btc * fallback_mark, row.rate)
                charged = min(cost, account.cash_balance) if cost > ZERO else cost
                unpaid = cost - charged
                if unpaid:
                    account.status = "BANKRUPT"
                    position.unpaid_loss += unpaid
                self._ledger(account, "FUNDING", key, -charged, ZERO, through_time, position.id)
                position.accrued_holding_costs = money(position.accrued_holding_costs + charged)
                position.fee_total = money(position.fee_total + charged)
                self._event(account.id, "HOLDING_FEE_APPLIED", key,
                            {"funding_rate": str(row.rate), "source": row.source,
                             "cost_usd": str(cost), "charged_usd": str(charged),
                             "unpaid_usd": str(unpaid)}, through_time, position.id)
                applied += 1
            return applied
        periods_due = max(0, int((through_time - position.entry_at) // ROLLOVER_PERIOD))
        if rates.rollover_rate_per_4h == ZERO:
            position.last_holding_period = periods_due
            return 0
        applied = 0
        for period in range(position.last_holding_period + 1, periods_due + 1):
            due = (position.entry_at + ROLLOVER_PERIOD * period).astimezone(timezone.utc)
            candle = self.session.scalar(select(CandleORM).where(
                CandleORM.symbol == Instrument.BTC_USD,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
                CandleORM.timestamp <= due - MINUTE,
            ).order_by(CandleORM.timestamp.desc()).limit(1))
            usable_candle = candle is not None and due - (candle.timestamp + MINUTE) <= timedelta(minutes=5)
            conversion = candle.close if usable_candle else position.entry_price
            conversion_source = "closed_1m_near_due" if usable_candle else "entry_price_fallback_estimate"
            cost, basis = rollover_cost(rates, PaperSide(position.side), position.quantity_btc,
                                        position.entry_notional, conversion)
            cost = money(cost)
            charged = min(cost, account.cash_balance)
            unpaid = cost - charged
            if unpaid:
                account.status = "BANKRUPT"
                position.unpaid_loss += unpaid
            key = f"rollover:{position.id}:{period}"
            self._ledger(account, "ROLLOVER", key, -charged, ZERO, due, position.id)
            position.accrued_holding_costs = money(position.accrued_holding_costs + charged)
            position.fee_total = money(position.fee_total + charged)
            position.last_holding_period = period
            self._event(account.id, "HOLDING_FEE_APPLIED", key, {
                "period": period, "due_at": due.isoformat(), "cost_usd": str(cost),
                "charged_usd": str(charged), "unpaid_usd": str(unpaid),
                "conversion_source": conversion_source, **basis,
            }, due, position.id)
            applied += 1
        return applied

    def close_position(
        self, position_id: UUID, reason: str, mark_price: Decimal,
        price_at: datetime, *, source: str, reason_text: str = "",
        theoretical_touch_at: datetime | None = None,
        reference_price_at: datetime | None = None,
        bid_price: Decimal | None = None, ask_price: Decimal | None = None,
        barrier_fill_price: Decimal | None = None,
        fill_assumption: str | None = None,
    ) -> dict:
        require_utc(price_at)
        if reason not in {"TAKE_PROFIT", "STOP_LOSS", "AGENT", "RISK_LIMIT", "TIMEOUT", "LIQUIDATION"}:
            raise ValueError("Unsupported close reason")
        account = self._account(lock=True)
        if account is None:
            raise ValueError("Paper account does not exist")
        position = self.session.scalar(select(PaperPositionORM).where(
            PaperPositionORM.id == position_id,
            PaperPositionORM.account_id == account.id,
        ).with_for_update())
        if position is None:
            return {"status": "REJECTED", "reason": "Position does not belong to account"}
        if position.status != "OPEN":
            return {"status": "ALREADY_CLOSED", "position_id": str(position.id)}
        if mark_price <= ZERO or price_at < position.entry_at:
            return {"status": "REJECTED", "reason": "Invalid post-entry close price"}
        barrier = (position.take_profit if reason == "TAKE_PROFIT" else
                   position.stop_loss if reason == "STOP_LOSS" else None)
        if reason in {"TAKE_PROFIT", "STOP_LOSS"} and barrier is None:
            return {"status": "REJECTED", "reason": "Protective level is missing"}
        rates = FeeRates.from_dict(position.fee_snapshot)
        holding_through = theoretical_touch_at if reason in {"TAKE_PROFIT", "STOP_LOSS"} and theoretical_touch_at else price_at
        self.apply_holding_costs(position, account, holding_through, mark_price)
        # PAPER assumption: a confirmed 1m touch fills at the exact TP/SL.
        # Discretionary exits use the configured spread around the quote midpoint.
        exit_price = money((barrier_fill_price if barrier_fill_price is not None else barrier) if barrier is not None else
                           execution_price(dec(mark_price), PaperSide(position.side), False, rates,
                                           bid=bid_price, ask=ask_price))
        gross = money(gross_pnl(PaperSide(position.side), position.entry_price, exit_price, position.quantity_btc))
        exit_fee = money(position.quantity_btc * exit_price * rates.trade_exit_fee_rate)
        cash_delta = gross - exit_fee
        unpaid = max(ZERO, -(account.cash_balance + cash_delta))
        if unpaid:
            cash_delta += unpaid
            position.unpaid_loss += unpaid
            account.status = "BANKRUPT"
            reason = "LIQUIDATION"
            reason_text = f"Unfunded gap loss {unpaid} USD. {reason_text}"
        position.close_price = exit_price
        position.close_at = price_at
        position.close_source = source
        position.close_reason = reason
        position.close_reason_text = reason_text
        position.status = "LIQUIDATED" if unpaid or reason == "LIQUIDATION" else "CLOSED"
        position.realized_gross_pnl = gross
        position.realized_net_pnl = money(gross - position.initial_costs - position.accrued_holding_costs - exit_fee + unpaid)
        position.fee_total = money(position.fee_total + exit_fee)
        self._ledger(account, "MARGIN_RELEASED", f"release:{position.id}", ZERO,
                     -position.reserved_margin, price_at, position.id)
        self._ledger(account, "GROSS_PNL", f"gross:{position.id}", gross + unpaid,
                     ZERO, price_at, position.id)
        self._ledger(account, "EXIT_FEE", f"exit-fee:{position.id}", -exit_fee,
                     ZERO, price_at, position.id)
        self._event(account.id, "CLOSED", f"close:{position.id}", {
            "reason": reason, "reason_text": reason_text,
            "mark_price": str(mark_price), "execution_price": str(exit_price),
            "quote_mode": ("barrier" if barrier is not None else
                           "fixed_spread_quote_mid" if bid_price is not None else
                           "fixed_spread_last_trade"),
            "bid_price": str(bid_price) if bid_price is not None else None,
            "ask_price": str(ask_price) if ask_price is not None else None,
            "source": source, "gross_pnl": str(gross),
            "reference_mid": str((dec(bid_price) + dec(ask_price)) / 2 if bid_price is not None and ask_price is not None else mark_price),
            "cfd_spread_usd_per_btc": str(rates.spread_usd_per_btc),
            "execution_side": "SELL" if position.side == "LONG" else "BUY",
            "execution_model": rates.execution_model,
            "fill_assumption": fill_assumption or ("BARRIER_TOUCH_OHLC" if barrier is not None else
                                                   "quote_mid" if bid_price is not None else "last_trade_mid_assumption"),
            "fee_profile_version": rates.version,
            "paper_net_estimate_usd": str(position.realized_net_pnl),
            "exit_fee": str(exit_fee), "unfunded_loss": str(unpaid),
            "theoretical_touch_at": theoretical_touch_at.isoformat() if theoretical_touch_at else None,
            "reference_price_at": reference_price_at.isoformat() if reference_price_at else None,
        }, price_at, position.id)
        self.session.flush()
        return {"status": "CLOSED", "position_id": str(position.id),
                "reason": reason, "paper_net_estimate_usd": str(position.realized_net_pnl)}

    def correct_closed_barrier_fill(self, position_id: UUID, expected_reason: str,
                                    now: datetime | None = None) -> dict:
        """Auditably replace an old discretionary close after the first 1m TP/SL hit."""
        now = now or utc_now()
        require_utc(now)
        if expected_reason not in {"TAKE_PROFIT", "STOP_LOSS"}:
            raise ValueError("Expected TAKE_PROFIT or STOP_LOSS")
        account = self._account(lock=True)
        if account is None:
            raise ValueError("Paper account does not exist")
        position = self.session.scalar(select(PaperPositionORM).where(
            PaperPositionORM.id == position_id,
            PaperPositionORM.account_id == account.id,
        ).with_for_update())
        if position is None:
            raise ValueError("Position does not belong to paper account")
        key = f"barrier-fill-correction-v1:{position.id}"
        if self.session.scalar(select(PaperPositionEventORM.id).where(
            PaperPositionEventORM.dedupe_key == key
        )):
            return {"status": "ALREADY_CORRECTED", "position_id": str(position.id)}
        if position.status != "CLOSED" or position.close_at is None or position.unpaid_loss != ZERO:
            raise ValueError("Only a normally closed position can be corrected")
        start = position.entry_at.replace(second=0, microsecond=0)
        if start < position.entry_at:
            start += MINUTE
        candles = self.session.scalars(select(CandleORM).where(
            CandleORM.symbol == Instrument.BTC_USD,
            CandleORM.timeframe == Timeframe.ONE_MINUTE,
            CandleORM.timestamp >= start,
            CandleORM.timestamp + MINUTE <= position.close_at,
        ).order_by(CandleORM.timestamp)).all()
        first_hit = None
        for index, candle in enumerate(candles):
            if candle.timestamp != start + MINUTE * index:
                raise ValueError("Cannot verify first barrier because 1m candles are missing")
            rates = FeeRates.from_dict(position.fee_snapshot)
            hit_reason, ambiguous, fill_price, assumption = barrier_touch(
                PaperSide(position.side), candle, position.stop_loss, position.take_profit, rates)
            if hit_reason:
                first_hit = (candle, hit_reason, ambiguous, fill_price, assumption)
                break
        if first_hit is None:
            raise ValueError("No recorded 1m candle touched a protective level before close")
        candle, reason, ambiguous, fill_price, assumption = first_hit
        if reason != expected_reason:
            raise ValueError(f"First barrier was {reason}, not {expected_reason}")
        barrier = position.stop_loss if reason == "STOP_LOSS" else position.take_profit
        rates = FeeRates.from_dict(position.fee_snapshot)
        new_exit_fee = money(position.quantity_btc * fill_price * rates.trade_exit_fee_rate)
        new_gross = money(gross_pnl(PaperSide(position.side), position.entry_price,
                                    fill_price, position.quantity_btc))
        new_net = money(new_gross - position.initial_costs - position.accrued_holding_costs - new_exit_fee)
        old = {
            "close_price": str(position.close_price), "close_at": iso_utc(position.close_at),
            "close_source": position.close_source, "close_reason": position.close_reason,
            "close_reason_text": position.close_reason_text,
            "realized_gross_pnl": str(position.realized_gross_pnl),
            "realized_net_pnl": str(position.realized_net_pnl), "fee_total": str(position.fee_total),
        }
        delta = money(new_net - position.realized_net_pnl)
        touch_at = (candle.timestamp + MINUTE).astimezone(timezone.utc)
        self._ledger(account, "CORRECTION", key, delta, ZERO, now, position.id)
        position.close_price = fill_price
        position.close_at = touch_at
        position.close_source = "1m_ohlc_barrier_assumption"
        position.close_reason = reason
        position.close_reason_text = (
            "User-requested paper correction: first complete post-entry 1m candle touched "
            f"{reason} at configured level. " +
            ("AMBIGUOUS_BOTH_HIT: SL FIRST. " if ambiguous else "") +
            "Original close retained in CORRECTED event."
        )
        position.realized_gross_pnl = new_gross
        position.realized_net_pnl = new_net
        position.fee_total = money(position.initial_costs + position.accrued_holding_costs + new_exit_fee)
        position.last_evaluated_candle_timestamp = candle.timestamp
        self._event(account.id, "CORRECTED", key, {
            "reason": "user_requested_exact_barrier_paper_fill",
            "old": old,
            "new": {
                "close_price": str(fill_price), "close_at": iso_utc(touch_at),
                "close_source": position.close_source, "close_reason": reason,
                "realized_gross_pnl": str(new_gross), "realized_net_pnl": str(new_net),
                "fee_total": str(position.fee_total),
            },
            "barrier_candle_timestamp": iso_utc(candle.timestamp),
            "barrier_candle_high": str(candle.high), "barrier_candle_low": str(candle.low),
            "ambiguous_both_hit": ambiguous, "cash_adjustment_usd": str(delta),
            "reference_mid": str(candle.open if assumption == "GAP_FILL_ASSUMPTION" else
                                 (fill_price + rates.spread_usd_per_btc / 2 if position.side == "LONG" else
                                  fill_price - rates.spread_usd_per_btc / 2)),
            "cfd_spread_usd_per_btc": str(rates.spread_usd_per_btc),
            "execution_side": "SELL" if position.side == "LONG" else "BUY",
            "execution_price": str(fill_price), "execution_model": rates.execution_model,
            "fill_assumption": assumption, "fee_profile_version": rates.version,
        }, now, position.id)
        self.session.flush()
        return {"status": "CORRECTED", "position_id": str(position.id),
                "close_reason": reason, "close_price": str(fill_price),
                "realized_net_pnl": str(new_net), "cash_adjustment_usd": str(delta)}

    def process_decision(self, decision_id: UUID, ticker: dict | None,
                         now: datetime | None = None) -> dict:
        now = now or utc_now()
        require_utc(now)
        row = self.session.scalar(select(AgentDecisionORM).where(
            AgentDecisionORM.id == decision_id).with_for_update())
        if row is None:
            raise ValueError("Decision does not exist")
        if row.paper_status == "DONE":
            return {"status": "DONE", "reason": "Already processed"}
        account = self._account()
        if account is None:
            return {"status": "PENDING", "reason": "Paper account not seeded"}
        action = AgentDecision.model_validate({
            "decision": row.decision,
            "open_proposal": row.open_proposal,
            "position_actions": row.position_actions or [],
            "pending_order_actions": row.pending_order_actions or [],
            "to_monitor": [],
            "text": row.text,
        })
        if now - row.created_at > timedelta(minutes=2):
            for item in action.position_actions:
                self._reject(account, f"{decision_id}:action:{item.position_id}",
                             "Decision action became stale", now, decision_id)
            if action.open_proposal:
                self._reject(account, f"{decision_id}:open", "Open proposal became stale", now, decision_id)
            if action.pending_order_actions:
                self._event(account.id, "REJECTED", f"reject:{decision_id}:pending-stale",
                            {"reason": "Pending order actions became stale"}, now, decision_id=decision_id)
            row.paper_status = "DONE"
            row.paper_result = {"status": "DONE", "reason": "Stale actions rejected"}
            return row.paper_result
        needs_price = action.open_proposal is not None or any(
            item.action is PositionActionType.CLOSE for item in action.position_actions
        ) or any(item.action.value in {"CREATE", "AMEND"} for item in action.pending_order_actions)
        if needs_price and ticker is None:
            return {"status": "PENDING", "reason": "Fresh ticker required"}
        if ticker:
            mark_price = dec(ticker["price"])
            bid_price = dec(ticker["bid"]) if "bid" in ticker else None
            ask_price = dec(ticker["ask"]) if "ask" in ticker else None
            price_at = datetime.fromisoformat(ticker["retrieved_at"])
            require_utc(price_at)
            if price_at <= row.created_at or now - price_at > timedelta(seconds=30):
                return {"status": "PENDING", "reason": "Fresh post-analysis ticker required"}
        else:
            mark_price = ZERO
            bid_price = ask_price = None
            price_at = now
        if ticker is not None:
            # A stored 1m TP/SL takes priority even if the minute collector
            # missed its first chance to obtain a post-close ticker.
            self.process_new_candles(processed_at=now, ticker=ticker)
        results = []
        for item in action.position_actions:
            position = self.session.get(PaperPositionORM, item.position_id)
            if position is None or position.account_id != account.id or position.status != "OPEN":
                results.append(self._reject(account, f"{decision_id}:action:{item.position_id}",
                                            "Position is closed or outside the account", now, decision_id))
                continue
            if item.action is PositionActionType.HOLD:
                continue
            close_result = self.close_position(
                item.position_id, "AGENT", mark_price, price_at,
                source=ticker["source"], reason_text=item.reason,
                bid_price=bid_price, ask_price=ask_price,
            )
            if close_result["status"] == "ALREADY_CLOSED":
                close_result = self._reject(
                    account, f"{decision_id}:action:{item.position_id}",
                    "Position was auto-closed before agent action", now, decision_id,
                )
            results.append(close_result)
        if action.open_proposal:
            results.append(self.open_position(
                action.open_proposal, mark_price, price_at,
                execution_key=f"{decision_id}:open", decision_id=decision_id,
                source=ticker["source"], now=now,
                bid_price=bid_price, ask_price=ask_price,
            ))
        if action.pending_order_actions:
            from services.pending_orders import PendingOrderManager
            manager = PendingOrderManager(self.session, self)
            for index, item in enumerate(action.pending_order_actions):
                results.append(manager.apply_action(item, decision_id, ticker, now, action_index=index))
        row.paper_status = "DONE"
        row.paper_result = {"status": "DONE", "results": results}
        self.session.flush()
        return row.paper_result

    def process_new_candles(self, processed_at: datetime | None = None,
                            ticker: dict | None = None) -> dict:
        """Evaluate closed 1m bars; a TP/SL touch fills at its configured level.

        This is an explicit PAPER assumption based on OHLC, not an exchange fill.
        Other risk exits still need a post-signal price reference.
        """
        processed_at = processed_at or utc_now()
        require_utc(processed_at)
        ticker_price = None
        ticker_at = None
        ticker_bid = ticker_ask = None
        if ticker is not None:
            try:
                ticker_price = dec(ticker["price"])
                ticker_bid = dec(ticker["bid"]) if "bid" in ticker else None
                ticker_ask = dec(ticker["ask"]) if "ask" in ticker else None
                ticker_at = datetime.fromisoformat(ticker["retrieved_at"])
                require_utc(ticker_at)
                if (not ticker_price.is_finite() or ticker_price <= ZERO
                    or ticker_at > processed_at + timedelta(seconds=5)
                    or processed_at - ticker_at > timedelta(seconds=30)):
                    ticker_price = ticker_at = None
            except (InvalidOperation, KeyError, TypeError, ValueError):
                ticker_price = ticker_at = None
                ticker_bid = ticker_ask = None
        account = self._account(lock=True)
        if account is None:
            return {"evaluated": 0, "closed": 0, "pending_price": 0}
        positions = self._positions(account.id, lock=True)
        evaluated = closed = pending_price = 0
        for position in positions:
            if position.status != "OPEN":
                continue
            start = position.entry_at.replace(second=0, microsecond=0)
            if start < position.entry_at:
                start += MINUTE
            if position.last_evaluated_candle_timestamp is not None:
                start = max(start, position.last_evaluated_candle_timestamp + MINUTE)
            candles = self.session.scalars(select(CandleORM).where(
                CandleORM.symbol == Instrument.BTC_USD,
                CandleORM.timeframe == Timeframe.ONE_MINUTE,
                CandleORM.timestamp >= start,
                CandleORM.timestamp + MINUTE <= processed_at,
            ).order_by(CandleORM.timestamp)).all()
            for index, candle in enumerate(candles):
                # A position entered during a bar is first evaluated against a
                # later complete bar. The final bar needs a post-close ticker.
                if candle.timestamp < start or position.status != "OPEN":
                    continue
                next_candle = candles[index + 1] if index + 1 < len(candles) else None
                rates = FeeRates.from_dict(position.fee_snapshot)
                reason, ambiguous, barrier_price, barrier_assumption = barrier_touch(
                    PaperSide(position.side), candle, position.stop_loss, position.take_profit, rates)
                hold_minutes = (int((position.strategy_parameters or {}).get("max_hold_minutes", 60))
                                if position.strategy == Strategy.SCALP_M5.value else
                                int((position.strategy_parameters or {}).get("max_hold_hours", self.risk.max_hold_hours)) * 60)
                if reason is None and candle.timestamp + MINUTE >= position.entry_at + timedelta(minutes=hold_minutes):
                    reason = "TIMEOUT"
                if reason is None:
                    open_at_bar = self._positions(account.id)
                    equity_at_bar = account.cash_balance + sum((
                        gross_pnl(PaperSide(p.side), p.entry_price, candle.close, p.quantity_btc)
                        for p in open_at_bar
                    ), ZERO)
                    if equity_at_bar <= account.initial_balance * (Decimal("1") - self.risk.emergency_drawdown):
                        reason = "RISK_LIMIT"
                if reason is None and position.stop_loss is None:
                    if candle.timestamp + MINUTE >= position.entry_at + timedelta(hours=self.risk.no_sl_max_hours):
                        reason = "TIMEOUT"
                    elif gross_pnl(PaperSide(position.side), position.entry_price,
                                   candle.close, position.quantity_btc) < -account.initial_balance * self.risk.emergency_drawdown:
                        reason = "RISK_LIMIT"
                if reason:
                    barrier_hit = reason in {"TAKE_PROFIT", "STOP_LOSS"}
                    if not barrier_hit and next_candle is None and (
                        ticker_price is None or ticker_at is None or
                        ticker_at < candle.timestamp + MINUTE
                    ):
                        # Keep this bar pending. A later fresh ticker or the next
                        # persisted bar can provide a post-signal reference price.
                        pending_price += 1
                        break
                    notes = []
                    if ambiguous:
                        notes.append("AMBIGUOUS_BOTH_HIT: SL FIRST")
                    if not barrier_hit and next_candle is not None and next_candle.timestamp != candle.timestamp + MINUTE:
                        notes.append("GAP: next available candle open used")
                    fill_reference = (
                        candle.open if barrier_assumption == "GAP_FILL_ASSUMPTION" else
                        barrier_price + (rates.spread_usd_per_btc / 2 if position.side == "LONG" else
                                         -rates.spread_usd_per_btc / 2)
                    ) if barrier_hit and rates.execution_model == CFD_FIXED_20_USD else (
                        barrier_price if barrier_hit else next_candle.open if next_candle is not None else ticker_price
                    )
                    fill_at = (candle.timestamp + MINUTE).astimezone(timezone.utc) if barrier_hit else processed_at if next_candle is not None else ticker_at
                    reference_at = None if barrier_hit else (next_candle.timestamp if next_candle is not None else ticker_at)
                    source = ("1m_ohlc_barrier_assumption" if barrier_hit else
                              "next_1m_open_ohlc_backtest" if next_candle is not None else
                              "post_close_ticker_ohlc_signal")
                    self.close_position(
                        position.id, reason, fill_reference, fill_at,
                        source=source,
                        reason_text="; ".join(notes),
                        theoretical_touch_at=(candle.timestamp + MINUTE).astimezone(timezone.utc),
                        reference_price_at=reference_at.astimezone(timezone.utc) if reference_at else None,
                        bid_price=ticker_bid if not barrier_hit and next_candle is None else None,
                        ask_price=ticker_ask if not barrier_hit and next_candle is None else None,
                        barrier_fill_price=barrier_price if barrier_hit else None,
                        fill_assumption=barrier_assumption if barrier_hit else None,
                    )
                    position.last_evaluated_candle_timestamp = candle.timestamp
                    evaluated += 1
                    closed += 1
                    break
                position.last_evaluated_candle_timestamp = candle.timestamp
                evaluated += 1
            if position.status == "OPEN" and candles:
                self.apply_holding_costs(position, account,
                                         min(processed_at, candles[-1].timestamp + MINUTE).astimezone(timezone.utc),
                                         candles[-1].close)
                if account.status == "BANKRUPT":
                    self.close_position(
                        position.id, "LIQUIDATION", candles[-1].close, processed_at,
                        source="risk_liquidation_closed_1m_mark",
                        reason_text="Holding costs exhausted paper cash",
                    )
                    closed += 1
        self.session.flush()
        return {"evaluated": evaluated, "closed": closed, "pending_price": pending_price}
