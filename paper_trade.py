import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta

import requests
import yfinance as yf

import config
import rating
from sources import congress_trades, sec_insiders

logger = logging.getLogger(__name__)

PORTFOLIO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading", "portfolio.json")
STARTING_CASH = 2000.0
POSITION_SIZE_PCT = 0.20  # 20% of current cash per trade, not a fixed dollar amount -- so a
# losing streak shrinks future bet sizes instead of hitting a fixed floor (found via walk-
# forward backtesting: a fixed $400/$2,000 setup could be wiped out below its own minimum bet
# size by a bad crash and never trade again for the rest of the test window).
MAX_POSITION_SIZE = 3000.0  # hard dollar ceiling regardless of accumulated cash -- uncapped
# 20%-of-cash sizing grew into $70,000+ single positions in micro-cap stocks when tested at
# $100,000 starting capital, well beyond what thinly-traded names could realistically absorb.
_MIN_CASH_TO_TRADE = 1.0  # skip once remaining cash is too small to matter
TEST_LENGTH_DAYS = 7
TAKE_PROFIT_PCT = 0.065  # lock in gains around here rather than risk waiting for the full hold-days horizon
TRANSACTION_COST_PCT = 0.0015  # 0.15% per side (0.3% round trip) -- a conservative stand-in for
# spread/slippage on smaller-cap names, since no real fill data is available.

_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_telegram(text: str) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        response = requests.post(
            _TELEGRAM_API_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to send paper-trade Telegram message")


def _load_portfolio() -> dict:
    if not os.path.exists(PORTFOLIO_PATH):
        return {
            "starting_cash": STARTING_CASH,
            "position_size_pct": POSITION_SIZE_PCT,
            "started_at": date.today().isoformat(),
            "considered_keys": [],
            "open_positions": [],
            "closed_trades": [],
        }
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)


def _spy_price(portfolio: dict):
    """Live SPY price, used both to mark-to-market idle cash and to convert dollar amounts
    into/out of the equitized cash_shares balance. Falls back to the last price this script
    successfully saw if the live fetch fails, so a transient yfinance hiccup doesn't stall a
    check-in; returns None only if there's neither a fresh price nor any prior one to fall
    back on (only possible on this script's very first-ever run)."""
    price = _current_price("SPY")
    if price is not None and price > 0:
        portfolio["last_spy_price"] = price
        return price
    return portfolio.get("last_spy_price")


def _ensure_cash_shares(portfolio: dict, spy_price: float) -> None:
    """Initializes cash_shares on a brand-new portfolio, or migrates an older portfolio.json
    that still has a plain dollar "cash" field (from before cash was equitized into SPY) by
    converting its current dollar balance into the equivalent number of SPY shares at today's
    price -- a one-time conversion, not a retroactive rewrite of the cash the test already
    held while un-equitized."""
    if "cash_shares" in portfolio:
        return
    if "cash" in portfolio:
        portfolio["cash_shares"] = portfolio.pop("cash") / spy_price
    else:
        portfolio["cash_shares"] = portfolio["starting_cash"] / spy_price


def _save_portfolio(portfolio: dict) -> None:
    os.makedirs(os.path.dirname(PORTFOLIO_PATH), exist_ok=True)
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(portfolio, f, indent=2)


def _current_price(ticker: str):
    """Prefers the most recent intraday tick (today's 1-minute bars) so 4x/day check-ins
    reflect the price at that moment, not yesterday's close; falls back to daily close
    when intraday data isn't available (e.g., market closed, illiquid ticker)."""
    try:
        intraday = yf.Ticker(ticker).history(period="1d", interval="1m")
        if intraday is not None and not intraday.empty:
            return float(intraday["Close"].iloc[-1])
    except Exception:
        pass
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        logger.warning("Could not fetch current price for %s", ticker)
        return None


def _check_open_positions(portfolio: dict, spy_price: float) -> None:
    today = date.today()
    still_open = []
    for pos in portfolio["open_positions"]:
        price = _current_price(pos["ticker"])
        if price is None:
            still_open.append(pos)  # try again next check-in
            continue

        target_exit = date.fromisoformat(pos["target_exit_date"])
        take_profit_price = pos["entry_price"] * (1 + TAKE_PROFIT_PCT)
        exit_reason = None
        if price <= pos["stop_loss_price"]:
            exit_reason = "stop_loss"
        elif price >= take_profit_price:
            exit_reason = "take_profit"
        elif today >= target_exit:
            exit_reason = "hold_days_reached"

        if exit_reason is None:
            still_open.append(pos)
            continue

        effective_exit_price = price * (1 - TRANSACTION_COST_PCT)
        effective_entry_price = pos.get("effective_entry_price", pos["entry_price"])
        realized_pnl = (effective_exit_price - effective_entry_price) * pos["shares"]
        portfolio["cash_shares"] += (effective_exit_price * pos["shares"]) / spy_price
        portfolio["closed_trades"].append(
            {
                **pos,
                "exit_date": today.isoformat(),
                "exit_price": price,
                "exit_reason": exit_reason,
                "realized_pnl": realized_pnl,
                "realized_pnl_pct": (effective_exit_price / effective_entry_price - 1),
            }
        )
        pnl_pct = (effective_exit_price / effective_entry_price - 1) * 100
        logger.info("SOLD %s: %s, P&L %+.2f (%+.1f%%)", pos["ticker"], exit_reason, realized_pnl, pnl_pct)
        _send_telegram(
            f"🧪 *Paper trade SOLD* {pos['ticker']} — {exit_reason}\n"
            f"P&L: ${realized_pnl:+.2f} ({pnl_pct:+.1f}%)"
        )

    portfolio["open_positions"] = still_open


def _look_for_new_signals(portfolio: dict, spy_price: float) -> None:
    alerts = sec_insiders.fetch_alerts() + congress_trades.fetch_alerts()
    considered = set(portfolio["considered_keys"])

    for alert in alerts:
        if alert.dedupe_key in considered:
            continue
        considered.add(alert.dedupe_key)
        portfolio["considered_keys"].append(alert.dedupe_key)

        if not alert.ticker:
            continue

        r = rating.get_rating(alert)
        if r is None or r.suppress or r.hold_days is None or r.stop_loss_pct is None:
            continue

        cash_value = portfolio["cash_shares"] * spy_price
        if cash_value < _MIN_CASH_TO_TRADE:
            logger.info("Skipping %s: insufficient cash ($%.2f left)", alert.ticker, cash_value)
            continue

        price = _current_price(alert.ticker)
        if price is None or price <= 0:
            continue

        position_size = min(cash_value * portfolio.get("position_size_pct", POSITION_SIZE_PCT), MAX_POSITION_SIZE)
        effective_entry_price = price * (1 + TRANSACTION_COST_PCT)
        shares = position_size / effective_entry_price
        portfolio["cash_shares"] -= position_size / spy_price
        portfolio["open_positions"].append(
            {
                "dedupe_key": alert.dedupe_key,
                "ticker": alert.ticker,
                "entity": alert.entity,
                "person_name": alert.person_name,
                "role": alert.role,
                "entry_date": date.today().isoformat(),
                "entry_price": price,  # raw market price -- stop-loss/take-profit track this
                "effective_entry_price": effective_entry_price,  # cost-adjusted, for real P&L
                "shares": shares,
                "dollar_amount": position_size,
                "stop_loss_price": price * (1 + r.stop_loss_pct),
                "target_exit_date": (date.today() + timedelta(days=r.hold_days)).isoformat(),
                "hold_days": r.hold_days,
            }
        )
        logger.info("BOUGHT %s: $%.2f (%.3f shares @ $%.2f)", alert.ticker, position_size, shares, price)
        _send_telegram(
            f"🧪 *Paper trade BUY* {alert.ticker} — ${position_size:.2f} @ ${price:.2f}\n"
            f"{alert.role} · {alert.entity}\n"
            f"Stop loss: ${price * (1 + r.stop_loss_pct):.2f}  |  Take profit: +{TAKE_PROFIT_PCT*100:.1f}%  |  "
            f"Target hold: ~{r.hold_days}d"
        )


def _print_summary(portfolio: dict, spy_price: float, send_to_telegram: bool) -> None:
    open_value = 0.0
    for pos in portfolio["open_positions"]:
        price = _current_price(pos["ticker"]) or pos["entry_price"]
        open_value += price * pos["shares"]

    cash_value = portfolio["cash_shares"] * spy_price
    total_value = cash_value + open_value
    realized_pnl = sum(t["realized_pnl"] for t in portfolio["closed_trades"])
    started_at = date.fromisoformat(portfolio["started_at"])
    days_elapsed = (date.today() - started_at).days
    return_pct = (total_value / portfolio["starting_cash"] - 1) * 100

    lines = [
        f"Day {days_elapsed} of {TEST_LENGTH_DAYS} (started {portfolio['started_at']})",
        f"Cash (equitized in SPY @ ${spy_price:.2f}): ${cash_value:,.2f}  |  Open positions: {len(portfolio['open_positions'])}  |  Closed trades: {len(portfolio['closed_trades'])}",
        f"Realized P&L: ${realized_pnl:+,.2f}",
        f"Portfolio value: ${total_value:,.2f} (started ${portfolio['starting_cash']:,.2f}, {return_pct:+.2f}%)",
    ]
    if days_elapsed >= TEST_LENGTH_DAYS:
        lines.append("\n*** 7-day test window complete ***")

    for line in lines:
        print(line)

    if send_to_telegram:
        _send_telegram("🧪 *Paper trade daily summary*\n" + "\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Check-in for the paper-trading strategy test")
    parser.add_argument(
        "--label", default="close", choices=["open", "noon", "3pm", "close"],
        help="Which of the day's 4 check-ins this is; the daily Telegram summary only sends at 'close' "
             "(stop-loss/take-profit/hold-days checks and buy/sell alerts still run at every check-in)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.validate(require_telegram=False)

    portfolio = _load_portfolio()

    spy_price = _spy_price(portfolio)
    if spy_price is None:
        logger.error("Could not determine a SPY price (live fetch failed, no prior price cached); skipping this check-in")
        return
    _ensure_cash_shares(portfolio, spy_price)

    _check_open_positions(portfolio, spy_price)

    days_elapsed = (date.today() - date.fromisoformat(portfolio["started_at"])).days
    if days_elapsed < TEST_LENGTH_DAYS:
        _look_for_new_signals(portfolio, spy_price)
    else:
        logger.info("7-day test window elapsed; no new positions will be opened (existing ones still close out normally).")

    _save_portfolio(portfolio)
    _print_summary(portfolio, spy_price, send_to_telegram=(args.label == "close"))


if __name__ == "__main__":
    main()
