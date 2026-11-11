"""Import explicitly sourced signed perpetual funding rates for later simulations."""

import argparse
import csv
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from database.connection import SessionLocal
from database.models.paper import PaperFundingRateORM
from services.paper_trading import require_utc


def main() -> None:
    parser = argparse.ArgumentParser(description="Import historical perpetual funding CSV")
    parser.add_argument("csv_file", help="CSV columns: instrument,effective_at,rate,source")
    args = parser.parse_args()
    rows = []
    with open(args.csv_file, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            at = datetime.fromisoformat(row["effective_at"].replace("Z", "+00:00"))
            require_utc(at)
            if not row["source"].strip():
                raise ValueError("Each funding rate needs a source")
            rows.append({
                "id": uuid4(), "instrument": row["instrument"],
                "effective_at": at, "rate": Decimal(row["rate"]),
                "source": row["source"].strip(),
            })
    with SessionLocal.begin() as session:
        for row in rows:
            session.execute(insert(PaperFundingRateORM).values(row).on_conflict_do_nothing(
                constraint="uq_paper_funding_rate_time"
            ))
    print(f"Przetworzono {len(rows)} jawnie źródłowanych stawek funding")


if __name__ == "__main__":
    main()
