from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base

MONEY = Numeric(28, 12)


class PaperAccountORM(Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (
        UniqueConstraint("name", name="uq_paper_accounts_name"),
        CheckConstraint("initial_balance > 0 AND cash_balance >= 0", name="ck_paper_account_balances"),
        CheckConstraint("currency = 'USD'", name="ck_paper_account_currency"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    initial_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperFeeProfileORM(Base):
    __tablename__ = "paper_fee_profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "version", name="uq_paper_fee_profile_version"),
        CheckConstraint("market_type IN ('SPOT_MARGIN', 'PERPETUAL')", name="ck_paper_fee_market_type"),
        CheckConstraint("status IN ('UNCONFIGURED', 'ESTIMATED', 'OBSERVED')", name="ck_paper_fee_status"),
        CheckConstraint(
            "trade_entry_fee_rate >= 0 AND trade_exit_fee_rate >= 0 AND "
            "margin_opening_fee_rate >= 0 AND rollover_rate_per_4h >= 0 AND "
            "slippage_bps >= 0 AND spread_bps >= 0",
            name="ck_paper_fee_nonnegative",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("paper_accounts.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    market_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    trade_entry_fee_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    trade_exit_fee_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    margin_opening_fee_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    rollover_rate_per_4h: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    slippage_bps: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    spread_bps: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperPositionORM(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("agent_decision_id", name="uq_paper_position_agent_decision"),
        UniqueConstraint("execution_key", name="uq_paper_position_execution_key"),
        CheckConstraint("side IN ('LONG', 'SHORT')", name="ck_paper_position_side"),
        CheckConstraint("status IN ('OPEN', 'CLOSED', 'LIQUIDATED')", name="ck_paper_position_status"),
        CheckConstraint("entry_price > 0 AND quantity_btc > 0 AND entry_notional > 0 AND reserved_margin > 0", name="ck_paper_position_values"),
        CheckConstraint("leverage = 1", name="ck_paper_position_leverage"),
        CheckConstraint("stop_loss IS NULL OR stop_loss > 0", name="ck_paper_position_sl"),
        CheckConstraint("take_profit IS NULL OR take_profit > 0", name="ck_paper_position_tp"),
        Index("ix_paper_position_account_status", "account_id", "status"),
        Index("ix_paper_position_entry_time", "entry_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("paper_accounts.id"), nullable=False)
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    market_type: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_candle_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_source: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_key: Mapped[str] = mapped_column(String(160), nullable=False)
    quantity_btc: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_notional: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    leverage: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reserved_margin: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(MONEY)
    take_profit: Mapped[Decimal | None] = mapped_column(MONEY)
    entry_reason: Mapped[str] = mapped_column(Text, nullable=False)
    agent_decision_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("agent_decisions.id"))
    scenario_key: Mapped[str | None] = mapped_column(String(80))
    fee_profile_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("paper_fee_profiles.id"), nullable=False)
    fee_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    initial_costs: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    accrued_holding_costs: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close_price: Mapped[Decimal | None] = mapped_column(MONEY)
    close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_source: Mapped[str | None] = mapped_column(String(80))
    close_reason: Mapped[str | None] = mapped_column(String(20))
    close_reason_text: Mapped[str | None] = mapped_column(Text)
    realized_gross_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_net_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    fee_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unpaid_loss: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    last_evaluated_candle_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_holding_period: Mapped[int] = mapped_column(Integer, nullable=False)


class PaperPositionEventORM(Base):
    __tablename__ = "paper_position_events"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_paper_position_event_dedupe"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("paper_accounts.id"), nullable=False)
    position_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("paper_positions.id"))
    decision_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("agent_decisions.id"))
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(180), nullable=False)


class PaperAccountLedgerORM(Base):
    __tablename__ = "paper_account_ledger"
    __table_args__ = (UniqueConstraint("event_key", name="uq_paper_ledger_event_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("paper_accounts.id"), nullable=False)
    position_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("paper_positions.id"))
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    margin_delta_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_key: Mapped[str] = mapped_column(String(180), nullable=False)


class PaperFundingRateORM(Base):
    __tablename__ = "paper_funding_rates"
    __table_args__ = (UniqueConstraint("instrument", "effective_at", name="uq_paper_funding_rate_time"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
