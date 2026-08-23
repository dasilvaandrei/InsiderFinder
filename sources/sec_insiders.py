import logging
from datetime import date, timedelta
from typing import List, Optional

import config
import filters
from models import InsiderAlert

logger = logging.getLogger(__name__)

_identity_set = False


def _ensure_identity() -> None:
    global _identity_set
    if _identity_set:
        return
    from edgar import set_identity

    set_identity(config.EDGAR_IDENTITY)
    _identity_set = True


def _owner_role(owner) -> str:
    title = getattr(owner, "officer_title", None)
    if title:
        return str(title)
    if getattr(owner, "is_director", False):
        return "Director"
    if getattr(owner, "is_officer", False):
        return "Officer"
    if getattr(owner, "is_ten_pct_owner", False):
        return "10% Owner"
    position = getattr(owner, "position", None)
    return str(position) if position else ""


def _issuer_label(form4) -> str:
    issuer = getattr(form4, "issuer", None)
    if issuer is None:
        return ""
    name = getattr(issuer, "name", None)
    ticker = getattr(issuer, "ticker", None)
    if name and ticker:
        return f"{name} ({ticker})"
    return str(name or ticker or issuer)


def _list_current_form4_filings():
    from edgar import get_current_filings

    return get_current_filings(form="4", page_size=None)


def _list_historical_form4_filings(lookback_days: int):
    from edgar import get_filings

    end = date.today()
    start = end - timedelta(days=lookback_days)
    years = sorted({start.year, end.year})
    quarters = sorted({(start.month - 1) // 3 + 1, (end.month - 1) // 3 + 1})
    filing_date = f"{start.isoformat()}:{end.isoformat()}"
    return get_filings(year=years, quarter=quarters, form="4", filing_date=filing_date)


def fetch_alerts(lookback_days: Optional[int] = None) -> List[InsiderAlert]:
    """Scan SEC Form 4 open-market purchases (Code P) and return qualifying alerts.

    Uses the real-time current-filings feed for the normal ~1-day lookback (today's
    filings lag by a business day in the indexed search), falling back to the slower
    indexed historical search for wider windows (mainly useful for testing).
    """
    _ensure_identity()

    lookback_days = config.LOOKBACK_DAYS if lookback_days is None else lookback_days

    try:
        if lookback_days <= 2:
            filings = _list_current_form4_filings()
        else:
            logger.warning(
                "lookback_days=%d exceeds the real-time window; falling back to the "
                "indexed historical search, which is much slower for wide ranges.",
                lookback_days,
            )
            filings = _list_historical_form4_filings(lookback_days)
    except Exception:
        logger.exception("Failed to list SEC Form 4 filings")
        return []

    raw_records = []
    for filing in filings:
        try:
            form4 = filing.obj()
        except Exception:
            logger.warning("Skipping filing %s: could not parse Form 4", getattr(filing, "accession_no", "?"))
            continue

        purchases = getattr(form4, "common_stock_purchases", None)
        if purchases is None or len(purchases) == 0:
            continue

        owners = getattr(getattr(form4, "reporting_owners", None), "owners", None) or []
        role = " / ".join(filter(None, (_owner_role(o) for o in owners))) or str(
            getattr(form4, "position", "") or ""
        )
        person_name = getattr(form4, "insider_name", None) or " / ".join(
            filter(None, (getattr(o, "name", "") for o in owners))
        )
        issuer_label = _issuer_label(form4)
        ticker = str(getattr(getattr(form4, "issuer", None), "ticker", "") or "")
        public_date = str(getattr(filing, "filing_date", "") or "")
        url = getattr(filing, "homepage_url", None) or getattr(filing, "filing_url", "") or ""

        for row in purchases.to_dict(orient="records"):
            code = str(row.get("Code", ""))
            acquired_disposed = str(row.get("AcquiredDisposed", "A"))
            shares = row.get("Shares") or 0
            price = row.get("Price") or 0
            try:
                value = float(shares) * float(price)
            except (TypeError, ValueError):
                value = 0.0

            if not filters.is_qualifying_purchase_line(code, acquired_disposed, role):
                continue

            raw_records.append(
                {
                    "ticker": ticker,
                    "person_name": person_name or "Unknown insider",
                    "role": role or "Insider",
                    "entity": issuer_label,
                    "value": value,
                    "transaction_date": str(row.get("Date", "")),
                    "public_date": public_date,
                    "url": url,
                }
            )

    alerts: List[InsiderAlert] = []
    for event in filters.aggregate_purchase_events(raw_records):
        if not filters.passes_value_threshold(event["value"]):
            continue

        value_display = f"${event['value']:,.0f}"
        if event["n_insiders"] > 1:
            value_display += f" (largest of {event['n_insiders']} insiders buying today)"

        alerts.append(
            InsiderAlert(
                source="SEC",
                person_name=event["person_name"],
                role=event["role"],
                entity=event["entity"],
                transaction_type="Open Market Purchase",
                transaction_date=event["transaction_date"],
                value_low=event["value"],
                value_display=value_display,
                url=event["url"],
                ticker=event["ticker"],
                public_date=event["public_date"],
            )
        )

    return alerts
