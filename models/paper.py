import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum


ZERO = Decimal("0")
ONE = Decimal("1")
BTC_STEP = Decimal("0.00000001")
MONEY_STEP = Decimal("0.000000000001")


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
    impact = (rates.spread_bps / 2 + rates.slippage_bps) / Decimal("10000")
    return reference * (ONE + impact if buying else ONE - impact)


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

        available = min(cash - reserved, equity * c.max_total_margin - reserved,
                        equity * (ONE - c.min_free_margin) - reserved)
        single_limit = c.no_sl_max_margin if stop_loss is None else c.max_position_margin
        estimated_loss_rate = (
            rates.trade_entry_fee_rate + rates.trade_exit_fee_rate +
            (rates.margin_opening_fee_rate if rates.market_type is MarketType.SPOT_MARGIN else ZERO) +
            (rates.spread_bps / 2 + rates.slippage_bps) / Decimal("10000")
        )
        cap = min(
            available,
            equity * single_limit / (ONE + single_limit * estimated_loss_rate),
            (equity * c.max_total_margin - reserved) / (ONE + c.max_total_margin * estimated_loss_rate),
        )
        if stop_loss is not None:
            risk_budget = min(equity * c.max_risk_per_position,
                              equity * c.max_total_risk - current_risk)
            per_btc = abs(entry - stop_loss) + entry * (
                rates.trade_entry_fee_rate + rates.trade_exit_fee_rate +
                (rates.margin_opening_fee_rate + rates.rollover_rate_per_4h
                 if rates.market_type is MarketType.SPOT_MARGIN else ZERO)
            )
            if risk_budget <= ZERO or per_btc <= ZERO:
                return None, "Aggregate stop risk limit reached"
            cap = min(cap, risk_budget * entry / per_btc)
        if cap < c.min_notional:
            return None, "Insufficient free margin or risk budget"
        quantity = (cap / entry).quantize(BTC_STEP, rounding=ROUND_DOWN)
        if quantity <= ZERO or quantity * entry < c.min_notional:
            return None, "Calculated quantity below minimum notional"
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
        return quantity, "ACCEPTED"
