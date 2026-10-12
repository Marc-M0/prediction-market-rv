"""Arbitrage maths: both hedges at the ask, fees included, plus the warning flags."""
from __future__ import annotations

from dataclasses import replace

from prediction_market_rv.config import DUST_PRICE, MAX_SPREAD, SUSPICIOUS_EDGE
from prediction_market_rv.core.fee_engine import kalshi_fee, poly_fee
from prediction_market_rv.core.models import Market


def invert_market(m: Market) -> Market:
    """Swap YES<->NO so a 'will NOT happen' market lines up with the positive phrasing."""
    return replace(
        m,
        yes_bid=None if m.yes_ask is None else round(1.0 - m.yes_ask, 4),
        yes_ask=None if m.yes_bid is None else round(1.0 - m.yes_bid, 4),
    )


def best_arb(p: Market, k: Market) -> dict:
    """
    Two hedges, each pays exactly $1 at resolution. Both legs are bought at the ASK (taker),
    so BOTH venues' taker fees apply:
      A) YES on Polymarket + NO on Kalshi
      B) YES on Kalshi    + NO on Polymarket
    net edge = 1 - cost - fees   (per $1 of payout, i.e. per contract pair)
    """
    legs = []
    if p.yes_ask is not None and k.no_ask is not None:
        pf, kf = poly_fee(p.yes_ask, p.fee_rate), kalshi_fee(k.no_ask)
        legs.append(("YES Polymarket + NO Kalshi", p.yes_ask, k.no_ask, p.yes_ask + k.no_ask, pf, kf))
    if k.yes_ask is not None and p.no_ask is not None:
        pf, kf = poly_fee(p.no_ask, p.fee_rate), kalshi_fee(k.yes_ask)
        legs.append(("YES Kalshi + NO Polymarket", k.yes_ask, p.no_ask, k.yes_ask + p.no_ask, pf, kf))
    if not legs:
        return {"strategy": "n/a (no live prices)", "leg1": None, "leg2": None,
                "cost": float("nan"), "gross_edge": float("nan"), "net_edge": float("-inf"),
                "poly_fee": 0.0, "kalshi_fee": 0.0, "fees": 0.0}
    name, leg1, leg2, cost, pf, kf = max(legs, key=lambda l: 1.0 - l[3] - l[4] - l[5])
    return {"strategy": name, "leg1": leg1, "leg2": leg2, "cost": cost,
            "gross_edge": 1.0 - cost, "net_edge": 1.0 - cost - pf - kf,
            "poly_fee": pf, "kalshi_fee": kf, "fees": pf + kf}


def flags_for(p: Market, k: Market, arb: dict, inverted: bool, warns: list[str] | None = None) -> list[str]:
    f = list(warns or [])
    if inverted:
        f.append("INVERTED")
    for m, tag in ((p, "poly"), (k, "kalshi")):
        if m.yes_bid is None or m.yes_ask is None:
            f.append(f"{tag}:no-book")
        elif m.yes_ask - m.yes_bid > MAX_SPREAD:
            f.append(f"{tag}:wide-spread")
    if arb["net_edge"] > SUSPICIOUS_EDGE:
        f.append("CHECK-RULES")
    # [GUARD] a leg quoted under 1c (or over 99c) is a dust / stale quote (e.g. Polymarket YES $0.001 vs Kalshi $0.96)
    if arb["leg1"] is not None and (min(arb["leg1"], arb["leg2"]) < DUST_PRICE
                                    or max(arb["leg1"], arb["leg2"]) > 1 - DUST_PRICE):
        f.append("dust-leg")
    return f
