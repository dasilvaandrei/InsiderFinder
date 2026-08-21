# InsiderFinder

Tracks large open-market stock purchases by corporate insiders (SEC Form 4) and
US Senators (STOCK Act Periodic Transaction Reports), and sends Telegram alerts for
purchases over a configurable dollar threshold (default $100,000).

## How it works

- **`sources/sec_insiders.py`** — uses [edgartools](https://github.com/dgunning/edgartools)
  to scan SEC EDGAR's real-time filing feed for Form 4 filings, keeping only transaction
  code `P` (open-market purchase) — code `A` (grants) and `F` (tax withholding) are excluded
  by construction — from officers (CEO/CFO/COO/President) or directors.
- **`sources/congress_trades.py`** — scrapes the official Senate eFD system
  (`efdsearch.senate.gov`) for newly filed Periodic Transaction Reports, keeping only
  `Purchase` transactions. (House Clerk disclosures are PDF-only with no structured
  export, so House coverage isn't included yet.)
- **`filters.py`** — shared $100,000+ threshold and seniority/purchase-type logic.
- **`notifier.py`** — sends one Telegram message per qualifying alert. Telegram's Bot
  API is free — no cost, no message fees, no meaningful rate limits for personal use.
- **`state.py`** — keeps `state/seen_alerts.json` so the same filing/disclosure never
  triggers a duplicate alert on a later run.
- **`main.py`** — orchestrates a single run, or a persistent loop with a built-in scheduler.

Senate disclosures are filtered by *filing date* (when the trade became public), not
trade date — STOCK Act filings can lag the actual trade by up to 45 days, so this is
what makes the alert timely.

## Step 1 — Create your Telegram bot (free)

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the
   prompts. It gives you a **bot token** (looks like `123456789:AA...`).
2. Send your new bot any message (e.g. "hi") so it knows who you are.
3. Get your **chat id**: message [@userinfobot](https://t.me/userinfobot) and it replies
   with your numeric id, or visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `message.chat.id` from the JSON.

You now have `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Step 2 — Run it locally first

```bash
git clone https://github.com/dasilvaandrei/InsiderFinder.git
cd InsiderFinder
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `EDGAR_IDENTITY` — SEC EDGAR requires every requester to self-identify (`"Name email@example.com"`).
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — from Step 1.
- `MIN_TRANSACTION_VALUE` / `LOOKBACK_DAYS` — tune if needed; defaults match the $100k / 1-day requirements.

Test it:

```bash
python main.py --dry-run --lookback-days 2
```

Prints formatted alerts to the console instead of sending to Telegram. Drop
`--dry-run` once you're ready to actually send to Telegram. A full scan can take
several minutes — it's checking every qualifying SEC filing and Senate PTR
individually, which is normal for a once-a-day job.

## Step 3 — Deploy for free via GitHub Actions (no server needed)

This runs the bot automatically on GitHub's own infrastructure, on a schedule —
nothing to host or keep running yourself.

1. Push this repo to GitHub if you haven't (it's already wired to
   `github.com/dasilvaandrei/InsiderFinder`):
   ```bash
   git add .
   git commit -m "Add InsiderFinder bot"
   git push
   ```
2. On GitHub: your repo → **Settings → Secrets and variables → Actions → New repository
   secret**. Add three secrets:
   - `EDGAR_IDENTITY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. The workflow file `.github/workflows/daily-run.yml` is already included. It runs
   daily at 15:00 US/Eastern (`cron: "0 19 * * *"` = 19:00 UTC, which is 3:00 PM EDT).
   **If you're not in US/Eastern, edit that cron line** — convert your local 3:00 PM to
   UTC and put it there. Note DST: US/Eastern shifts to UTC-5 in winter, so the line
   would need to become `"0 20 * * *"` for EST months if you want it pinned to real
   local time year-round.
4. On GitHub: repo → **Actions** tab → you should see "Daily insider alert scan". Click
   it → **Run workflow** to trigger it manually right now and confirm it works, without
   waiting for the schedule.
5. That's it — GitHub Actions will run it daily going forward. Check the Actions tab
   for run history/logs if an alert doesn't arrive when expected.

GitHub Actions is free for this use case (public repos: unlimited minutes; private
repos: 2,000 free minutes/month, and this job uses only a few minutes per run).

## Alternative — Cron on your own machine/server

```cron
0 15 * * * cd /path/to/InsiderFinder && /path/to/InsiderFinder/.venv/bin/python main.py >> logs/cron.log 2>&1
```

Runs daily at 3:00 PM in the machine's local timezone. Use `0 15 * * 1-5` instead for
weekdays only.

## Alternative — built-in scheduler (e.g. Windows without cron)

```bash
python main.py --loop
```

Keeps a process running in the foreground and fires a run every day at 15:00 local
time via the `schedule` library. Use a process manager (`pm2`, `systemd`, Task
Scheduler, `screen`/`tmux`) to keep it alive across reboots/logouts.

## Backtesting and historical ratings

`run_backtest.py` simulates buying at each historical signal to see whether the
strategy actually works, without look-ahead bias: every signal is anchored to the
date it became *publicly* known (SEC filing date / Senate PTR filed date), never the
trade date, and the simulated entry is always the next trading day's open after that.

```bash
python run_backtest.py --start 2024-08-20 --end 2026-08-19
```

This downloads SEC's official quarterly bulk Form 3/4/5 data sets and scrapes Senate
PTR filings for the range, prices each signal against SPY at several holding periods
(1 to 30 trading days) using `yfinance`, and prints a table of win rate / average
excess return per (role, dollar size, holding period) segment. Results are cached in
`backtest/signals.db` (gitignored — regenerate locally, don't commit it) so re-runs
are fast; `--skip-collect`/`--skip-prices` reuse what's already cached.

Each run also writes `ratings.json` — a small, git-committed summary of those same
segment stats. The live bot (`notifier.py`, via `rating.py`) reads this file and, for
every new alert:

- **Picks the best-performing holding horizon** for that (role, dollar size) segment
  (highest risk-adjusted score among horizons with ≥20 historical samples), and
  suggests it as the hold time.
- **Suggests a stop loss** — the 20th percentile of historical stock returns at that
  horizon ("in the worse ~20% of past cases, the stock had fallen to about this level
  by now").
- **Checks how far the stock has already moved since disclosure** (live `yfinance`
  price vs. entry) against the historically typical move at that many days out, and
  flags it ("⚠️ ... may be priced in") if it's already run further than ~1.5x typical.
- **Suppresses the alert entirely** if that segment's best-scoring horizon has a
  historical win rate below 50% (i.e., historically worse than just holding SPY) —
  you won't see a Telegram message for it at all, only a log line.

Segments with fewer than 20 historical samples are marked "not enough history yet"
(sent, not suppressed — there just isn't enough evidence either way). Re-run
`run_backtest.py` periodically (and commit the updated `ratings.json`) to keep this
current — it's a static snapshot, not live-updating.

## Paper trading (forward test)

`paper_trade.py` runs the live strategy forward with virtual money instead of real
trades — starts with $2,000, buys a fixed $400 per qualifying (non-suppressed) signal
up to 5 concurrent positions, and exits each position at whichever comes first: its
suggested stop loss or its suggested hold-days horizon. It's a genuine forward test,
not another backtest — GitHub Actions doesn't run continuously, so
`.github/workflows/paper-trade.yml` triggers it once a day (same cadence as the live
alert bot); each run checks current prices against open positions' exit conditions,
then looks for new signals. State persists in `paper_trading/portfolio.json` (via
GitHub's cache, gitignored — not committed). After 7 days it stops opening new
positions (existing ones still close out normally) and flags the test as complete in
its summary output.

Run it locally any time to check status: `python paper_trade.py`.

## Notes

- edgartools handles SEC's fair-access rate limits internally; no manual throttling needed.
- The Senate scrape uses the real `efdsearch.senate.gov` search flow (accept terms →
  search → parse each report's transaction table) — it's the official source, but it's
  a real website, not a stable API, so it can break if the Senate changes their site.
- This tool surfaces public disclosure data only. It is not financial advice.
