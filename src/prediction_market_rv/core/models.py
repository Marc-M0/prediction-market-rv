"""Canonical Market record shared by every module, plus small parsing helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone


@dataclass
class Market:
    venue: str
    market_id: str
    title: str
    close_time: datetime | None
    yes_bid: float | None   # 0..1 (None = no live order book)
    yes_ask: float | None   # 0..1
    url: str = ""
    group: str = ""         # parent event title (context only)
    subject: str = ""       # what YES refers to (team name / candidate) when known
    kind: str = "binary"    # "binary" or "game" (2-sided: team A vs team B)
    alt_time: datetime | None = None   # Kalshi expected_expiration_time (games close much later)
    fee_rate: float = 0.0   # Polymarket taker feeRate for this market (Kalshi uses its own formula)
    fee_cat: str = ""
    teams: tuple | None = None   # (team A, team B) for two-sided games
    series_ticker: str = ""   # Kalshi only: needed to build the exact event page URL
    event_ticker: str = ""
    event_id: str = ""        # groups the options of one multi-choice / ladder event (kind == "option")

    @property
    def no_ask(self) -> float | None:
        """Price to buy NO = 1 - best YES bid."""
        return None if self.yes_bid is None else round(1.0 - self.yes_bid, 4)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def to_prob(value) -> float | None:
    """Accepts None, 0.37, '0.37' (dollars) -> float in 0..1."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0.0 <= v <= 1.0 else None
