import argparse
from decimal import Decimal

from database.connection import SessionLocal
from models.paper import FeeRates, MarketType
from services.paper_trading import PaperTradingService


def okx_eea_taker_profile() -> FeeRates:
    """Requested PAPER assumption: 0.1% taker and fixed 4 bps spread."""
    return FeeRates(
        market_type=MarketType.SPOT_MARGIN, status="ESTIMATED",
        source="user_requested_okx_eea_taker_and_fixed_4bps_spread_2026-11-11",
        version=1,
        trade_entry_fee_rate=Decimal("0.001"),
        trade_exit_fee_rate=Decimal("0.001"),
        margin_opening_fee_rate=Decimal("0"),
        rollover_rate_per_4h=Decimal("0"),
        slippage_bps=Decimal("0"), spread_bps=Decimal("4"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotently seed the USD paper account")
    parser.add_argument("--estimated-demo-profile", action="store_true",
                        help="Add explicit fictional SPOT_MARGIN rates for simulation only")
    parser.add_argument("--okx-eea-taker-profile", action="store_true",
                        help="Use requested 0.1%% taker and fixed 4 bps PAPER spread, with other costs zero")
    for name in (
        "trade-entry-fee-rate", "trade-exit-fee-rate",
        "margin-opening-fee-rate", "rollover-rate-per-4h",
        "slippage-bps", "spread-bps",
    ):
        parser.add_argument(f"--{name}", type=Decimal)
    parser.add_argument("--fee-source", help="Source label for custom simulated rates")
    parser.add_argument("--fee-status", choices=["ESTIMATED", "OBSERVED"], default="ESTIMATED")
    args = parser.parse_args()
    custom_values = (
        args.trade_entry_fee_rate, args.trade_exit_fee_rate,
        args.margin_opening_fee_rate, args.rollover_rate_per_4h,
        args.slippage_bps, args.spread_bps,
    )
    if args.estimated_demo_profile and args.okx_eea_taker_profile:
        parser.error("Choose one built-in fee profile")
    if (args.estimated_demo_profile or args.okx_eea_taker_profile) and any(value is not None for value in custom_values):
        parser.error("Choose a built-in profile or complete custom rates")
    if (args.estimated_demo_profile or args.okx_eea_taker_profile) and args.fee_source:
        parser.error("Built-in profiles already have a source label")
    if args.fee_source and not any(value is not None for value in custom_values):
        parser.error("--fee-source requires all six custom rates")
    if any(value is not None for value in custom_values) and (
        any(value is None for value in custom_values) or not args.fee_source
    ):
        parser.error("Custom profile requires all six rates and --fee-source")
    with SessionLocal.begin() as session:
        service = PaperTradingService(session)
        account = service.seed_account()
        if args.estimated_demo_profile:
            rates = FeeRates(
                market_type=MarketType.SPOT_MARGIN, status="ESTIMATED",
                source="explicit_simulation_assumption_not_kraken_quote",
                version=1,
                trade_entry_fee_rate=Decimal("0.0026"),
                trade_exit_fee_rate=Decimal("0.0026"),
                margin_opening_fee_rate=Decimal("0.0002"),
                rollover_rate_per_4h=Decimal("0.0002"),
                slippage_bps=Decimal("5"), spread_bps=Decimal("10"),
            )
        elif args.okx_eea_taker_profile:
            rates = okx_eea_taker_profile()
        elif all(value is not None for value in custom_values):
            rates = FeeRates(
                market_type=MarketType.SPOT_MARGIN, status=args.fee_status,
                source=args.fee_source, version=1,
                trade_entry_fee_rate=args.trade_entry_fee_rate,
                trade_exit_fee_rate=args.trade_exit_fee_rate,
                margin_opening_fee_rate=args.margin_opening_fee_rate,
                rollover_rate_per_4h=args.rollover_rate_per_4h,
                slippage_bps=args.slippage_bps, spread_bps=args.spread_bps,
            )
        else:
            rates = None
        if rates is not None:
            profile = service.configure_fee_profile(rates)
            print(f"Symulacyjny profil opłat: wersja {profile.version}, status {profile.status}")
        print(f"Rachunek PAPER {account.id}: saldo początkowe {account.initial_balance} USD")
    with SessionLocal() as session:
        print(PaperTradingService(session).account_snapshot())


if __name__ == "__main__":
    main()
