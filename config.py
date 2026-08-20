import os

from dotenv import load_dotenv

load_dotenv()

EDGAR_IDENTITY = os.environ.get("EDGAR_IDENTITY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

MIN_TRANSACTION_VALUE = float(os.environ.get("MIN_TRANSACTION_VALUE", "100000"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))

SENIOR_TITLE_KEYWORDS = (
    "chief executive",
    "ceo",
    "chief financial",
    "cfo",
    "chief operating",
    "coo",
    "president",
    "director",
)

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
SEEN_ALERTS_PATH = os.path.join(STATE_DIR, "seen_alerts.json")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def validate(require_telegram: bool = True) -> None:
    required = [("EDGAR_IDENTITY", EDGAR_IDENTITY)]
    if require_telegram:
        required += [("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)]

    missing = [name for name, value in required if not value]
    if missing:
        raise RuntimeError(
            f"Missing required .env values: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
