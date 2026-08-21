import argparse
import logging
import os
from datetime import date, datetime

import config
from backtest import collect_congress, collect_sec, evaluate, prices, report

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest insider/Congress purchase signals against actual returns")
    parser.add_argument("--start", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date, YYYY-MM-DD")
    parser.add_argument("--skip-collect", action="store_true", help="Skip signal collection, reuse existing DB")
    parser.add_argument("--skip-prices", action="store_true", help="Skip price fetching, reuse cached prices")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    if not args.skip_collect:
        config.validate(require_telegram=False)
        sec_count = collect_sec.collect(start_date, end_date)
        logger.info("Collected %d SEC signals", sec_count)
        congress_count = collect_congress.collect(start_date, end_date)
        logger.info("Collected %d Congress signals", congress_count)

    if not args.skip_prices:
        prices.fetch()
        logger.info("Price cache updated")

    written = evaluate.evaluate()
    logger.info("Wrote %d new backtest results", written)

    summary = report.build_report()
    report.print_report(summary)

    os.makedirs(os.path.join("backtest", "reports"), exist_ok=True)
    csv_path = os.path.join("backtest", "reports", f"{datetime.now():%Y%m%d_%H%M%S}.csv")
    report.save_csv(summary, csv_path)
    logger.info("Saved report to %s", csv_path)

    report.save_ratings_json(summary, "ratings.json")
    logger.info("Updated ratings.json for the live bot")


if __name__ == "__main__":
    main()
