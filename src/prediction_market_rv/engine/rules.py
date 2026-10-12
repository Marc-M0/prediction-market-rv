"""Hard constraints: reasons why two markets are NOT the same contract."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache

from prediction_market_rv.config import ENTITY_SOFT_MIN_SCORE, MAX_CLOSE_GAP_DAYS
from prediction_market_rv.core.models import Market
from prediction_market_rv.core.teams import same_matchup, subject_equiv
from prediction_market_rv.core.text import (
    _entity_extras,
    concepts,
    directions,
    months,
    normalize,
    numbers,
    qualifiers,
)


GAME_CONCEPTS = {
    "advance": ("advanc", "qualif", "progress"),
    "regulation": ("regulation", "full-time", "full time", "90 min", "90-min"),
    "pens": ("penalt", "shootout"),
    "overtime": ("extra time", "overtime"),
}


@lru_cache(maxsize=None)      # [SPEED]
def game_concepts(text: str) -> set[str]:
    t = normalize(text).lower()
    return {name for name, stems in GAME_CONCEPTS.items() if any(st in t for st in stems)}


def _times(m: Market) -> list[datetime]:
    return [t for t in (m.close_time, m.alt_time) if t]


def reject_reason(a: Market, b: Market, score: float | None = None) -> str | None:
    """Return why two markets are NOT the same contract, or None if compatible."""
    both_games = a.kind == "game" and b.kind == "game"
    na, nb = numbers(a.title), numbers(b.title)
    if na != nb:
        small = lambda s_: {n for n in s_ if not (1 <= n <= 31 or 2020 <= n <= 2100)}
        if small(na) != small(nb):
            return "numbers differ"
    if qualifiers(a.title) != qualifiers(b.title):
        return "different sub-market (set / map / game number)"
    da, db = directions(a.title), directions(b.title)
    if da and db and da != db:
        return "direction differs"
    ma, mb = months(a.title), months(b.title)
    if ma and mb and ma.isdisjoint(mb):
        return "month differs"
    if both_games:
        if not (a.teams and b.teams):
            return "teams not parsed"
        if not same_matchup(a.teams, b.teams):
            return "different matchup (an opponent differs)"
        if game_concepts(a.title) != game_concepts(b.title):
            return "different market type (advance / regulation / penalties)"
        if not subject_equiv(a.subject, b.subject):
            return "different team (YES refers to another side)"
    else:
        if concepts(a.title) != concepts(b.title):
            return "different concept (announce/ticket/lead-left/...)"
        ea, eb = _entity_extras(a.title, b.title)
        if ea and eb:
            return "different names"
        if (ea or eb) and (score is None or score < ENTITY_SOFT_MIN_SCORE):
            return "different names"
    ta, tb = _times(a), _times(b)
    if ta and tb:
        gap = min(abs((x - y).total_seconds()) for x in ta for y in tb) / 86400
        if gap > MAX_CLOSE_GAP_DAYS:
            return "close dates differ"
    return None


def soft_warnings(a: Market, b: Market) -> list[str]:
    """Pairs we accept but that deserve a human look."""
    w = []
    if not (a.kind == "game" and b.kind == "game"):
        ea, eb = _entity_extras(a.title, b.title)
        if ea or eb:
            w.append("WARN: names only partly match (" + ", ".join(ea + eb) + ")")
    return w


def compatible(a: Market, b: Market) -> bool:
    return reject_reason(a, b) is None
