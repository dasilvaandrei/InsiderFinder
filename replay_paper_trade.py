import argparse
import logging
from datetime import date, timedelta

import yfinance as yf

import config
import rating
from sources import congress_trades, sec_insiders

logger = logging.getLogger(__name__)

STARTING_CASH = 2000.0
POSITION_SIZE = 400.0
TAKE_PROFIT_PCT = 0.065  # lock in gains around here rather than risk waiting for the full hold-days horizon

_price_cache = {}


def _price_series(ticker: str, start: date):
    """Hourly bars — yfinance keeps intraday history for ~2 years, comfortably covering
    the replay window (unlike 1-minute bars, which are only available for ~7 days back)."""
    if ticker not in _price_cache:
        try:
            _price_cache[ticker] = yf.Ticker(ticker).history(start=start.isoformat(), interval="1h")
        except Exception:
            logger.warning("Could not fetch intraday price history for %s", ticker)
            _price_cache[ticker] = None
    return _price_cache[ticker]


def _build_checkpoints(series) -> list:
    """Flattens intraday bars into (day_index, date, label, price) checkpoints — open,
    ~12pm, ~3pm, close — one set per trading day, in chronological order."""
    by_day = {}
    for ts in series.index:
        by_day.setdefault(ts.date(), []).append((ts, float(series.loc[ts, "Close"])))

    checkpoints = []
    for day_index, (day, bars) in enumerate(sorted(by_day.items())):
        bars.sort(key=lambda b: b[0])
        open_price = bars[0][1]
        close_price = bars[-1][1]
        noon_price = min(bars, key=lambda b: abs(b[0].hour + b[0].minute / 60 - 12))[1]
        three_price = min(bars, key=lambda b: abs(b[0].hour + b[0].minute / 60 - 15))[1]
        checkpoints.append((day_index, day, "open", open_price))
        checkpoints.append((day_index, day, "12pm", noon_price))
        checkpoints.append((day_index, day, "3pm", three_price))
        checkpoints.append((day_index, day, "close", close_price))
    return checkpoints


def _simulate_trade(alert, r) -> dict:
    """Computes the hypothetical entry/exit for one alert, checking stop-loss/take-profit/
    hold-days at 4 intraday checkpoints/day instead of just end-of-day. Ignores capital
    constraints (those are applied afterward, chronologically, in replay())."""
    public = date.fromisoformat(alert.public_date)
    series = _price_series(alert.ticker, public)
    if series is None or series.empty:
        return None

    checkpoints = _build_checkpoints(series)
    entry_cp = next((cp for cp in checkpoints if cp[1] > public and cp[2] == "open"), None)
    if entry_cp is None:
        return None
    entry_day_index, entry_date, _, entry_price = entry_cp
    if entry_price <= 0:
        return None

    stop_loss_price = entry_price * (1 + r.stop_loss_pct)
    take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT)

    for day_index, day, label, price in checkpoints:
        if day_index < entry_day_index or (day_index == entry_day_index and label == "open"):
            continue  # skip everything up to and including the entry checkpoint itself

        if price <= stop_loss_price:
            return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
                    "exit_date": day, "exit_price": price, "exit_reason": "stop_loss"}
        if price >= take_profit_price:
            return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
                    "exit_date": day, "exit_price": price, "exit_reason": "take_profit"}
        if day_index - entry_day_index >= r.hold_days:
            return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
                    "exit_date": day, "exit_price": price, "exit_reason": "hold_days_reached"}

    _, last_day, _, last_price = checkpoints[-1]
    return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
            "exit_date": last_day, "exit_price": last_price, "exit_reason": "still_open"}


def replay(lookback_days: int) -> None:
    config.validate(require_telegram=False)

    alerts = sec_insiders.fetch_alerts(lookback_days) + congress_trades.fetch_alerts(lookback_days)
    alerts = [a for a in alerts if a.ticker and a.public_date]

    candidates = []
    seen_keys = set()
    for alert in alerts:
        if alert.dedupe_key in seen_keys:
            continue
        seen_keys.add(alert.dedupe_key)

        r = rating.get_rating(alert)
        if r is None or r.suppress or r.hold_days is None or r.stop_loss_pct is None:
            continue

        trade = _simulate_trade(alert, r)
        if trade is not None:
            candidates.append(trade)

    candidates.sort(key=lambda t: t["entry_date"])

    cash = STARTING_CASH
    pending_exits = []  # (exit_date, dollars_freed)
    taken = []
    skipped_no_cash = 0

    for t in candidates:
        still_pending = []
        for exit_date, amount in pending_exits:
            if exit_date <= t["entry_date"]:
                cash += amount
            else:
                still_pending.append((exit_date, amount))
        pending_exits = still_pending

        if cash < POSITION_SIZE:
            skipped_no_cash += 1
            continue

        cash -= POSITION_SIZE
        shares = POSITION_SIZE / t["entry_price"]
        exit_value = t["exit_price"] * shares
        pending_exits.append((t["exit_date"], exit_value))
        taken.append({**t, "shares": shares, "pnl": exit_value - POSITION_SIZE, "pnl_pct": exit_value / POSITION_SIZE - 1})

    for exit_date, amount in pending_exits:
        cash += amount

    closed = [t for t in taken if t["exit_reason"] != "still_open"]
    open_ = [t for t in taken if t["exit_reason"] == "still_open"]
    total_pnl = sum(t["pnl"] for t in taken)
    final_value = STARTING_CASH + total_pnl

    print(f"Replayed {lookback_days} days: {len(candidates)} qualifying signals, {len(taken)} taken "
          f"({skipped_no_cash} skipped for lack of cash)")
    print(f"{'Ticker':8} {'Role':22} {'Entry':11} {'Exit':11} {'Reason':16} {'P&L':>9} {'P&L%':>8}")
    for t in sorted(taken, key=lambda x: x["entry_date"]):
        a = t["alert"]
        print(
            f"{a.ticker:8} {a.role[:22]:22} {t['entry_date'].isoformat():11} {t['exit_date'].isoformat():11} "
            f"{t['exit_reason']:16} {t['pnl']:>+8.2f} {t['pnl_pct']*100:>+7.1f}%"
        )
    print()
    print(f"Closed trades: {len(closed)}  |  Still open (mark-to-market): {len(open_)}")
    print(f"Total P&L: ${total_pnl:+,.2f}")
    print(f"Final value: ${final_value:,.2f} (started ${STARTING_CASH:,.2f}, {(final_value/STARTING_CASH-1)*100:+.2f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the paper-trading strategy over a recent historical window")
    parser.add_argument("--days", type=int, default=14, help="How many days back to replay (default 14)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    replay(args.days)


if __name__ == "__main__":
    main()
