"""Multi-choice lists and ladders: match the events first, then option <-> same option."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import re

from rapidfuzz import fuzz
from rapidfuzz import process

from prediction_market_rv.config import (
    EVENT_CANDIDATES,
    EVENT_MIN_SCORE,
    MAX_CLOSE_GAP_DAYS,
    MIN_OPTION_COVERAGE,
    NAME_MAX_GAP_DAYS,
)
from prediction_market_rv.core.models import Market
from prediction_market_rv.core.teams import subject_equiv
from prediction_market_rv.core.text import (
    MONTHS,
    MONTH_ABBR,
    _tokens,
    clean_words,
    concepts,
    directions,
    months,
    normalize,
    numbers,
    qualifiers,
)
from prediction_market_rv.engine.rules import _times
from prediction_market_rv.engine import contract_guard as cg


EVENT_TYPE_STEMS = {
    "win": ("win", "champion"), "reach": ("reach", "advance", "qualif"), "final": ("final",),
    "semi": ("semi",), "scorer": ("scorer", "golden"), "mvp": ("mvp",), "relegate": ("relegat",),
    "promote": ("promot",), "group": ("group",), "first": ("first",), "next": ("next",),
    "out": ("resign", "leave", "fired", "removed", "impeach"),
}


@lru_cache(maxsize=None)      # [SPEED]
def event_types(text: str) -> set[str]:
    """What KIND of question an event asks (win / reach the final / top scorer ...) - must agree."""
    toks = _tokens(text)
    return {name for name, stems in EVENT_TYPE_STEMS.items()
            if any(t.startswith(st) for t in toks for st in stems)}

def _big(nums: set) -> set:
    return {n for n in nums if not (1 <= n <= 31 or 2020 <= n <= 2100)}     # drop day-of-month / years


def event_reject(a: str, b: str) -> str | None:
    """Do these two EVENT titles ask the same question? ('2026 World Cup winner' vs 'reach the final' -> no)."""
    if _big(numbers(a)) != _big(numbers(b)):
        return "numbers differ"
    ya, yb = set(re.findall(r"\b20\d\d\b", a)), set(re.findall(r"\b20\d\d\b", b))
    if ya and yb and ya != yb:
        return "year differs"
    if concepts(a) != concepts(b):
        return "different concept"
    if event_types(a) != event_types(b):
        return "different kind of question"
    if qualifiers(a) != qualifiers(b):
        return "different sub-market"
    return None


# [GUARD] words that are part of a ladder label's wording ("Before Jan 1", "Below 8"), not of a NAME
_RUNG_WORDS = (MONTHS | MONTH_ABBR | {
    "before", "after", "by", "above", "below", "over", "under", "between", "than", "least", "most", "more",
    "less", "and", "the", "from", "through", "only", "fewer", "higher", "lower", "exactly"})


@lru_cache(maxsize=None)      # [SPEED]
def _rung_name_words(label: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{3,}", normalize(label).lower()) if w not in _RUNG_WORDS]


def option_check(p: Market, k: Market):
    """(reason | None, date gap in days). Same option? names must be equivalent, ladder rungs must have the
    same threshold / date, and the full titles must pass the usual hard checks."""
    sp, sk = p.subject or "", k.subject or ""
    is_ladder = bool(re.search(r"\d", sp) or re.search(r"\d", sk))        # ladder rung: $100,000 / a date / "<8"
    if is_ladder:
        # [GUARD] compare the WHOLE canonical rung (operator + value, small numbers included):
        # '<8' == 'Below 8', '12+' == '12 or more', '8' != '9'. The old _big() dropped every number 1..31,
        # so all rungs of "How many House seats..." looked identical -> "ambiguous rung" -> nothing matched.
        if cg.rung_key(sp) != cg.rung_key(sk):
            return "different threshold", 0.0
        wp, wk = _rung_name_words(sp), _rung_name_words(sk)
        if len(wp) >= 2 and len(wk) >= 2 and not subject_equiv(sp, sk):        # 'Claude 4 Opus' vs 'Claude 4 Sonnet'
            return "different option", 0.0
    elif not sp or not sk or not subject_equiv(sp, sk):
        return "different option", 0.0
    if _big(numbers(p.title)) != _big(numbers(k.title)):
        return "numbers differ", 0.0
    if qualifiers(p.title) != qualifiers(k.title):
        return "different sub-market", 0.0
    da, db = directions(p.title), directions(k.title)
    if da and db and da != db:
        return "direction differs", 0.0
    ma, mb = months(p.title), months(k.title)
    if ma and mb and ma.isdisjoint(mb):
        return "month differs", 0.0
    ta, tb = _times(p), _times(k)
    gap = 0.0
    if ta and tb:
        gap = min(abs((x - y).total_seconds()) for x in ta for y in tb) / 86400
        # [GUARD] ladder rungs / dates: strict (MAX_CLOSE_GAP_DAYS). NAME options: Polymarket's endDate is sometimes a
        # placeholder, so a moderate gap is tolerated but flagged WARN (see match_option_events); a big gap means
        # a different contract (e.g. 'reach the final' vs 'win the title', NHL vs MLS) and is rejected.
        if gap > (MAX_CLOSE_GAP_DAYS if is_ladder else NAME_MAX_GAP_DAYS):
            return "close dates differ", gap
    return None, gap


def _match_options(poly, kalshi, pidx, kidx, rejected, reasons):
    """Within ONE matched event pair: pair each Polymarket option with the same Kalshi option."""
    by_p: dict[int, list] = defaultdict(list)
    for pi in pidx:
        for ki in kidx:
            why, gap = option_check(poly[pi], kalshi[ki])
            if why is None:
                by_p[pi].append((gap, ki))
            else:
                reasons[f"option: {why}"] += 1       # [GUARD] every option-level rejection is now counted in the run log
                if why not in ("different option", "different threshold", "close dates differ"):
                    rejected.append((95.0, f"option: {why}", poly[pi], kalshi[ki]))
    picks = []
    for pi, lst in by_p.items():
        lst.sort()
        if len(lst) > 1 and lst[1][0] - lst[0][0] < 1.0:             # two rungs equally close -> ambiguous
            reasons["option: ambiguous rung"] += 1
            continue
        picks.append((lst[0][0], pi, lst[0][1]))
    picks.sort()
    used, out = set(), []
    for gap, pi, ki in picks:
        if ki in used:
            continue
        used.add(ki)
        out.append((pi, ki, gap))
    return out


def match_option_events(poly, kalshi, rejected, reasons, report=None):
    """Returns (candidates [(score, pi, ki)], warnings {(pi, ki): [..]}).
    If `report` is a list, one row is appended per option event (either platform) that stayed unpaired:
    [platform, event title, # options, first options, closest event on other platform, similarity, why, link/ticker]."""
    pev: dict[str, list] = defaultdict(list)
    kev: dict[str, list] = defaultdict(list)
    for i, m in enumerate(poly):
        if m.kind == "option" and m.event_id:
            pev[m.event_id].append(i)
    for i, m in enumerate(kalshi):
        if m.kind == "option" and m.event_id:
            kev[m.event_id].append(i)
    if not pev or not kev:
        if report is not None:
            for pe, pidx in pev.items():
                report.append(["Polymarket", poly[pidx[0]].group, len(pidx),
                               ", ".join(poly[i].subject for i in pidx[:6]), "", 0,
                               "no Kalshi option events loaded", poly[pidx[0]].url])
            for ke, kidx in kev.items():
                report.append(["Kalshi", kalshi[kidx[0]].group, len(kidx),
                               ", ".join(kalshi[i].subject for i in kidx[:6]), "", 0,
                               "no Polymarket option events loaded", ke])
        return [], {}
    k_ids = list(kev)
    k_titles = [kalshi[kev[e][0]].group for e in k_ids]
    k_clean = [clean_words(t) for t in k_titles]

    info_p: dict = {}          # pe -> (similarity, kalshi event id, reason) for its closest failed attempt

    def _note(pe, sc, ke, why):
        cur = info_p.get(pe)
        if cur is None or sc > cur[0]:
            info_p[pe] = (sc, ke, why)

    found = []
    for pe, pidx in pev.items():
        p_title = poly[pidx[0]].group
        pc = clean_words(p_title)
        for _, _, j in process.extract(pc, k_clean, scorer=fuzz.token_set_ratio, limit=EVENT_CANDIDATES):
            ke, kt = k_ids[j], k_titles[j]
            ev_score = (fuzz.token_set_ratio(pc, k_clean[j]) + fuzz.token_sort_ratio(pc, k_clean[j])) / 2
            if ev_score < EVENT_MIN_SCORE:
                _note(pe, ev_score, ke, f"title similarity below {EVENT_MIN_SCORE:.0f}")
                continue
            why = event_reject(p_title, kt)
            if why:
                reasons[f"event: {why}"] += 1
                _note(pe, ev_score, ke, f"event rejected: {why}")
                continue
            matches = _match_options(poly, kalshi, pidx, kev[ke], rejected, reasons)
            n = len(matches)
            if n == 0:
                _note(pe, ev_score, ke, "titles similar, but no option matched (names / thresholds / dates differ)")
                continue
            coverage = n / max(1, min(len(pidx), len(kev[ke])))
            if not ((n >= 2 and coverage >= MIN_OPTION_COVERAGE) or (n == 1 and ev_score >= 85)):
                reasons["event: options do not line up"] += 1
                _note(pe, ev_score, ke, f"only {n} of {min(len(pidx), len(kev[ke]))} options lined up")
                continue
            found.append((n, ev_score, pe, ke, matches, p_title, kt))

    found.sort(key=lambda x: (x[0], x[1]), reverse=True)        # one Kalshi event per Polymarket event
    used_pe, used_ke = set(), set()
    cands, warn = [], {}
    for n, ev_score, pe, ke, matches, pt, kt in found:
        if pe in used_pe or ke in used_ke:
            _note(pe, ev_score, ke, "closest Kalshi event was already paired with another Polymarket event")
            continue
        used_pe.add(pe)
        used_ke.add(ke)
        for pi, ki, gap in matches:
            cands.append((min(99.0, ev_score), pi, ki))
            w = [f"INFO: option '{poly[pi].subject}' | Poly event '{pt[:45]}' ~ Kalshi event '{kt[:45]}'"]
            if n == 1:
                w.append("WARN: only one option matched between these two events")
            if gap > MAX_CLOSE_GAP_DAYS:
                w.append(f"WARN: the two close dates are {gap:.0f} days apart - check both resolution rules")
            warn[(pi, ki)] = w
    reasons["option events matched"] = len(used_pe)

    # ---- report every option event that found no counterpart ----
    if report is not None:
        p_ids = list(pev)
        p_titles = [poly[pev[e][0]].group for e in p_ids]
        p_clean = [clean_words(t) for t in p_titles]
        for pe, pidx in pev.items():
            if pe in used_pe:
                continue
            sc, ke, why = info_p.get(pe, (0.0, None, "no Kalshi option event to compare"))
            report.append(["Polymarket", poly[pidx[0]].group, len(pidx),
                           ", ".join(poly[i].subject for i in pidx[:6]),
                           kalshi[kev[ke][0]].group if ke else "", round(sc), why, poly[pidx[0]].url])
        for ke, kidx in kev.items():
            if ke in used_ke:
                continue
            kt = kalshi[kidx[0]].group
            kc = clean_words(kt)
            best = process.extractOne(kc, p_clean, scorer=fuzz.token_set_ratio) if p_clean else None
            if not best:
                report.append(["Kalshi", kt, len(kidx), ", ".join(kalshi[i].subject for i in kidx[:6]),
                               "", 0, "no Polymarket option event to compare", ke])
                continue
            pe = p_ids[best[2]]
            pt = p_titles[best[2]]
            sc = (fuzz.token_set_ratio(kc, p_clean[best[2]]) + fuzz.token_sort_ratio(kc, p_clean[best[2]])) / 2
            if sc < EVENT_MIN_SCORE:
                why = f"title similarity below {EVENT_MIN_SCORE:.0f}"
            elif event_reject(pt, kt):
                why = f"event rejected: {event_reject(pt, kt)}"
            elif pe in used_pe:
                why = "closest Polymarket event was already paired with another Kalshi event"
            else:
                why = "titles similar, but too few options lined up"
            report.append(["Kalshi", kt, len(kidx), ", ".join(kalshi[i].subject for i in kidx[:6]),
                           pt, round(sc), why, ke])
    return cands, warn
