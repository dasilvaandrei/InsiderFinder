import config


def is_senior_role(role_text: str) -> bool:
    if not role_text:
        return False
    lowered = role_text.lower()
    return any(keyword in lowered for keyword in config.SENIOR_TITLE_KEYWORDS)


def passes_sec_filter(transaction_code: str, acquired_disposed: str, value: float, role_text: str) -> bool:
    is_open_market_purchase = (transaction_code or "").strip().upper() == "P"
    is_acquisition = (acquired_disposed or "A").strip().upper() != "D"
    return (
        is_open_market_purchase
        and is_acquisition
        and value >= config.MIN_TRANSACTION_VALUE
        and is_senior_role(role_text)
    )


def passes_congress_filter(transaction_type: str, value_low: float) -> bool:
    lowered = (transaction_type or "").lower()
    is_purchase = "purchase" in lowered
    is_sale = "sale" in lowered
    return is_purchase and not is_sale and value_low >= config.MIN_TRANSACTION_VALUE
