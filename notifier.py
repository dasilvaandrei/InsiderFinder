import logging

import requests

import config
from models import InsiderAlert

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def format_message(alert: InsiderAlert) -> str:
    icon = "🏛️" if alert.source == "Congress" else "🏢"
    lines = [
        f"{icon} *{alert.transaction_type}* — {alert.value_display}",
        f"*{alert.person_name}* ({alert.role})",
        f"Entity: {alert.entity}",
        f"Date: {alert.transaction_date}",
    ]
    if alert.url:
        lines.append(f"[Source filing]({alert.url})")
    return "\n".join(lines)


def send_alert(alert: InsiderAlert) -> bool:
    url = _API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": format_message(alert),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("Failed to send Telegram alert for %s / %s", alert.person_name, alert.entity)
        return False
