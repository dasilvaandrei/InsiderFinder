import logging
from datetime import date, timedelta

import yfinance as yf

from backtest import db

logger = logging.getLogger(__name__)

BENCHMARK_TICKER = "SPY"


def _unique_tickers(conn) -> list:
    rows = conn.execute("SELECT DISTINCT ticker FROM signals").fetchall()
    return sorted({r[0] for r in rows if r[0]})


def fetch(buffer_days: int = 60) -> None:
    """Downloads daily Open/Close for every ticker referenced in `signals` (plus the
    benchmark), one ticker at a time, and caches into the `prices` table."""
    conn = db.connect()
    tickers = _unique_tickers(conn)
    if BENCHMARK_TICKER not in tickers:
        tickers.append(BENCHMARK_TICKER)

    date_range = conn.execute("SELECT MIN(public_date), MAX(public_date) FROM signals").fetchone()
    min_date, max_date = date_range
    if not min_date:
        conn.close()
        return

    start = date.fromisoformat(min_date)
    end = date.fromisoformat(max_date) + timedelta(days=buffer_days)  # room for the longest holding horizon

    for i, ticker in enumerate(tickers, start=1):
        try:
            hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat())
        except Exception:
            logger.warning("Failed to fetch prices for %s", ticker)
            continue

        if hist is None or hist.empty:
            continue

        rows = [
            (ticker, idx.date().isoformat(), float(row["Open"]), float(row["Close"]))
            for idx, row in hist.iterrows()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO prices (ticker, date, open, close) VALUES (?, ?, ?, ?)",
            rows,
        )
        if i % 25 == 0:
            conn.commit()
            logger.info("Fetched prices for %d/%d tickers", i, len(tickers))

    conn.commit()
    conn.close()
