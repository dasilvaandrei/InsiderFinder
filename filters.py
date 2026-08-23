from collections import defaultdict

import config


def is_senior_role(role_text: str) -> bool:
    if not role_text:
        return False
    lowered = role_text.lower()
    return any(keyword in lowered for keyword in config.SENIOR_TITLE_KEYWORDS)


def is_qualifying_purchase_line(transaction_code: str, acquired_disposed: str, role_text: str) -> bool:
    """Structural filter only (transaction code / direction / seniority) — deliberately excludes
    the dollar threshold, which is applied after aggregate_purchase_events() sums an insider's
    same-day transaction line-items into their true total, so a large purchase split into many
    smaller execution-price tranches isn't wrongly excluded for looking small line-by-line."""
    is_open_market_purchase = (transaction_code or "").strip().upper() == "P"
    is_acquisition = (acquired_disposed or "A").strip().upper() != "D"
    return is_open_market_purchase and is_acquisition and is_senior_role(role_text)


def is_qualifying_purchase_type(transaction_type: str) -> bool:
    lowered = (transaction_type or "").lower()
    return "purchase" in lowered and "sale" not in lowered


def passes_value_threshold(value: float) -> bool:
    return value >= config.MIN_TRANSACTION_VALUE


def passes_sec_filter(transaction_code: str, acquired_disposed: str, value: float, role_text: str) -> bool:
    return is_qualifying_purchase_line(transaction_code, acquired_disposed, role_text) and passes_value_threshold(value)


def passes_congress_filter(transaction_type: str, value_low: float) -> bool:
    return is_qualifying_purchase_type(transaction_type) and passes_value_threshold(value_low)


def aggregate_purchase_events(records: list) -> list:
    """Collapses raw qualifying transaction line-items into one record per real-world purchase
    event, so a single large buy split into many execution-price tranches — or several different
    insiders each buying the same stock the same day — isn't traded as several independent,
    diversified bets when they'd all resolve to the identical simulated entry/exit price.

    Each input dict must have: ticker, person_name, public_date, value, transaction_date. Other
    keys (role, url, transaction_type, entity, ...) are carried through from a representative row.

    Two-level collapse:
      1. Group by (ticker, person_name, public_date) and sum `value` — the true total an insider
         bought that day, however many execution lines it was reported across.
      2. Group those per-insider totals by (ticker, public_date) and keep only the single largest
         one — backtesting this dataset showed no separate historical edge from multiple different
         insiders buying the same stock the same day (win rate ~50%, same as a single buyer), so
         there's no basis yet for scaling position size or keeping more than one representative
         trade per event. The `n_insiders` count is preserved for informational display.
    """
    by_person_day = defaultdict(list)
    for r in records:
        by_person_day[(r["ticker"], r["person_name"], r["public_date"])].append(r)

    person_totals = []
    for rows in by_person_day.values():
        total_value = sum(r["value"] for r in rows)
        rep = max(rows, key=lambda r: r["transaction_date"])
        person_totals.append({**rep, "value": total_value})

    by_ticker_day = defaultdict(list)
    for r in person_totals:
        by_ticker_day[(r["ticker"], r["public_date"])].append(r)

    collapsed = []
    for rows in by_ticker_day.values():
        best = max(rows, key=lambda r: r["value"])
        collapsed.append({**best, "n_insiders": len(rows)})
    return collapsed
