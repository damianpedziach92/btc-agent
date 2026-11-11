import argparse
import json

from agents.market_analyst import INSTRUCTIONS_HASH
from agents.market_tools import MarketTools
from database.connection import SessionLocal
from models.market import Instrument
from repositories.agent_memory_repository import AgentMemoryRepository
from services.market_analysis import run_market_analysis


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the BTC/USD market analyst")
    parser.add_argument("--check-tools", action="store_true", help="Test read-only market tools without an OpenAI key")
    parser.add_argument("--force", action="store_true", help="Allow an older 15m candle; show the saved report if already analyzed")
    parser.add_argument("--preview", action="store_true", help="Run the current instructions again without saving a report")
    args = parser.parse_args()

    if args.check_tools:
        tools = MarketTools()
        for name, parameters in (
            ("get_paper_account", {}),
            ("get_open_positions", {}),
            ("get_price", {"instrument": "BTC/USD"}),
            ("get_indicators", {"instrument": "BTC/USD", "timeframe": "15m", "limit": 3}),
            ("get_candles", {"instrument": "BTC/USD", "timeframe": "15m", "limit": 3}),
        ):
            print(name, tools.call(name, json.dumps(parameters)))
        return

    try:
        decision = run_market_analysis(force=args.force, preview=args.preview)
    except RuntimeError as exc:
        parser.exit(1, f"{exc}\n")
    if decision is not None:
        if args.preview:
            print(f"WYNIK TESTOWY — instrukcja {INSTRUCTIONS_HASH}; decyzja nie została zapisana w bazie\n")
        print(decision.model_dump_json(indent=2))
        return

    with SessionLocal() as session:
        latest = AgentMemoryRepository(session).get_latest(Instrument.BTC_USD, 1)
    if latest:
        print("Brak nowej analizy; ostatnia zapisana decyzja poniżej. Aby przetestować zmienione instrukcje, użyj --preview.\n")
        print(json.dumps({
            "decision": latest[0].decision,
            "open_proposal": latest[0].open_proposal,
            "position_actions": latest[0].position_actions,
            "text": latest[0].text,
            "model": latest[0].model,
            "created_at": latest[0].created_at.isoformat(),
            "paper_status": latest[0].paper_status,
        }, ensure_ascii=False, indent=2))
    else:
        print("Brak nowej zamkniętej świecy 15m do analizy")


if __name__ == "__main__":
    main()
