import argparse
import logging
import os
import time
from logging.handlers import RotatingFileHandler

import config
import notifier
import state
from sources import congress_trades, sec_insiders

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(config.LOG_DIR, "insiderfinderbot.log"),
            maxBytes=1_000_000,
            backupCount=3,
        ),
    ]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def run_once(lookback_days: int = None, dry_run: bool = False) -> int:
    config.validate(require_telegram=not dry_run)
    seen = state.SeenStore()

    alerts = sec_insiders.fetch_alerts(lookback_days) + congress_trades.fetch_alerts(lookback_days)
    logger.info("Fetched %d qualifying alert(s) before dedupe", len(alerts))

    sent = 0
    for alert in alerts:
        if not seen.is_new(alert.dedupe_key):
            continue

        if dry_run:
            print(notifier.format_message(alert))
            print("-" * 40)
        else:
            if not notifier.send_alert(alert):
                continue

        seen.mark_seen(alert.dedupe_key)
        sent += 1

    seen.prune_and_save()
    logger.info("Sent %d new alert(s)", sent)
    return sent


def run_loop() -> None:
    import schedule

    schedule.every().day.at("15:00").do(run_once)
    logger.info("Scheduler started. Waiting for 15:00 daily run (Ctrl+C to stop).")
    while True:
        schedule.run_pending()
        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description="InsiderFinderBot: SEC + Congress insider purchase alerts")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of sending to Telegram")
    parser.add_argument("--lookback-days", type=int, default=None, help="Override LOOKBACK_DAYS for this run")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run a persistent process using the built-in scheduler (daily at 15:00), instead of running once",
    )
    args = parser.parse_args()

    setup_logging()

    if args.loop:
        run_loop()
    else:
        run_once(lookback_days=args.lookback_days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
