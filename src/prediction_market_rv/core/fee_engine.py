"""Platform fees: Kalshi taker fee (rounded up to the cent) and Polymarket category fee."""
from __future__ import annotations

import math

from prediction_market_rv.config import KALSHI_FEE_COEF, ORDER_CONTRACTS, POLY_FEE_RATES


TAG_TO_FEE_CAT = {
    "geopolitics": "geopolitics", "world": "geopolitics", "world-events": "geopolitics",
    "sports": "sports", "crypto": "crypto", "politics": "politics", "elections": "politics",
    "finance": "finance", "business": "finance", "tech": "tech", "mentions": "mentions",
    "economy": "economics", "economics": "economics", "culture": "culture", "weather": "weather",
}


def poly_fee_info(ev: dict, m: dict, is_game: bool) -> tuple[float, str]:
    """(feeRate, category). Uses the market's feesEnabled flag when present, else event tags."""
    if m.get("feesEnabled") is False:
        return 0.0, "fee-free"
    if is_game:
        return POLY_FEE_RATES["sports"], "sports"
    slugs = [str(t.get("slug", "")).lower() for t in (ev.get("tags") or []) if isinstance(t, dict)]
    cats = {TAG_TO_FEE_CAT[x] for x in slugs if x in TAG_TO_FEE_CAT}
    for cat in ("geopolitics", "sports", "crypto", "politics", "finance", "tech",
                "mentions", "economics", "culture", "weather"):
        if cat in cats:
            return POLY_FEE_RATES[cat], cat
    return POLY_FEE_RATES["other"], "other(assumed)"      # unknown category -> conservative


def kalshi_fee(price: float, contracts: int = ORDER_CONTRACTS) -> float:
    """Kalshi taker fee PER CONTRACT: ceil_to_cent(0.07*C*P*(1-P)) / C."""
    if price <= 0 or price >= 1:
        return 0.0
    total = KALSHI_FEE_COEF * contracts * price * (1.0 - price)
    return math.ceil(round(total * 100, 6)) / 100.0 / contracts


def poly_fee(price: float, rate: float) -> float:
    """Polymarket taker fee PER SHARE = rate * p * (1-p)   (same for YES at p and NO at 1-p)."""
    return rate * price * (1.0 - price)
