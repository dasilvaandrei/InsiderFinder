import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

import yfinance as yf

from backtest.report import MIN_SAMPLE_SIZE, _role_bucket, _value_bucket
from models import InsiderAlert

logger = logging.getLogger(__name__)

RATINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratings.json")
EXTENDED_MOVE_MULTIPLIER = 1.5  # actual move beyond this multiple of the historical average is flagged

_cache = None


@dataclass
class Rating:
    label: str
    win_rate: float
    avg_excess_return: float
    count: int
    horizon_days: int
    suppress: bool
    stop_loss_pct: Optional[float] = None
    hold_days: Optional[int] = None
    move_note: Optional[str] = None


def _load_buckets() -> list:
    global _cache
    if _cache is None:
        if not os.path.exists(RATINGS_PATH):
            _cache = []
        else:
            with open(RATINGS_PATH) as f:
                _cache = json.load(f).get("buckets", [])
    return _cache


def _bucket_rows(source: str, role: str, value: float, buckets: Optional[list] = None) -> list:
    """`buckets` defaults to the live, globally-cached ratings.json contents; callers doing
    walk-forward validation can pass an alternate (e.g. train-period-only) bucket list instead."""
    role_bucket = _role_bucket(source, role)
    value_bucket = _value_bucket(value or 0)
    source_buckets = buckets if buckets is not None else _load_buckets()
    return [
        row
        for row in source_buckets
        if row["source"] == source and row["role_bucket"] == role_bucket and row["value_bucket"] == value_bucket
    ]


def _best_horizon_row(rows: list) -> Optional[dict]:
    """The horizon with the best risk-adjusted score, among horizons with enough samples."""
    qualified = [r for r in rows if r["count"] >= MIN_SAMPLE_SIZE]
    if not qualified:
        return None
    return max(qualified, key=lambda r: r["risk_adjusted"])


def _closest_horizon_row(rows: list, target_days: int) -> Optional[dict]:
    qualified = [r for r in rows if r["count"] >= MIN_SAMPLE_SIZE]
    if not qualified:
        return None
    return min(qualified, key=lambda r: abs(r["horizon_days"] - target_days))


def _classify(win_rate: float, avg_excess_return: float) -> str:
    if win_rate >= 0.55 and avg_excess_return > 0.01:
        return "🟢 Historically favorable"
    return "🟡 Mixed historical signal"


def _market_move_note(alert: InsiderAlert, rows: list) -> Optional[tuple]:
    """Compares how far the stock has already moved since disclosure against the
    historically typical move at this point, so an already-extended move can be flagged."""
    if not alert.ticker or not alert.public_date:
        return None
    try:
        public = date.fromisoformat(alert.public_date)
    except ValueError:
        return None

    days_elapsed = (date.today() - public).days
    if days_elapsed < 1:
        return None  # nothing to compare yet

    expected_row = _closest_horizon_row(rows, days_elapsed)
    if expected_row is None:
        return None

    try:
        stock_hist = yf.Ticker(alert.ticker).history(start=public.isoformat())
        spy_hist = yf.Ticker("SPY").history(start=public.isoformat())
    except Exception:
        logger.warning("Could not fetch live price move for %s", alert.ticker)
        return None

    if stock_hist is None or spy_hist is None or len(stock_hist) < 2 or len(spy_hist) < 2:
        return None

    try:
        stock_entry, stock_now = float(stock_hist["Open"].iloc[1]), float(stock_hist["Close"].iloc[-1])
        spy_entry, spy_now = float(spy_hist["Open"].iloc[1]), float(spy_hist["Close"].iloc[-1])
        excess_so_far = (stock_now / stock_entry - 1) - (spy_now / spy_entry - 1)
    except (ZeroDivisionError, ValueError, IndexError):
        return None

    expected_excess = expected_row["avg_excess_return"]
    note = f"Moved {excess_so_far*100:+.1f}% vs SPY since disclosure ({days_elapsed}d so far)"

    if expected_excess > 0 and excess_so_far > EXTENDED_MOVE_MULTIPLIER * expected_excess:
        return (
            f"⚠️ {note} — already well beyond the historical {expected_excess*100:+.1f}% typical move, may be priced in",
            excess_so_far,
        )

    return note, excess_so_far


def get_rating(alert: InsiderAlert) -> Optional[Rating]:
    """Looks up the historical backtest stats for the bucket this live alert falls into,
    picks the best-performing holding horizon, and adds a stop-loss / market-move read.
    Returns None if ratings.json has no data at all for this bucket (still sent, just unrated)."""
    rows = _bucket_rows(alert.source, alert.role, alert.value_low)
    if not rows:
        return None

    best = _best_horizon_row(rows)
    if best is None:
        # some rows exist but none has enough samples at any horizon yet
        fallback = rows[0]
        return Rating(
            "⚪ Not enough history yet",
            fallback["win_rate"],
            fallback["avg_excess_return"],
            fallback["count"],
            fallback["horizon_days"],
            suppress=False,
        )

    if best["win_rate"] < 0.5:
        return Rating(
            "🔴 Historically underperforms SPY — suppressed",
            best["win_rate"],
            best["avg_excess_return"],
            best["count"],
            best["horizon_days"],
            suppress=True,
        )

    label = _classify(best["win_rate"], best["avg_excess_return"])
    move_result = _market_move_note(alert, rows)
    move_note = None
    if move_result is not None:
        move_note, _excess_so_far = move_result
        if move_note.startswith("⚠️"):
            label = "🟡 Mixed historical signal (extended move)"

    stop_loss_pct = best.get("stop_loss_pct")
    if stop_loss_pct is not None:
        # The 20th percentile of a strong bucket's returns can itself be positive — clamp so
        # the stop loss is always below entry, never above it.
        stop_loss_pct = min(stop_loss_pct, 0.0)

    return Rating(
        label,
        best["win_rate"],
        best["avg_excess_return"],
        best["count"],
        best["horizon_days"],
        suppress=False,
        stop_loss_pct=stop_loss_pct,
        hold_days=best["horizon_days"],
        move_note=move_note,
    )
