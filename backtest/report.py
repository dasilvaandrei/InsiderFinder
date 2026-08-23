import csv
import json
from collections import defaultdict
from datetime import datetime
from statistics import mean, median, stdev

from backtest import db

MIN_SAMPLE_SIZE = 20  # below this, a bucket's stats are too noisy to rate confidently

_ROLE_BUCKETS = [
    ("CEO", ("chief executive", "ceo")),
    ("CFO", ("chief financial", "cfo")),
    ("COO", ("chief operating", "coo")),
    ("President", ("president",)),
    ("Director", ("director",)),
]


def _role_bucket(source: str, role: str) -> str:
    if source == "Congress":
        return "Senator"
    lowered = (role or "").lower()
    for label, keywords in _ROLE_BUCKETS:
        if any(k in lowered for k in keywords):
            return label
    return "Other"


def _value_bucket(value: float) -> str:
    if value >= 1_000_000:
        return "$1M+"
    if value >= 250_000:
        return "$250k-1M"
    return "$100k-250k"


def _percentile(values: list, pct: float) -> float:
    """pct in [0, 1]. Simple linear-interpolation percentile, no numpy dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = pct * (len(ordered) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def build_report(start_date: str = None, end_date: str = None) -> list:
    """Aggregates backtest_results into (source, role_bucket, value_bucket, horizon_days)
    stats. `start_date`/`end_date` (ISO strings, inclusive/exclusive respectively) restrict
    which signals' results feed the calibration -- used for walk-forward validation, where
    the stats used to rate a trade must come only from signals disclosed before it."""
    conn = db.connect()
    query = """SELECT s.source, s.role, s.value, r.horizon_days, r.excess_return, r.stock_return
               FROM backtest_results r JOIN signals s ON s.id = r.signal_id"""
    conditions, params = [], []
    if start_date is not None:
        conditions.append("s.public_date >= ?")
        params.append(start_date)
    if end_date is not None:
        conditions.append("s.public_date < ?")
        params.append(end_date)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    excess_groups = defaultdict(list)
    stock_return_groups = defaultdict(list)
    for source, role, value, horizon_days, excess_return, stock_return in rows:
        key = (source, _role_bucket(source, role), _value_bucket(value or 0), horizon_days)
        excess_groups[key].append(excess_return)
        stock_return_groups[key].append(stock_return)

    summary = []
    for key, returns in sorted(excess_groups.items()):
        source, role_bucket, value_bucket, horizon_days = key
        n = len(returns)
        win_rate = sum(1 for r in returns if r > 0) / n
        avg = mean(returns)
        med = median(returns)
        risk_adjusted = avg / stdev(returns) if n > 1 and stdev(returns) else 0.0
        # 20th percentile of raw stock return: "in the worse ~20% of historical cases, the
        # stock had fallen to about this level by this horizon" — a stop-loss reference.
        stop_loss_pct = _percentile(stock_return_groups[key], 0.20)
        summary.append(
            {
                "source": source,
                "role_bucket": role_bucket,
                "value_bucket": value_bucket,
                "horizon_days": horizon_days,
                "count": n,
                "win_rate": win_rate,
                "avg_excess_return": avg,
                "median_excess_return": med,
                "risk_adjusted": risk_adjusted,
                "stop_loss_pct": stop_loss_pct,
            }
        )
    return summary


def print_report(summary: list) -> None:
    header = f"{'Source':9} {'Role':10} {'Value':11} {'Days':5} {'N':5} {'Win%':6} {'AvgExc%':8} {'MedExc%':8} {'Score':6}"
    print(header)
    print("-" * len(header))
    for r in summary:
        print(
            f"{r['source']:9} {r['role_bucket']:10} {r['value_bucket']:11} {r['horizon_days']:<5} "
            f"{r['count']:<5} {r['win_rate']*100:5.1f}% {r['avg_excess_return']*100:7.2f}% "
            f"{r['median_excess_return']*100:7.2f}% {r['risk_adjusted']:6.2f}"
        )


def save_csv(summary: list, path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0].keys()) if summary else [])
        writer.writeheader()
        writer.writerows(summary)


def save_ratings_json(summary: list, path: str) -> None:
    """Writes the (source, role_bucket, value_bucket, horizon_days) summary to a small,
    git-committed JSON file the live bot can load without needing the full signals DB."""
    payload = {
        "generated_at": datetime.now().isoformat(),
        "min_sample_size": MIN_SAMPLE_SIZE,
        "buckets": summary,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
