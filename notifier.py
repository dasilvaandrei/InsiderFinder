import logging
from typing import Optional

import requests

import config
from models import InsiderAlert
from rating import Rating

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def format_message(alert: InsiderAlert, r: Optional[Rating]) -> str:
    icon = "🏛️" if alert.source == "Congress" else "🏢"
    lines = [
        f"{icon} *{alert.transaction_type}* — {alert.value_display}",
        f"*{alert.person_name}* ({alert.role})",
        f"Entity: {alert.entity}",
        f"Date: {alert.transaction_date}",
    ]
    if alert.url:
        lines.append(f"[Source filing]({alert.url})")

    if r is None:
        lines.append("⚪ No backtest history for this bucket yet")
    else:
        lines.append(
            f"{r.label} — {r.win_rate*100:.0f}% win rate, {r.avg_excess_return*100:+.1f}% avg "
            f"vs SPY over {r.horizon_days}d (n={r.count})"
        )
        if r.hold_days is not None:
            lines.append(f"Suggested hold: ~{r.hold_days} trading days")
        if r.stop_loss_pct is not None:
            lines.append(f"Suggested stop loss: {r.stop_loss_pct*100:.1f}% below entry")
        if r.move_note:
            lines.append(r.move_note)

    return "\n".join(lines)


def send_alert(alert: InsiderAlert, r: Optional[Rating]) -> bool:
    url = _API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": format_message(alert, r),
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
