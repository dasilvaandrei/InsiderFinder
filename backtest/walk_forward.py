import argparse
import logging
from datetime import date, timedelta

from backtest.db import connect
from backtest.replay_full_history import (
    _simulate_trade,
    print_portfolio_summary,
    run_portfolio_simulation,
)
from backtest.report import build_report
from models import InsiderAlert
from rating import _best_horizon_row, _bucket_rows

logger = logging.getLogger(__name__)


def _default_split_date(conn) -> str:
    """70% of the way through the available SEC signal date range, by calendar time (not
    signal count) -- leaves a genuinely later, unseen period to test against."""
    row = conn.execute("SELECT MIN(public_date), MAX(public_date) FROM signals WHERE source='SEC'").fetchone()
    min_date, max_date = date.fromisoformat(row[0]), date.fromisoformat(row[1])
    span_days = (max_date - min_date).days
    split = min_date + timedelta(days=int(span_days * 0.7))
    return split.isoformat()


def _get_strategy_rating(alert: InsiderAlert, train_buckets: list):
    """Same selection logic as rating.get_rating(), but against an explicit train-only
    bucket list instead of the live, full-dataset ratings.json -- so a test-period trade is
    never rated using stats that include its own, or any later, outcome."""
    rows = _bucket_rows(alert.source, alert.role, alert.value_low, buckets=train_buckets)
    if not rows:
        return None
    best = _best_horizon_row(rows)
    if best is None or best["win_rate"] < 0.5:
        return None
    stop_loss_pct = best.get("stop_loss_pct")
    if stop_loss_pct is not None:
        stop_loss_pct = min(stop_loss_pct, 0.0)
    return {"stop_loss_pct": stop_loss_pct, "hold_days": best["horizon_days"]}


def _load_test_signals(conn, split_date: str) -> list:
    return conn.execute(
        "SELECT id, source, person_name, role, ticker, transaction_date, public_date, value, url "
        "FROM signals WHERE public_date >= ? ORDER BY public_date",
        (split_date,),
    ).fetchall()


def walk_forward(split_date: str = None) -> None:
    conn = connect()
    if split_date is None:
        split_date = _default_split_date(conn)

    train_buckets = build_report(end_date=split_date)
    qualified = sum(1 for b in train_buckets if b["count"] >= 20)
    print(f"Train period: signals before {split_date}  |  {qualified} bucket/horizon rows meet the "
          f"20-sample minimum (these are the ONLY stats the test period is allowed to use)")
    print()

    test_signal_rows = _load_test_signals(conn, split_date)

    candidates = []
    skipped_no_rating = 0
    skipped_no_price = 0
    for sid, source, person_name, role, ticker, transaction_date, public_date, value, url in test_signal_rows:
        if not ticker or not public_date:
            continue
        alert = InsiderAlert(
            source=source,
            person_name=person_name or "",
            role=role or "",
            entity=ticker,
            transaction_type="Open Market Purchase",
            transaction_date=transaction_date,
            value_low=value or 0.0,
            value_display=f"${value:,.0f}" if value else "",
            url=url or "",
            ticker=ticker,
            public_date=public_date,
        )
        strategy = _get_strategy_rating(alert, train_buckets)
        if strategy is None or strategy["hold_days"] is None or strategy["stop_loss_pct"] is None:
            skipped_no_rating += 1
            continue

        trade = _simulate_trade(conn, alert, strategy)
        if trade is None:
            skipped_no_price += 1
            continue
        candidates.append(trade)

    result = run_portfolio_simulation(candidates)
    header = (
        f"Test period: signals from {split_date} onward (never used to calibrate the strategy) | "
        f"{len(test_signal_rows)} total signals | {skipped_no_rating} suppressed/no-rating | "
        f"{skipped_no_price} skipped (no price data) | {len(candidates)} qualifying trades | "
        f"{len(result['taken'])} actually taken ({result['skipped_no_cash']} skipped for lack of cash)"
    )
    print_portfolio_summary(result, header)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward out-of-sample test: calibrate the strategy's stop-loss/hold-days/suppression "
                     "stats on an early period only, then trade a later, held-out period the calibration never saw."
    )
    parser.add_argument(
        "--split-date", default=None,
        help="ISO date; signals before this calibrate the strategy, signals on/after this are the held-out "
             "test set. Defaults to 70%% through the available SEC signal date range.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    walk_forward(args.split_date)


if __name__ == "__main__":
    main()
