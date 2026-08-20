import json
import os
from datetime import date, timedelta

import config

_RETENTION_DAYS = 90


def _load() -> dict:
    if not os.path.exists(config.SEEN_ALERTS_PATH):
        return {}
    try:
        with open(config.SEEN_ALERTS_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp_path = config.SEEN_ALERTS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, config.SEEN_ALERTS_PATH)


class SeenStore:
    def __init__(self):
        self._data = _load()

    def is_new(self, dedupe_key: str) -> bool:
        return dedupe_key not in self._data

    def mark_seen(self, dedupe_key: str) -> None:
        self._data[dedupe_key] = date.today().isoformat()

    def prune_and_save(self) -> None:
        cutoff = date.today() - timedelta(days=_RETENTION_DAYS)
        self._data = {
            key: seen_date
            for key, seen_date in self._data.items()
            if _safe_date(seen_date) >= cutoff
        }
        _save(self._data)


def _safe_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return date.today()
