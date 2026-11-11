
import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from scripts.import_candles import main as import_candles


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


def collect_market_data():
    logger.info("Rozpoczynam pobieranie BTC/USD")

    try:
        import_candles()

        from services.paper_orchestration import process_new_paper_candles, process_pending_decisions
        from services.telegram_notifications import process_pending_notifications
        from services.review_notifications import process_pending_review_notifications
        process_new_paper_candles()
        process_pending_decisions()
        process_pending_notifications()
        process_pending_review_notifications()

        logger.info("Pobieranie zakończone")

    except Exception:
        logger.exception("Błąd pobierania danych")


def analyze_market():
    logger.info("Rozpoczynam analizę BTC/USD")
    try:
        from services.market_analysis import run_market_analysis

        report = run_market_analysis()
        if report is None:
            logger.info("Brak nowej świecy 15m do analizy")
        else:
            logger.info("Analiza BTC/USD:\n%s", report.model_dump_json())
    except Exception:
        logger.exception("Błąd analizy rynku")


def review_decisions():
    logger.info("Rozpoczynam dwugodzinny audyt decyzji BTC/USD")
    try:
        from services.decision_review import run_decision_review
        from services.review_notifications import process_pending_review_notifications

        result = run_decision_review()
        logger.info("Audyt decyzji: status=%s, okno=%s, liczba decyzji=%s, błędy=%s",
                    result["status"], result["window_end"], result.get("decision_count"),
                    len(result.get("findings", [])))
        process_pending_review_notifications()
    except Exception:
        logger.exception("Błąd audytu decyzji")


def main():
    load_dotenv()
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        collect_market_data,
        trigger=CronTrigger(
            minute="*",
            second=5,
            timezone="UTC"
        ),
        id="collect_btc_usd",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    if os.getenv("OPENAI_API_KEY"):
        scheduler.add_job(
            analyze_market,
            trigger=CronTrigger(minute="*/15", second=20, timezone="UTC"),
            id="analyze_btc_usd",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        logger.info("Analiza rynku włączona co 15 minut")
        scheduler.add_job(
            review_decisions,
            trigger=CronTrigger(minute="2/5", second=20, timezone="UTC"),
            id="review_btc_usd_decisions",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
        logger.info("Audyt decyzji włączony dla każdego okna 2h; próba od HH:02:20 UTC, ponawiana co 5 minut")
    else:
        logger.info("Analiza rynku wyłączona: brak OPENAI_API_KEY")

    logger.info("Scheduler uruchomiony")

    # Pierwszy import wykonujemy od razu
    collect_market_data()
    # Następnie działa harmonogram
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Zatrzymano scheduler")


if __name__ == "__main__":
    main()
