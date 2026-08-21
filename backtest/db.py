import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    person_name TEXT,
    role TEXT,
    ticker TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    public_date TEXT NOT NULL,
    value REAL,
    url TEXT,
    UNIQUE(source, ticker, person_name, transaction_date, value)
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    close REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS backtest_results (
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    horizon_days INTEGER NOT NULL,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    stock_return REAL,
    benchmark_return REAL,
    excess_return REAL,
    PRIMARY KEY (signal_id, horizon_days)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def insert_signal(conn: sqlite3.Connection, source: str, person_name: str, role: str, ticker: str,
                   transaction_date: str, public_date: str, value: float, url: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO signals
           (source, person_name, role, ticker, transaction_date, public_date, value, url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, person_name, role, ticker, transaction_date, public_date, value, url),
    )
