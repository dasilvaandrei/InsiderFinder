import logging
import os
import zipfile
from datetime import date
from typing import List, Tuple

import pandas as pd
import requests

import filters
from backtest import db

logger = logging.getLogger(__name__)

_ZIP_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_HEADERS = {"User-Agent": "InsiderFinderBot backtest research (personal use)"}


def _quarters_between(start: date, end: date) -> List[str]:
    quarters = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        quarters.append(f"{y}q{q}")
        q += 1
        if q > 4:
            q = 1
            y += 1
    return quarters


class QuarterNotPublished(Exception):
    pass


def _download_quarter(quarter: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    zip_path = os.path.join(_CACHE_DIR, f"{quarter}_form345.zip")
    if not os.path.exists(zip_path):
        response = requests.get(_ZIP_URL.format(q=quarter), headers=_HEADERS, timeout=120)
        if response.status_code == 404:
            raise QuarterNotPublished(
                f"{quarter} isn't published yet (SEC posts each quarter's bulk data set "
                "after the quarter closes) — the live daily scan already covers this period."
            )
        response.raise_for_status()
        with open(zip_path, "wb") as f:
            f.write(response.content)
    return zip_path


def _load_quarter_frames(quarter: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    zip_path = _download_quarter(quarter)
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("SUBMISSION.tsv") as f:
            submissions = pd.read_csv(f, sep="\t", low_memory=False)
        with zf.open("REPORTINGOWNER.tsv") as f:
            owners = pd.read_csv(f, sep="\t", low_memory=False)
        with zf.open("NONDERIV_TRANS.tsv") as f:
            transactions = pd.read_csv(f, sep="\t", low_memory=False)
    return submissions, owners, transactions


def collect(start_date: date, end_date: date) -> int:
    """Downloads quarterly SEC bulk Form 3/4/5 data sets, filters to qualifying open-market
    purchases (Code P, senior role, value threshold), and inserts them into the signals DB."""
    conn = db.connect()
    inserted = 0

    for quarter in _quarters_between(start_date, end_date):
        try:
            submissions, owners, transactions = _load_quarter_frames(quarter)
        except QuarterNotPublished as e:
            logger.info(str(e))
            continue
        except Exception:
            logger.exception("Failed to load SEC bulk data set for %s", quarter)
            continue

        submissions = submissions[submissions["DOCUMENT_TYPE"].astype(str).str.strip().isin(["4", "4/A"])].copy()
        submissions["FILING_DATE"] = pd.to_datetime(submissions["FILING_DATE"], errors="coerce")
        submissions = submissions[
            (submissions["FILING_DATE"].dt.date >= start_date)
            & (submissions["FILING_DATE"].dt.date <= end_date)
        ]
        if submissions.empty:
            continue

        owners_by_accession = (
            owners.groupby("ACCESSION_NUMBER")
            .agg(
                RPTOWNERNAME=("RPTOWNERNAME", lambda s: " / ".join(sorted(set(str(x) for x in s if pd.notna(x))))),
                RPTOWNER_TITLE=("RPTOWNER_TITLE", lambda s: " / ".join(sorted(set(str(x) for x in s if pd.notna(x))))),
            )
            .reset_index()
        )

        purchases = transactions[transactions["TRANS_CODE"].astype(str).str.strip() == "P"]

        merged = purchases.merge(submissions, on="ACCESSION_NUMBER", how="inner")
        merged = merged.merge(owners_by_accession, on="ACCESSION_NUMBER", how="left")

        for row in merged.itertuples(index=False):
            role = getattr(row, "RPTOWNER_TITLE", "") or ""
            acquired_disposed = str(getattr(row, "TRANS_ACQUIRED_DISP_CD", "A") or "A")
            shares = getattr(row, "TRANS_SHARES", 0) or 0
            price = getattr(row, "TRANS_PRICEPERSHARE", 0) or 0
            try:
                value = float(shares) * float(price)
            except (TypeError, ValueError):
                value = 0.0

            if not filters.passes_sec_filter("P", acquired_disposed, value, role):
                continue

            ticker = str(getattr(row, "ISSUERTRADINGSYMBOL", "") or "").strip()
            if not ticker:
                continue

            trans_date = str(getattr(row, "TRANS_DATE", "") or "")
            filing_date_val = getattr(row, "FILING_DATE", None)
            filing_date = filing_date_val.date().isoformat() if pd.notna(filing_date_val) else ""
            cik = str(getattr(row, "ISSUERCIK", "")).lstrip("0") or "0"
            url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4"

            db.insert_signal(
                conn,
                source="SEC",
                person_name=getattr(row, "RPTOWNERNAME", "") or "",
                role=role,
                ticker=ticker,
                transaction_date=trans_date,
                public_date=filing_date,
                value=value,
                url=url,
            )
            inserted += 1

        conn.commit()
        logger.info("Processed %s: %d qualifying SEC signals so far", quarter, inserted)

    conn.close()
    return inserted
