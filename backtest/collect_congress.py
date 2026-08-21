import logging
from datetime import date

import filters
from backtest import db
from sources.congress_trades import (
    _BASE_URL,
    _LINK_RE,
    _open_session,
    _parse_amount_low,
    _parse_transactions,
    normalize_filed_date,
    search_ptr_filings_between,
)

logger = logging.getLogger(__name__)


def collect(start_date: date, end_date: date) -> int:
    """Scrapes the official Senate eFD system for PTR filings in [start_date, end_date],
    filters to qualifying Purchase transactions, and inserts them into the signals DB."""
    conn = db.connect()
    inserted = 0

    try:
        session = _open_session()
        rows = search_ptr_filings_between(session, start_date, end_date)
    except Exception:
        logger.exception("Failed to search Senate eFD Periodic Transaction Reports")
        conn.close()
        return 0

    logger.info("Found %d Senate PTR filings between %s and %s", len(rows), start_date, end_date)

    for row in rows:
        try:
            first_name, last_name, _office, report_html, filed_date = row
        except (ValueError, TypeError):
            continue

        link_match = _LINK_RE.search(report_html or "")
        if not link_match:
            continue
        report_url = _BASE_URL + link_match.group(1)

        try:
            transactions = _parse_transactions(session, report_url)
        except Exception:
            logger.warning("Skipping unparseable Senate PTR report: %s", report_url)
            continue

        person_name = f"{first_name} {last_name}".strip()
        public_date = normalize_filed_date(filed_date)

        for txn in transactions:
            transaction_type = str(txn.get("Type", ""))
            amount_text = str(txn.get("Amount", ""))
            value_low = _parse_amount_low(amount_text)

            if not filters.passes_congress_filter(transaction_type, value_low):
                continue

            ticker = str(txn.get("Ticker") or "").strip().upper()
            if not ticker or ticker == "--":
                continue  # non-stock assets (funds, real estate, etc.) report ticker as "--"

            db.insert_signal(
                conn,
                source="Congress",
                person_name=person_name or "Unknown senator",
                role="Senator",
                ticker=ticker,
                transaction_date=str(txn.get("Transaction Date", "")),
                public_date=public_date,
                value=value_low,
                url=report_url,
            )
            inserted += 1

    conn.commit()
    conn.close()
    return inserted
