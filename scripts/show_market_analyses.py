from database.connection import SessionLocal
from models.market import Instrument
from repositories.agent_memory_repository import AgentMemoryRepository
from repositories.market_analysis_repository import MarketAnalysisRepository


def main() -> None:
    with SessionLocal() as session:
        decisions = AgentMemoryRepository(session).get_latest(Instrument.BTC_USD, 10)
        reports = MarketAnalysisRepository(session).get_latest(Instrument.BTC_USD)

    if not reports and not decisions:
        print("Brak zapisanych analiz rynku")
        return

    for item in decisions:
        print(f"\nBTC/USD | świeca 15m {item.candle_timestamp.isoformat()}")
        print(f"Utworzono: {item.created_at.isoformat()} | model: {item.model}")
        print(f"{item.decision}: {item.text}")

    for report in reports:
        print(f"\nBTC/USD | świeca 15m {report.candle_timestamp.isoformat()}")
        print(f"Utworzono: {report.created_at.isoformat()} | model: {report.model}")
        print(report.analysis)


if __name__ == "__main__":
    main()
