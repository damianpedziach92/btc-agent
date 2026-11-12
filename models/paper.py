import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum


ZERO = Decimal("0")
ONE = Decimal("1")
BTC_STEP = Decimal("0.00000001")
MONEY_STEP = Decimal("0.000000000001")
CFD_SPREAD_USD_PER_BTC = Decimal("20")
CFD_FIXED_20_USD = "CFD_FIXED_20_USD"
LEGACY_BPS = "LEGACY_BPS"


def dec(value: str | int | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_STEP)


class MarketType(str, Enum):
    SPOT_MARGIN = "SPOT_MARGIN"
    PERPETUAL = "PERPETUAL"


class PaperSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_position: Decimal = Decimal("0.01")
    max_total_risk: Decimal = Decimal("0.03")
    max_position_margin: Decimal = Decimal("0.20")
    max_total_margin: Decimal = Decimal("0.60")
    min_free_margin: Decimal = Decimal("0.20")
    max_positions: int = 5
    min_notional: Decimal = Decimal("10")
    min_tp_net_profit_usd: Decimal = Decimal("0.01")
    allow_positions_without_sl: bool = False
    no_sl_max_margin: Decimal = Decimal("0.05")
    no_sl_max_hours: int = 4
    max_hold_hours: int = 24
    emergency_drawdown: Decimal = Decimal("0.10")
    max_candle_age_seconds: int = 150
    min_consecutive_1m: int = 3
    min_stop_m15_atr_multiple: Decimal = Decimal("0")
    stop_swing_m15_candles: int = 2
    stop_swing_atr_buffer: Decimal = Decimal("0")
    experimental_min_rr_after_costs: Decimal = ZERO
    experimental_max_chase_atr: Decimal = ZERO
    pending_max_active: int = 3
    pending_max_ttl_minutes: int = 60
    pending_max_distance_usd: Decimal = Decimal("2000")

    @classmethod
    def from_env(cls) -> "RiskConfig":
        def get(name: str, default: str) -> str:
            return os.getenv(f"PAPER_{name}", default)

        config = cls(
            max_risk_per_position=dec(get("MAX_RISK_PER_POSITION", "0.01")),
            max_total_risk=dec(get("MAX_TOTAL_RISK", "0.03")),
            max_position_margin=dec(get("MAX_POSITION_MARGIN", "0.20")),
            max_total_margin=dec(get("MAX_TOTAL_MARGIN", "0.60")),
            min_free_margin=dec(get("MIN_FREE_MARGIN", "0.20")),
            max_positions=int(get("MAX_POSITIONS", "5")),
            min_notional=dec(get("MIN_NOTIONAL", "10")),
            min_tp_net_profit_usd=dec(get("MIN_TP_NET_PROFIT_USD", "0.01")),
            allow_positions_without_sl=get("ALLOW_POSITIONS_WITHOUT_SL", "false").lower() == "true",
            no_sl_max_margin=dec(get("NO_SL_MAX_MARGIN", "0.05")),
            no_sl_max_hours=int(get("NO_SL_MAX_HOURS", "4")),
            max_hold_hours=int(get("MAX_HOLD_HOURS", "24")),
            emergency_drawdown=dec(get("EMERGENCY_DRAWDOWN", "0.10")),
            max_candle_age_seconds=int(get("MAX_CANDLE_AGE_SECONDS", "150")),
            min_consecutive_1m=int(get("MIN_CONSECUTIVE_1M", "3")),
            min_stop_m15_atr_multiple=dec(get("MIN_STOP_M15_ATR_MULTIPLE", "0")),
            stop_swing_m15_candles=int(get("STOP_SWING_M15_CANDLES", "2")),
            stop_swing_atr_buffer=dec(get("STOP_SWING_ATR_BUFFER", "0")),
            experimental_min_rr_after_costs=dec(get("EXPERIMENTAL_MIN_RR_AFTER_COSTS", "0")),
            experimental_max_chase_atr=dec(get("EXPERIMENTAL_MAX_CHASE_ATR", "0")),
            pending_max_active=int(get("PENDING_MAX_ACTIVE", "3")),
            pending_max_ttl_minutes=int(get("PENDING_MAX_TTL_MINUTES", "60")),
            pending_max_distance_usd=dec(get("PENDING_MAX_DISTANCE_USD", "2000")),
        )
        fractions = (
            config.max_risk_per_position, config.max_total_risk,
            config.max_position_margin, config.max_total_margin,
            config.min_free_margin, config.no_sl_max_margin,
            config.emergency_drawdown,
        )
        if any(not ZERO < value <= ONE for value in fractions):
            raise ValueError("Paper risk fractions must be between 0 and 1")
        if config.max_total_margin > ONE - config.min_free_margin:
            raise ValueError("Paper margin cap exceeds the free-margin reserve")
        if config.max_positions < 1 or config.no_sl_max_hours < 1 or config.max_hold_hours < 1:
            raise ValueError("Paper position count and no-SL hold time must be positive")
        if (config.min_notional <= ZERO or config.min_tp_net_profit_usd <= ZERO or
            config.max_candle_age_seconds < 1 or config.min_consecutive_1m < 1):
            raise ValueError("Paper minimum notional, TP net profit and candle age must be positive")
        if (config.min_stop_m15_atr_multiple < ZERO or config.stop_swing_atr_buffer < ZERO or
            config.experimental_min_rr_after_costs < ZERO or
            config.experimental_max_chase_atr < ZERO or
            config.pending_max_distance_usd <= ZERO or not 1 <= config.pending_max_active <= 10 or
            not 1 <= config.pending_max_ttl_minutes <= 240 or
            config.stop_swing_m15_candles < 1):
            raise ValueError("Paper M15 stop configuration must be nonnegative with a positive swing length")
        return config

    def as_dict(self) -> dict:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in vars(self).items()}


@dataclass(frozen=True)
class FeeRates:
    market_type: MarketType
    status: str
    source: str
    version: int
    trade_entry_fee_rate: Decimal
    trade_exit_fee_rate: Decimal
    margin_opening_fee_rate: Decimal
    rollover_rate_per_4h: Decimal
    slippage_bps: Decimal
    spread_bps: Decimal
    execution_model: str = LEGACY_BPS
    spread_usd_per_btc: Decimal = ZERO

    def as_dict(self) -> dict:
        return {key: value.value if isinstance(value, Enum) else str(value) if isinstance(value, Decimal) else value
                for key, value in vars(self).items()}

    @classmethod
    def from_dict(cls, data: dict) -> "FeeRates":
        return cls(
            market_type=MarketType(data["market_type"]), status=data["status"],
            source=data["source"], version=int(data["version"]),
            trade_entry_fee_rate=dec(data["trade_entry_fee_rate"]),
            trade_exit_fee_rate=dec(data["trade_exit_fee_rate"]),
            margin_opening_fee_rate=dec(data["margin_opening_fee_rate"]),
            rollover_rate_per_4h=dec(data["rollover_rate_per_4h"]),
            slippage_bps=dec(data["slippage_bps"]), spread_bps=dec(data["spread_bps"]),
            execution_model=data.get("execution_model", LEGACY_BPS),
            spread_usd_per_btc=dec(data.get("spread_usd_per_btc", "0")),
        )


def cfd20_rates() -> FeeRates:
    return FeeRates(
        MarketType.SPOT_MARGIN, "ESTIMATED", "user_requested_cfd_fixed_20_usd_spread", 1,
        ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, CFD_FIXED_20_USD, CFD_SPREAD_USD_PER_BTC,
    )


def execution_price(mark: Decimal, side: PaperSide, opening: bool, rates: FeeRates,
                    bid: Decimal | None = None, ask: Decimal | None = None) -> Decimal:
    """Apply the configured PAPER spread around the observed quote midpoint."""
    if not mark.is_finite() or mark <= ZERO:
        raise ValueError("Price must be positive")
    buying = (side is PaperSide.LONG) == opening
    if (bid is None) != (ask is None):
        raise ValueError("Bid and ask must be supplied together")
    if bid is not None:
        bid, ask = dec(bid), dec(ask)
        if not bid.is_finite() or not ask.is_finite() or bid <= ZERO or ask < bid:
            raise ValueError("Invalid bid/ask quote")
        reference = (bid + ask) / 2
    else:
        reference = mark
    if rates.execution_model == CFD_FIXED_20_USD:
        return reference + (rates.spread_usd_per_btc / 2 if buying else -rates.spread_usd_per_btc / 2)
    if rates.execution_model != LEGACY_BPS:
        raise ValueError("Unknown PAPER execution model")
    impact = (rates.spread_bps / 2 + rates.slippage_bps) / Decimal("10000")
    return reference * (ONE + impact if buying else ONE - impact)


def barrier_touch(side: PaperSide, candle, stop_loss: Decimal | None,
                  take_profit: Decimal | None, rates: FeeRates) -> tuple[str | None, bool, Decimal | None, str]:
    """Return first conservative OHLC barrier and executable fill price."""
    offset = rates.spread_usd_per_btc / 2 if rates.execution_model == CFD_FIXED_20_USD else ZERO
    low = candle.low - offset if side is PaperSide.LONG else candle.low + offset
    high = candle.high - offset if side is PaperSide.LONG else candle.high + offset
    opened = candle.open - offset if side is PaperSide.LONG else candle.open + offset
    stop_hit = stop_loss is not None and (low <= stop_loss if side is PaperSide.LONG else high >= stop_loss)
    target_hit = take_profit is not None and (high >= take_profit if side is PaperSide.LONG else low <= take_profit)
    if stop_hit:
        worse_gap = rates.execution_model == CFD_FIXED_20_USD and (
            opened < stop_loss if side is PaperSide.LONG else opened > stop_loss)
        return "STOP_LOSS", target_hit, opened if worse_gap else stop_loss, "GAP_FILL_ASSUMPTION" if worse_gap else "BARRIER_TOUCH_OHLC"
    if target_hit:
        return "TAKE_PROFIT", False, take_profit, "BARRIER_TOUCH_OHLC"
    return None, False, None, "NO_TOUCH"


def gross_pnl(side: PaperSide, entry: Decimal, exit_price: Decimal, quantity: Decimal) -> Decimal:
    return (exit_price - entry if side is PaperSide.LONG else entry - exit_price) * quantity


def entry_costs(rates: FeeRates, side: PaperSide, quantity: Decimal, entry: Decimal) -> tuple[Decimal, dict]:
    notional = quantity * entry
    trade_fee = notional * rates.trade_entry_fee_rate
    if rates.market_type is MarketType.SPOT_MARGIN:
        if side is PaperSide.SHORT:
            margin_fee_btc = quantity * rates.margin_opening_fee_rate
            margin_fee = margin_fee_btc * entry
            basis = {"borrowed_btc": str(quantity), "fee_btc": str(margin_fee_btc), "conversion_usd_per_btc": str(entry)}
        else:
            margin_fee = notional * rates.margin_opening_fee_rate
            basis = {"borrowed_usd": str(notional)}
    else:
        margin_fee = ZERO
        basis = {"perpetual_margin_opening_fee": "not_applicable"}
    return trade_fee + margin_fee, {"trade_fee_usd": str(trade_fee), "margin_opening_fee_usd": str(margin_fee), **basis}


def rollover_cost(rates: FeeRates, side: PaperSide, quantity: Decimal, entry_notional: Decimal, conversion_price: Decimal) -> tuple[Decimal, dict]:
    if rates.market_type is not MarketType.SPOT_MARGIN:
        raise ValueError("Rollover applies only to SPOT_MARGIN")
    if side is PaperSide.SHORT:
        fee_btc = quantity * rates.rollover_rate_per_4h
        return fee_btc * conversion_price, {"fee_btc": str(fee_btc), "conversion_usd_per_btc": str(conversion_price)}
    return entry_notional * rates.rollover_rate_per_4h, {"borrowed_usd": str(entry_notional)}


def funding_cost(side: PaperSide, notional: Decimal, signed_rate: Decimal) -> Decimal:
    """Positive rate: LONG pays, SHORT receives; negative rate reverses it."""
    return notional * signed_rate * (ONE if side is PaperSide.LONG else -ONE)


def estimated_target_net_pnl(
    rates: FeeRates, side: PaperSide, entry: Decimal, target: Decimal,
    quantity: Decimal, stop_loss: Decimal | None, rollover_periods: int,
) -> Decimal:
    """Conservative PAPER net at exact TP, including costs through the hold limit."""
    if rates.market_type is not MarketType.SPOT_MARGIN:
        raise ValueError("A sourced funding model is required to estimate PERPETUAL target net")
    if rollover_periods < 0:
        raise ValueError("Rollover period count cannot be negative")
    initial = money(entry_costs(rates, side, quantity, entry)[0])
    exit_fee = money(quantity * target * rates.trade_exit_fee_rate)
    conversion = max(entry, stop_loss or entry) if side is PaperSide.SHORT else entry
    one_rollover = money(rollover_cost(rates, side, quantity, entry * quantity, conversion)[0])
    return money(
        money(gross_pnl(side, entry, target, quantity)) - initial - exit_fee -
        one_rollover * rollover_periods
    )


class RiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def size(
        self, *, side: PaperSide, entry: Decimal, stop_loss: Decimal | None,
        take_profit: Decimal | None, equity: Decimal, cash: Decimal,
        reserved: Decimal, current_risk: Decimal, position_count: int,
        rates: FeeRates,
    ) -> tuple[Decimal | None, str]:
        c = self.config
        if rates.status == "UNCONFIGURED":
            return None, "Fee profile is UNCONFIGURED"
        if position_count >= c.max_positions:
            return None, "Maximum open position count reached"
        if equity <= ZERO or cash <= ZERO:
            return None, "No positive equity or cash"
        if stop_loss is None and not c.allow_positions_without_sl:
            return None, "Stop loss required by paper risk policy"
        if stop_loss is not None and ((side is PaperSide.LONG and stop_loss >= entry) or
                                      (side is PaperSide.SHORT and stop_loss <= entry)):
            return None, "Stop loss must be on the loss side of entry"
        if take_profit is not None and ((side is PaperSide.LONG and take_profit <= entry) or
                                        (side is PaperSide.SHORT and take_profit >= entry)):
            return None, "Take profit must be on the profit side of entry"

        single_limit = c.no_sl_max_margin if stop_loss is None else c.max_position_margin
        # equity is marked at executable liquidation prices before this entry.
        if rates.execution_model == CFD_FIXED_20_USD:
            exit_at_same_mid = entry - rates.spread_usd_per_btc if side is PaperSide.LONG else entry + rates.spread_usd_per_btc
        else:
            impact = (rates.spread_bps / 2 + rates.slippage_bps) / Decimal("10000")
            mid = entry / (ONE + impact if side is PaperSide.LONG else ONE - impact)
            exit_at_same_mid = execution_price(mid, side, False, rates)
        entry_cost_per_btc = entry_costs(rates, side, ONE, entry)[0]
        immediate_cost_per_btc = (entry_cost_per_btc +
                                  ONE * exit_at_same_mid * rates.trade_exit_fee_rate -
                                  gross_pnl(side, entry, exit_at_same_mid, ONE))
        # Solve the post-cost margin inequalities for q; rounding down keeps
        # the later exact checks within limits even at the boundary.
        limits = [
            (cash - reserved - equity * c.min_free_margin) /
            (entry + entry_cost_per_btc - c.min_free_margin * immediate_cost_per_btc),
            equity * single_limit / (entry + single_limit * immediate_cost_per_btc),
            (equity * c.max_total_margin - reserved) /
            (entry + c.max_total_margin * immediate_cost_per_btc),
        ]
        cap_quantity = min(limits)
        if stop_loss is not None:
            risk_budget = min(equity * c.max_risk_per_position,
                              equity * c.max_total_risk - current_risk)
            per_btc = abs(entry - stop_loss) + entry_cost_per_btc + stop_loss * rates.trade_exit_fee_rate + (
                entry * rates.rollover_rate_per_4h if rates.market_type is MarketType.SPOT_MARGIN else ZERO)
            if risk_budget <= ZERO or per_btc <= ZERO:
                return None, "Aggregate stop risk limit reached"
            cap_quantity = min(cap_quantity, risk_budget / per_btc)
        quantity = cap_quantity.quantize(BTC_STEP, rounding=ROUND_DOWN)
        if quantity <= ZERO or quantity * entry < c.min_notional:
            return None, (f"Calculated quantity below minimum notional: required_margin={c.min_notional}, "
                          f"available_margin={max(ZERO, cash-reserved)}, estimated_close_cost={immediate_cost_per_btc}, "
                          f"max_quantity={max(ZERO, quantity)}, min_notional={c.min_notional}, "
                          f"risk_budget={risk_budget if stop_loss is not None else 'n/a'}")
        if take_profit is not None:
            hold_hours = c.no_sl_max_hours if stop_loss is None else c.max_hold_hours
            rollover_periods = hold_hours // 4
            projected_net = estimated_target_net_pnl(
                rates, side, entry, take_profit, quantity, stop_loss, rollover_periods
            )
            if projected_net < c.min_tp_net_profit_usd:
                return None, (
                    f"Take profit would yield estimated net {projected_net} USD after "
                    f"entry/exit fees and {rollover_periods} possible rollover periods; "
                    f"minimum is {c.min_tp_net_profit_usd} USD"
                )
            if rates.execution_model == CFD_FIXED_20_USD and stop_loss is not None and c.experimental_min_rr_after_costs > ZERO:
                risk_per_btc = abs(entry-stop_loss) + entry_cost_per_btc + stop_loss*rates.trade_exit_fee_rate
                if projected_net / (risk_per_btc*quantity) < c.experimental_min_rr_after_costs:
                    return None, (f"RR after costs below experimental minimum {c.experimental_min_rr_after_costs}; "
                                  f"actual={projected_net/(risk_per_btc*quantity):.3f}")
        return quantity, "ACCEPTED"
