import hashlib
from dataclasses import dataclass


@dataclass
class InsiderAlert:
    source: str          # "SEC" or "Congress"
    person_name: str
    role: str             # e.g. "CEO", "Director", "Senator", "Representative"
    entity: str            # company name/ticker, or traded ticker for Congress
    transaction_type: str  # e.g. "Open Market Purchase"
    transaction_date: str  # ISO date string, when the trade actually happened
    value_low: float        # dollar value (SEC) or low end of disclosed range (Congress)
    value_display: str      # human-readable amount for the alert text
    url: str                # link to the filing/disclosure
    ticker: str = ""         # bare ticker symbol, for price lookups
    public_date: str = ""    # ISO date string, when the trade became publicly known (filing/filed date)

    @property
    def dedupe_key(self) -> str:
        raw = "|".join(
            [
                self.source,
                self.person_name,
                self.entity,
                self.transaction_date,
                self.value_display,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
