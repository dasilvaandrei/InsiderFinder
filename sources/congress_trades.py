import io
import logging
import re
from datetime import date, timedelta
from typing import List, Optional

import pandas as pd
import requests

import config
import filters
from models import InsiderAlert

logger = logging.getLogger(__name__)

_BASE_URL = "https://efdsearch.senate.gov"
_HOME_URL = f"{_BASE_URL}/search/home/"
_SEARCH_URL = f"{_BASE_URL}/search/"
_DATA_URL = f"{_BASE_URL}/search/report/data/"
_PTR_REPORT_TYPE = 11

_HEADERS = {"User-Agent": "InsiderFinderBot/1.0 (personal use; official Senate eFD search)"}
_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
_LINK_RE = re.compile(r'href="([^"]+)"')
_AMOUNT_RE = re.compile(r"\$?([\d,]+)")


def _parse_amount_low(amount_text: str) -> float:
    """STOCK Act disclosures report a dollar range, e.g. '$100,001 - $250,000' or 'Over $50,000,000'."""
    numbers = [int(n.replace(",", "")) for n in _AMOUNT_RE.findall(amount_text or "")]
    return float(min(numbers)) if numbers else 0.0


def _open_session() -> requests.Session:
    """Accept the eFD disclaimer to establish a search-capable session, per the site's own flow."""
    session = requests.Session()
    session.headers.update(_HEADERS)

    home = session.get(_HOME_URL, timeout=30)
    home.raise_for_status()
    csrf_match = _CSRF_RE.search(home.text)
    if not csrf_match:
        raise RuntimeError("Could not find CSRF token on Senate eFD home page")

    session.post(
        _HOME_URL,
        data={"csrfmiddlewaretoken": csrf_match.group(1), "prohibition_agreement": "1"},
        headers={"Referer": _HOME_URL},
        timeout=30,
    )

    search_page = session.get(_SEARCH_URL, timeout=30)
    search_page.raise_for_status()
    csrf_match = _CSRF_RE.search(search_page.text)
    if not csrf_match:
        raise RuntimeError("Could not find CSRF token on Senate eFD search page")

    session.headers["Referer"] = _SEARCH_URL
    session.headers["X-CSRFToken"] = session.cookies.get("csrftoken", "")
    session.csrf_token = csrf_match.group(1)
    return session


def _search_ptr_filings(session: requests.Session, lookback_days: int) -> list:
    end = date.today()
    start = end - timedelta(days=lookback_days)
    payload = {
        "draw": "1",
        "start": "0",
        "length": "100",
        "report_types": f"[{_PTR_REPORT_TYPE}]",
        "filer_types": "[]",
        "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
        "submitted_end_date": end.strftime("%m/%d/%Y 23:59:59"),
        "candidate_state": "",
        "senator_state": "",
        "office_id": "",
        "first_name": "",
        "last_name": "",
        "csrfmiddlewaretoken": session.csrf_token,
    }
    response = session.post(_DATA_URL, data=payload, timeout=30)
    response.raise_for_status()
    return response.json().get("data", [])


def _parse_transactions(session: requests.Session, report_url: str) -> List[dict]:
    response = session.get(report_url, timeout=30)
    response.raise_for_status()
    try:
        tables = pd.read_html(io.StringIO(response.text))
    except ValueError:
        return []
    return tables[0].to_dict(orient="records") if tables else []


def fetch_alerts(lookback_days: Optional[int] = None) -> List[InsiderAlert]:
    """Scrape the official Senate eFD system for Periodic Transaction Reports (STOCK Act)."""
    lookback_days = config.LOOKBACK_DAYS if lookback_days is None else lookback_days

    try:
        session = _open_session()
        rows = _search_ptr_filings(session, lookback_days)
    except Exception:
        logger.exception("Failed to search Senate eFD Periodic Transaction Reports")
        return []

    alerts: List[InsiderAlert] = []
    for row in rows:
        try:
            first_name, last_name, _office, report_html, _filed_date = row
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

        for txn in transactions:
            transaction_type = str(txn.get("Type", ""))
            amount_text = str(txn.get("Amount", ""))
            value_low = _parse_amount_low(amount_text)

            if not filters.passes_congress_filter(transaction_type, value_low):
                continue

            alerts.append(
                InsiderAlert(
                    source="Congress",
                    person_name=person_name or "Unknown senator",
                    role="Senator",
                    entity=str(txn.get("Ticker") or txn.get("Asset Name", "") or ""),
                    transaction_type="Purchase",
                    transaction_date=str(txn.get("Transaction Date", "")),
                    value_low=value_low,
                    value_display=amount_text,
                    url=report_url,
                )
            )

    return alerts
