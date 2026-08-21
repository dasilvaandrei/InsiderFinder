import logging
import sqlite3
from typing import Dict, List, Tuple

from backtest import db
from backtest.prices import BENCHMARK_TICKER

logger = logging.getLogger(__name__)

HORIZONS = (1, 2, 3, 5, 7, 10, 15, 20, 25, 30)


def _price_series(conn: sqlite3.Connection, ticker: str) -> List[Tuple[str, float, float]]:
    return conn.execute(
        "SELECT date, open, close FROM prices WHERE ticker = ? ORDER BY date", (ticker,)
    ).fetchall()


def _price_lookup(conn: sqlite3.Connection, ticker: str) -> Dict[str, Tuple[float, float]]:
    rows = conn.execute("SELECT date, open, close FROM prices WHERE ticker = ?", (ticker,)).fetchall()
    return {d: (o, c) for d, o, c in rows}


def evaluate() -> int:
    """For each signal, finds the entry day (first trading day strictly after public_date)
    and computes stock/benchmark/excess returns at each holding horizon. No-look-ahead by
    construction: entry never precedes the day the signal became public."""
    conn = db.connect()
    signals = conn.execute("SELECT id, ticker, public_date FROM signals").fetchall()

    price_cache: Dict[str, List[Tuple[str, float, float]]] = {}
    benchmark_lookup = _price_lookup(conn, BENCHMARK_TICKER)

    written = 0
    for signal_id, ticker, public_date in signals:
        if ticker not in price_cache:
            price_cache[ticker] = _price_series(conn, ticker)
        series = price_cache[ticker]
        if not series:
            continue

        entry_idx = next((i for i, (d, _, _) in enumerate(series) if d > public_date), None)
        if entry_idx is None:
            continue
        entry_date, entry_open, _ = series[entry_idx]
        if not entry_open:
            continue

        for horizon in HORIZONS:
            existing = conn.execute(
                "SELECT 1 FROM backtest_results WHERE signal_id = ? AND horizon_days = ?",
                (signal_id, horizon),
            ).fetchone()
            if existing:
                continue

            exit_idx = entry_idx + horizon
            if exit_idx >= len(series):
                continue
            exit_date, _, exit_close = series[exit_idx]
            if not exit_close:
                continue

            stock_return = exit_close / entry_open - 1

            bench_entry = benchmark_lookup.get(entry_date)
            bench_exit = benchmark_lookup.get(exit_date)
            if not bench_entry or not bench_exit or not bench_entry[0]:
                continue
            benchmark_return = bench_exit[1] / bench_entry[0] - 1
            excess_return = stock_return - benchmark_return

            conn.execute(
                """INSERT OR REPLACE INTO backtest_results
                   (signal_id, horizon_days, entry_date, entry_price, exit_date, exit_price,
                    stock_return, benchmark_return, excess_return)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal_id, horizon, entry_date, entry_open, exit_date, exit_close,
                 stock_return, benchmark_return, excess_return),
            )
            written += 1

    conn.commit()
    conn.close()
    return written
