import bisect
import logging
from datetime import date

from backtest.db import connect
from models import InsiderAlert
from rating import _bucket_rows, _best_horizon_row

logger = logging.getLogger(__name__)

STARTING_CASH = 2000.0
POSITION_SIZE_PCT = 0.20  # 20% of current cash per trade, not a fixed dollar amount -- so a
# losing streak shrinks future bet sizes instead of hitting a fixed floor. Found via walk-
# forward testing: a fixed $400/$2,000 setup could be wiped out below its own minimum bet size
# by a bad crash (COVID, Feb-Mar 2020) and never trade again for the rest of the test window.
MAX_POSITION_SIZE = 3000.0  # hard dollar ceiling regardless of how much cash has accumulated --
# found by testing at $100,000 starting capital: uncapped 20%-of-cash sizing grew into $70,000+
# single positions in micro-cap insider-trade stocks by the later years (median position size
# $17,049 across the run), which real markets in thinly-traded names couldn't absorb without
# slippage far beyond the flat 0.15% assumption modeled here. $3,000 is a placeholder, not
# backed by real liquidity data for these specific tickers -- adjust if a better estimate exists.
_MIN_CASH_TO_TRADE = 1.0  # skip once remaining cash is too small to matter
TAKE_PROFIT_PCT = 0.065  # matches the live paper-trading take-profit rule
TRANSACTION_COST_PCT = 0.0015  # 0.15% per side (0.3% round trip) -- a conservative stand-in for
# spread/slippage on smaller-cap names, since no real fill data is available. No brokerage
# commission is modeled separately since most retail brokers are commission-free today.


class SpyPriceLookup:
    """Looks up SPY's close price as of (or the nearest trading day before) any given date --
    lets idle cash be tracked as SPY shares instead of a static dollar figure, so capital not
    currently in a stock pick still earns the market's return instead of 0% while it waits."""

    def __init__(self, conn):
        rows = conn.execute("SELECT date, close FROM prices WHERE ticker = 'SPY' ORDER BY date").fetchall()
        self.dates = [date.fromisoformat(d) for d, _ in rows]
        self.closes = [c for _, c in rows]

    def price(self, d: date) -> float:
        idx = max(bisect.bisect_right(self.dates, d) - 1, 0)
        return self.closes[idx]

    @property
    def last_date(self) -> date:
        return self.dates[-1]


def _load_signals(conn) -> list:
    cur = conn.execute(
        "SELECT id, source, person_name, role, ticker, transaction_date, public_date, value, url "
        "FROM signals ORDER BY public_date"
    )
    return cur.fetchall()


def _get_strategy_rating(alert: InsiderAlert):
    """Mirrors rating.get_rating()'s bucket/suppress/stop-loss/hold-days selection, but skips
    the live market-move note — that does a live yfinance fetch comparing 'today's' price move,
    which is meaningless (and would be 2000+ live calls) when replaying historical signals."""
    rows = _bucket_rows(alert.source, alert.role, alert.value_low)
    if not rows:
        return None
    best = _best_horizon_row(rows)
    if best is None or best["win_rate"] < 0.5:
        return None
    stop_loss_pct = best.get("stop_loss_pct")
    if stop_loss_pct is not None:
        stop_loss_pct = min(stop_loss_pct, 0.0)  # same clamp as rating.get_rating
    return {"stop_loss_pct": stop_loss_pct, "hold_days": best["horizon_days"]}


_MAX_STEP_MOVE_RATIO = 3.0  # a >3x (or <1/3x) move between consecutive checkpoints is almost
# certainly an unadjusted stock split or a bad data point, not a genuine price move -- e.g.
# VYNE's cached price jumped $0.76 -> $43.72 overnight (confirmed via a direct prices-table
# query), producing a fictitious "take profit" hundreds of times bigger than the 6.5% rule
# should ever allow. A trade whose price series contains one of these is excluded rather than
# traded on, since neither the entry nor any exit along the way can be trusted.
_MIN_ENTRY_PRICE = 1.0  # excludes penny stocks: e.g. NRIS traded at $0.06-$0.14 with open==close
# flat for days at a stretch (confirmed via a direct prices-table query) -- essentially no real
# trading activity, so the quoted price isn't something a $400 position could realistically be
# filled at. Standard practice in backtesting to exclude sub-$1 names for this reason.


def _price_checkpoints(conn, ticker: str) -> list:
    """Daily (open, close) checkpoints from the cached prices table. True intraday (1h) bars
    aren't available/cached for the full 2-year range (yfinance's hourly history only reaches
    back ~730 days), so this approximates the live 4x/day check-in with 2x/day (open, close)."""
    cur = conn.execute("SELECT date, open, close FROM prices WHERE ticker = ? ORDER BY date", (ticker,))
    rows = [(d, o, c) for d, o, c in cur.fetchall() if o is not None and c is not None]
    checkpoints = []
    for day_index, (d, o, c) in enumerate(rows):
        day = date.fromisoformat(d)
        checkpoints.append((day_index, day, "open", float(o)))
        checkpoints.append((day_index, day, "close", float(c)))
    return checkpoints


def _is_plausible_step(prev_price: float, price: float) -> bool:
    if price <= 0 or prev_price <= 0:
        return False
    return prev_price / _MAX_STEP_MOVE_RATIO <= price <= prev_price * _MAX_STEP_MOVE_RATIO


def _simulate_trade(conn, alert: InsiderAlert, strategy: dict):
    checkpoints = _price_checkpoints(conn, alert.ticker)
    if not checkpoints:
        return None

    public = date.fromisoformat(alert.public_date)
    entry_idx = next((i for i, cp in enumerate(checkpoints) if cp[1] > public and cp[2] == "open"), None)
    if entry_idx is None:
        return None
    entry_day_index, entry_date, _, entry_price = checkpoints[entry_idx]
    if entry_price < _MIN_ENTRY_PRICE:
        return None
    if entry_idx > 0 and not _is_plausible_step(checkpoints[entry_idx - 1][3], entry_price):
        return None  # entry itself lands right on a split/data discontinuity

    stop_loss_price = entry_price * (1 + strategy["stop_loss_pct"])
    take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT)
    hold_days = strategy["hold_days"]

    prev_price = entry_price
    for day_index, day, label, price in checkpoints[entry_idx:]:
        if day_index < entry_day_index or (day_index == entry_day_index and label == "open"):
            continue

        if not _is_plausible_step(prev_price, price):
            return None
        prev_price = price

        if price <= stop_loss_price:
            return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
                    "exit_date": day, "exit_price": price, "exit_reason": "stop_loss"}
        if price >= take_profit_price:
            return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
                    "exit_date": day, "exit_price": price, "exit_reason": "take_profit"}
        if day_index - entry_day_index >= hold_days:
            return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
                    "exit_date": day, "exit_price": price, "exit_reason": "hold_days_reached"}

    _, last_day, _, last_price = checkpoints[-1]
    return {"alert": alert, "entry_date": entry_date, "entry_price": entry_price,
            "exit_date": last_day, "exit_price": last_price, "exit_reason": "still_open"}


def run_portfolio_simulation(candidates: list, starting_cash: float = STARTING_CASH, spy: SpyPriceLookup = None) -> dict:
    """Chronological, capital-constrained trade selection: sorts candidates by entry date,
    sizes each trade at POSITION_SIZE_PCT of whatever cash is actually free at that point
    (releasing cash from positions that have already exited by then). Proportional sizing means
    a losing streak shrinks future bet sizes rather than hitting a fixed-dollar floor.

    If `spy` is given, idle cash is equitized: tracked as SPY shares rather than a static dollar
    figure, so capital sitting out between trades earns SPY's return instead of 0% -- otherwise
    a strategy whose median hold is ~1 day can lose to buy-and-hold SPY on cash-drag alone, even
    if most individual trades beat SPY (confirmed on this dataset: median 1-day holds meant most
    capital sat idle earning nothing, and the un-equitized portfolio trailed SPY in every window
    tested). Without `spy`, idle cash earns nothing, matching the original behavior.

    Shared by the full-history replay and the walk-forward out-of-sample test so both use
    identical portfolio mechanics."""
    candidates = sorted(candidates, key=lambda t: t["entry_date"])
    equitize = spy is not None and len(candidates) > 0

    if equitize:
        cash_shares = starting_cash / spy.price(candidates[0]["entry_date"])
    else:
        cash = starting_cash

    def _cash_value(as_of: date) -> float:
        return cash_shares * spy.price(as_of) if equitize else cash

    def _release_cash(amount: float, as_of: date) -> None:
        nonlocal cash, cash_shares
        if equitize:
            cash_shares += amount / spy.price(as_of)
        else:
            cash += amount

    pending_exits = []  # (exit_date, dollars_freed)
    taken = []
    skipped_no_cash = 0

    for t in candidates:
        still_pending = []
        for exit_date, amount in pending_exits:
            if exit_date <= t["entry_date"]:
                _release_cash(amount, exit_date)
            else:
                still_pending.append((exit_date, amount))
        pending_exits = still_pending

        current_cash = _cash_value(t["entry_date"])
        if current_cash < _MIN_CASH_TO_TRADE:
            skipped_no_cash += 1
            continue

        position_size = min(current_cash * POSITION_SIZE_PCT, MAX_POSITION_SIZE)
        if equitize:
            cash_shares -= position_size / spy.price(t["entry_date"])
        else:
            cash -= position_size
        # Entry/exit thresholds (stop-loss, take-profit, hold-days) are decided off the raw
        # market price in _simulate_trade(); the cost only affects the price actually paid/
        # received, i.e. how many shares this position size buys and how much the sale nets.
        effective_entry_price = t["entry_price"] * (1 + TRANSACTION_COST_PCT)
        effective_exit_price = t["exit_price"] * (1 - TRANSACTION_COST_PCT)
        shares = position_size / effective_entry_price
        exit_value = effective_exit_price * shares
        pending_exits.append((t["exit_date"], exit_value))
        taken.append({
            **t,
            "shares": shares,
            "position_size": position_size,
            "pnl": exit_value - position_size,
            "pnl_pct": exit_value / position_size - 1,
        })

    for exit_date, amount in pending_exits:
        _release_cash(amount, exit_date)

    final_value = _cash_value(spy.last_date) if equitize else cash
    total_pnl = final_value - starting_cash

    closed = [t for t in taken if t["exit_reason"] != "still_open"]
    open_ = [t for t in taken if t["exit_reason"] == "still_open"]
    stock_picking_pnl = sum(t["pnl"] for t in taken)

    reasons = {}
    for t in taken:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    return {
        "taken": taken,
        "closed": closed,
        "open": open_,
        "total_pnl": total_pnl,
        "stock_picking_pnl": stock_picking_pnl,
        "equitized_cash": equitize,
        "final_value": final_value,
        "starting_cash": starting_cash,
        "skipped_no_cash": skipped_no_cash,
        "reasons": reasons,
    }


def print_portfolio_summary(result: dict, header_line: str) -> None:
    print(header_line)
    print(f"Exit reasons: {result['reasons']}")
    print()
    print(f"{'Ticker':8} {'Role':22} {'Entry':11} {'Exit':11} {'Reason':16} {'P&L':>9} {'P&L%':>8}")
    for t in sorted(result["taken"], key=lambda x: x["entry_date"]):
        a = t["alert"]
        print(
            f"{a.ticker:8} {a.role[:22]:22} {t['entry_date'].isoformat():11} {t['exit_date'].isoformat():11} "
            f"{t['exit_reason']:16} {t['pnl']:>+8.2f} {t['pnl_pct']*100:>+7.1f}%"
        )
    print()
    print(f"Closed trades: {len(result['closed'])}  |  Still open (mark-to-market): {len(result['open'])}")
    if result.get("equitized_cash"):
        print(f"Stock-picking P&L only (excludes idle-cash SPY gains): ${result['stock_picking_pnl']:+,.2f}")
    print(f"Total P&L: ${result['total_pnl']:+,.2f}")
    print(
        f"Final value: ${result['final_value']:,.2f} (started ${result['starting_cash']:,.2f}, "
        f"{(result['final_value']/result['starting_cash']-1)*100:+.2f}%)"
    )


def replay(starting_cash: float = STARTING_CASH, equitize_cash: bool = True) -> None:
    conn = connect()
    signal_rows = _load_signals(conn)
    spy = SpyPriceLookup(conn) if equitize_cash else None

    candidates = []
    skipped_no_rating = 0
    skipped_no_price = 0
    for sid, source, person_name, role, ticker, transaction_date, public_date, value, url in signal_rows:
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
        strategy = _get_strategy_rating(alert)
        if strategy is None or strategy["hold_days"] is None or strategy["stop_loss_pct"] is None:
            skipped_no_rating += 1
            continue

        trade = _simulate_trade(conn, alert, strategy)
        if trade is None:
            skipped_no_price += 1
            continue
        candidates.append(trade)

    result = run_portfolio_simulation(candidates, starting_cash=starting_cash, spy=spy)
    header = (
        f"{len(signal_rows)} total signals | {skipped_no_rating} suppressed/no-rating | "
        f"{skipped_no_price} skipped (no price data) | {len(candidates)} qualifying trades | "
        f"{len(result['taken'])} actually taken ({result['skipped_no_cash']} skipped for lack of cash)"
    )
    print_portfolio_summary(result, header)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Replay the full cached signal history through the live strategy")
    parser.add_argument("--starting-cash", type=float, default=STARTING_CASH, help="Starting portfolio cash")
    parser.add_argument("--no-equitize-cash", action="store_true",
                         help="Let idle cash sit at 0%% instead of tracking it as SPY shares between trades")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    replay(starting_cash=args.starting_cash, equitize_cash=not args.no_equitize_cash)


if __name__ == "__main__":
    main()
