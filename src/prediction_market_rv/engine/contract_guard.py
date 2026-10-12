"""
contract_guard.py - extra "are these really the SAME contract?" checks for the Polymarket <-> Kalshi matcher.

check_pair(poly_title, kalshi_title) -> (verdict, reasons)
    verdict = "reject"  hard conflict -> NOT the same contract, never trade
              "review"  nothing contradicts, but the wording is not close enough to trust blindly
              "ok"      all guards pass
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from functools import lru_cache

MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12,
          "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
          "oct": 10, "nov": 11, "dec": 12}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))

SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
NUM = r"(\d[\d,]*(?:\.\d+)?)(?:\s?(k|m|bn|b|thousand|million|billion)\b|(?:%))?"


def _clean(t: str) -> str:
    t = "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c))
    t = t.replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return t.lower()


def _val(num: str, suf: str | None) -> float:
    v = float(num.replace(",", ""))
    return round(v * SCALE.get((suf or "").lower(), 1.0), 6)


# ---------------------------------------------------------------- dates -------------------------------
DATE_RE = re.compile(rf"\b(?P<pre>before|by|until|prior to|on or before|on|in)?\s*(?P<mon>{_MON})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d\d))?\b")


def deadlines(t: str) -> set[date]:
    """Full dates with a year -> the LAST day on which a YES can still happen ('before Jan 1' = Dec 31)."""
    out = set()
    for m in DATE_RE.finditer(t):
        if not m.group("year"):
            continue
        try:
            d = date(int(m.group("year")), MONTHS[m.group("mon")], int(m.group("day")))
        except ValueError:
            continue
        if m.group("pre") in ("before", "prior to"):
            d -= timedelta(days=1)
        out.add(d)
    return out


def strip_dates(t: str) -> str:
    t = DATE_RE.sub(" ", t)
    t = re.sub(r"\b20\d\d\s*-\s*(?:20)?\d\d\b", " ", t)        # season 2026-27 / 2026-2027
    t = re.sub(r"\b20\d\d\b", " ", t)                             # bare years
    return t


def month_days(t: str) -> set[tuple[int, int]]:
    """(month, day) of every explicit calendar date; 'before X' counts as the day before X. Year ignored."""
    out = set()
    for m in DATE_RE.finditer(t):
        try:
            d = date(2001, MONTHS[m.group("mon")], int(m.group("day")))
        except ValueError:
            continue
        if m.group("pre") in ("before", "prior to"):
            d -= timedelta(days=1)
        out.add((d.month, d.day))
    return out


def window_words(t: str) -> set[str]:
    """Time windows that are NOT calendar dates: 'before the midterms' != 'in Oct 2026'."""
    w = set()
    if re.search(r"midterm|election day", t):
        w.add("midterms")
    if re.search(r"end of (?:the )?(?:year|month|\w+ 20\d\d)", t):
        w.add("end-of-period")
    if re.search(r"\bq[1-4]\b|quarter", t):
        w.add("quarter")
    return w


# ---- key verbs: "impeached" != "impeached AND REMOVED", "acquired" != "ANNOUNCE acquisition"
KEY_VERBS = {"remove": r"remov|convict", "announce": r"announc|unveil|reveal", "indict": r"indict|charg",
             "arrest": r"arrest", "resign": r"resign|step(?:s|ped)? down|out as|leave|fired",
             "acquire": r"acqui|buyout|takeover", "launch": r"launch|releas", "sign": r"\bsign",
             "ban": r"\bban", "veto": r"veto", "recession": r"recession", "rate_cut": r"rate cut|cut rates|lower rates",
             "rate_hike": r"rate hike|hike|raise",
             # 'Make the 2027 CFP National Championship GAME' (reach the final) != 'WIN the National Championship'
             "reach": r"\bmakes?\s+the\b|\breach(?:es|ed)?\b|\badvances?\b|\bqualif\w*|\bfinalist|"
                      r"(?:title|championship) game\b|\bto the (?:final|finals|semis|semifinals)\b"}


# ---- leagues: 'NHL Western Conference' != 'MLS Western Conference'
LEAGUE_PATTERNS = {
    "nhl": r"\bnhl\b|pro hockey", "nba": r"\bnba\b|pro basketball", "nfl": r"\bnfl\b|pro football",
    "mlb": r"\bmlb\b|pro baseball", "mls": r"\bmls\b", "wnba": r"\bwnba\b", "epl": r"\bepl\b|premier league",
    "ncaaf": r"college football|ncaa football|\bcfb\b|\bcfp\b",
    "ncaab": r"college basketball|ncaa (?:men'?s |women'?s )?basketball|march madness",
    "laliga": r"la liga", "seriea": r"serie a", "bundesliga": r"bundesliga", "ligue1": r"ligue 1",
    "uel": r"europa league", "ucl": r"champions league", "uecl": r"conference league", "unl": r"nations league"}


def leagues(t: str) -> set[str]:
    return {n for n, pat in LEAGUE_PATTERNS.items() if re.search(pat, t)}


def key_verbs(t: str) -> set[str]:
    return {name for name, pat in KEY_VERBS.items() if re.search(pat, t)}


# ---- game market type: full-game winner vs 1H / 1Q / race-to-N / halftime ...
GAME_TYPE_PATTERNS = {"1h": r"\b1h\b|1st half|first half", "2h": r"\b2h\b|2nd half|second half",
                      "q": r"\b[1-4]q\b|(?:1st|2nd|3rd|4th|first|second|third|fourth) quarter",
                      "race": r"race to|reach \d+ points|first to", "ht": r"half\s?time|leading at|trailing at",
                      "first": r"score first|first goal|to score", "ot": r"overtime|extra time|penalt",
                      "spread": r"spread|handicap|\(\s*[+-]\d|win by|margin", "total": r"total goals|over/under|o/u",
                      "btts": r"both teams|btts|exact score|clean sheet|anytime|goal ?scorer|corners?|\bcards?\b|shots"}


def game_type(t: str) -> frozenset:
    return frozenset(n for n, pat in GAME_TYPE_PATTERNS.items() if re.search(pat, t))


# ---------------------------------------------------------------- thresholds / ranks ----------------
CMP_PREFIX = [
    (r"at least|no less than|minimum of|>=|≥", ">="),
    (r"more than|greater than|higher than|above|over|exceeds?|surpass(?:es)?|>", ">"),
    (r"at most|no more than|maximum of|<=|≤", "<="),
    (r"less than|fewer than|lower than|below|under|<", "<"),
    (r"↑", ">"), (r"↓", "<"),
]
CMP_SUFFIX = [(r"\+|or more|or higher|or above", ">="), (r"or fewer|or less|or lower|or below", "<=")]


def comparators(t: str) -> set[tuple]:
    out = set()
    for pat, op in CMP_PREFIX:
        for m in re.finditer(rf"(?:{pat})\s*\$?\s*{NUM}", t):
            out.add((op, _val(m.group(1), m.group(2))))
    for pat, op in CMP_SUFFIX:
        for m in re.finditer(rf"\$?{NUM}\s*(?:{pat})", t):
            out.add((op, _val(m.group(1), m.group(2))))
    # integer-valued quantities: '>70' == '>=71', '<8' == '<=7'
    norm = set()
    for op, v in out:
        if float(v).is_integer() and op == ">":
            norm.add((">=", v + 1))
        elif float(v).is_integer() and op == "<":
            norm.add(("<=", v - 1))
        else:
            norm.add((op, v))
    return norm


def exact_values(t: str) -> set[float]:
    return {_val(m.group(1), m.group(2)) for m in re.finditer(rf"\b(?:be|is|exactly|equal to|equals)\s+\$?{NUM}", t)}


def ranges(t: str) -> set[tuple]:
    t = strip_dates(t)
    return {(_val(m.group(1), None), _val(m.group(3), None))
            for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(?:°\w?)?\s*(-|to)\s*(\d[\d,]*(?:\.\d+)?)", t)}


def all_numbers(t: str) -> set[float]:
    """Every number except dates / years / season labels. Ranks (#2, top 5) and thresholds count."""
    t = strip_dates(t)
    return {_val(m.group(1), m.group(2)) for m in re.finditer(rf"(?<![\w.]){NUM}", t)}


ORD_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10}


def norm_ordinals(t: str) -> str:
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b(?=\s+(?:overall|pick|seed|place|rank|round))", r"#\1", t)
    return re.sub(rf"\b({'|'.join(ORD_WORDS)})\b(?=\s+(?:overall|pick|seed|place|rank))",
                  lambda m: f"#{ORD_WORDS[m.group(1)]}", t)


def ranks(t: str) -> set[str]:
    out = set(re.findall(r"top[\s-]*(\d+)", t))
    out |= set(re.findall(r"#\s*(\d+)", t))
    out |= set(re.findall(r"\b(\d+)(?:st|nd|rd|th)\s+(?:place|seed|rank|pick|finish)", t))
    out |= set(re.findall(r"\b(?:seed|rank|place|pick)\s*#?(\d+)", t))
    return {"rank" + x for x in out}


# ---------------------------------------------------------------- scope / place ------------------------
def scope(t: str) -> set[str]:
    s = set()
    if re.search(r"\bglobal(?:ly)?\b|\bworldwide\b", t):
        s.add("global")
    if re.search(r"\bin the (?:us|u\.s\.|united states)\b|\bus-only\b|\bu\.s\. only\b", t):
        s.add("us")
    return s


PLACE_ALIASES = {"nyc": "new york city", "new york": "new york city", "new york city": "new york city",
                 "la": "los angeles", "los angeles": "los angeles"}


def weather_place(t: str) -> str | None:
    m = re.search(r"(?:highest|lowest|maximum|minimum|high|low)\s+temperature\s+in\s+([a-z .'\-]+?)(?:\s+(?:be|on|today|tomorrow|this)\b|\?|,|\s+-)", t)
    if not m:
        return None
    p = m.group(1).strip()
    return PLACE_ALIASES.get(p, p)


# ---------------------------------------------------------------- game props ----------------------------
GAME_PROP_RE = re.compile(r"half\s?time|1st half|2nd half|first half|second half|leading at|trailing at|score first|"
                          r"to score|scores first|first goal|first to|both teams|btts|exact score|clean sheet|"
                          r"goal ?scorer|anytime|corners?|\bcards?\b|shots|total goals|win by|margin|spread|"
                          r"handicap|\(\s*[+-]\d|overtime|extra time|penalt|1x2|double chance|draw no bet", re.I)


def game_prop(t: str) -> bool:
    return bool(GAME_PROP_RE.search(t))


# ---------------------------------------------------------------- topic coverage ------------------------
STOP = set("""will the a an of in on at by to be is are for and or than that this it its there who what which how
many does do has have had win wins won finish finishes season regular year yes no before after during until
announce announced announces officially official happen happens get gets make makes become becomes lead leads
nominee nomination candidate party us u.s. next first""".split())
GENERIC = {"champion", "championship", "champions", "winner", "title", "league", "cup"}


def _stem(w: str) -> str:
    w = w.strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    return w[:5] if len(w) >= 5 else w


def content_tokens(t: str) -> set[str]:
    t = strip_dates(t)
    t = re.sub(r"[^a-z\s]", " ", t)
    return {_stem(w) for w in t.split() if len(w) > 2 and w not in STOP}


def topic_coverage(p: str, k: str) -> float:
    tp, tk = content_tokens(p), content_tokens(k)
    return 1.0 if not tp else len(tp & tk) / len(tp)


# ---------------------------------------------------------------- main -----------------------------------
REVIEW_COVERAGE = 0.60     # below this the two titles share too little vocabulary to trust blindly
REJECT_COVERAGE = 0.34     # below this they are about different things


def check_pair(poly_title: str, kalshi_title: str, kind: str = "binary") -> tuple[str, list[str]]:
    """kind = 'game' for team-vs-team markets: tournament names carry digits ('Santa Cruz 2') and team names differ
    in spelling, so only the game-prop check applies there (the matcher already compares the teams)."""
    p, k = norm_ordinals(_clean(poly_title)), norm_ordinals(_clean(kalshi_title))
    hard, soft = [], []
    if kind == "game":
        gp, gk = game_type(p), game_type(k)
        return ("reject", [f"game market type differs {sorted(gp)} vs {sorted(gk)}"]) if gp != gk else ("ok", [])

    # 1. every non-date number must agree (ranks, top-N, thresholds, scores)
    np_, nk = all_numbers(p), all_numbers(k)
    if np_ != nk:
        hard.append(f"numbers differ {sorted(np_ ^ nk)[:4]}")
    if ranks(p) != ranks(k):
        hard.append(f"rank / top-N differs {sorted(ranks(p) ^ ranks(k))}")

    # 2. threshold wording: at least X != above X != exactly X ; ranges must match
    cp, ck = comparators(p), comparators(k)
    if cp and ck and cp != ck:
        hard.append(f"threshold differs {sorted(cp)} vs {sorted(ck)}")
    elif (cp and not ck and exact_values(k)) or (ck and not cp and exact_values(p)):
        hard.append("one side is an exact value, the other a threshold")
    elif bool(cp) != bool(ck) and (np_ & nk):
        soft.append("threshold wording only on one side")
    rp, rk = ranges(p), ranges(k)
    if rp and rk and rp != rk:
        hard.append("range differs")

    # 3. deadline (full dates only; 'before X' = X-1)
    dp, dk = deadlines(p), deadlines(k)
    if dp and dk and dp != dk:
        hard.append(f"deadline differs {sorted(dp)[0]} vs {sorted(dk)[0]}")

    mp, mk = month_days(p), month_days(k)
    if mp and mk and mp != mk:
        hard.append(f"calendar date differs {sorted(mp)[:2]} vs {sorted(mk)[:2]}")
    wp_, wk_ = window_words(p), window_words(k)
    if wp_ != wk_:
        hard.append(f"time window differs {sorted(wp_)} vs {sorted(wk_)}")
    vp, vk = key_verbs(p), key_verbs(k)
    if vp != vk:
        hard.append(f"event type differs {sorted(vp ^ vk)}")
    lp_, lk_ = leagues(p), leagues(k)
    if lp_ and lk_ and lp_.isdisjoint(lk_):
        hard.append(f"different league {sorted(lp_)} vs {sorted(lk_)}")
    if ("fantasy" in p) != ("fantasy" in k):
        hard.append("fantasy-points market vs real-stats market")

    # 4. scope / place
    sp, sk = scope(p), scope(k)
    if sp != sk:
        hard.append(f"scope differs {sorted(sp)} vs {sorted(sk)}")
    wp, wk = weather_place(p), weather_place(k)
    if wp and wk and wp != wk:
        hard.append(f"weather station differs: {wp} vs {wk}")

    if game_type(p) != game_type(k):
        hard.append("game prop vs full-match winner")

    if re.search(r"temperature|\brain\b|snow|precipitation|rainfall", p + " " + k):
        soft.append("weather market: venues use different stations / sources - read both rules")

    # 6. vocabulary coverage
    cov = topic_coverage(p, k)
    if cov < REJECT_COVERAGE:
        hard.append(f"different subject (shared vocabulary {cov:.0%})")
    elif cov < REVIEW_COVERAGE:
        soft.append(f"low wording overlap ({cov:.0%})")

    if hard:
        return "reject", hard
    return ("review", soft) if soft else ("ok", [])


# ---------------------------------------------------------------- ladder rungs -------------------------
@lru_cache(maxsize=None)
def rung_key(label: str):
    """Canonical form of a ladder option label (the part that differs between rungs of one event).
    '<8' == 'Below 8' ; '12+' == '12 or more' ; '8' == '8' ; 'December 31, 2026' == 'Before Jan 1, 2027'.
    Returns None when the label has no number / date (then it is a name option, compare by name instead)."""
    t = norm_ordinals(_clean(label))
    md = month_days(t)
    if md:                                   # year ignored on purpose: Polymarket writes 'October 31', Kalshi 'Before Nov 1, 2026'
        return ("date", tuple(sorted(md)))   # (the matcher's close-date gap check separates 2026 from 2027)
    c = comparators(t)
    if c:
        return ("cmp", tuple(sorted(c)))
    r = ranges(strip_dates(t))
    if r:
        return ("range", tuple(sorted(r)))
    n = all_numbers(t)
    return ("eq", tuple(sorted(n))) if n else None