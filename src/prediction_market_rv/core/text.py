"""Text normalisation helpers: cleaning, numbers, months, directions, concepts, entities."""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata

from rapidfuzz import fuzz


_QUAL_WORDS = "set|map|game|round|period|quarter|half|inning|leg|stage|frame"
_ORD = {"1st": "1", "first": "1", "2nd": "2", "second": "2", "3rd": "3", "third": "3",
        "4th": "4", "fourth": "4", "5th": "5", "fifth": "5"}


@lru_cache(maxsize=None)      # [SPEED]
def qualifiers(text: str) -> set:
    """Sub-market markers that must agree: ('game','4'), ('set','2'), ('map','1'), ('half','1')..."""
    t = normalize(text).lower()
    q = set(re.findall(rf"\b({_QUAL_WORDS})\s*#?(\d{{1,2}})\b", t))
    q |= {(w, _ORD[o]) for o, w in re.findall(
        rf"\b(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth)\s+({_QUAL_WORDS})\b", t)}
    return q


def infer_subjects(texts: list[str]) -> list[str]:
    """Strip the words shared by every question: ['Will France win the 2026 World Cup?',
    'Will Spain win the 2026 World Cup?'] -> ['France', 'Spain']."""
    toks = [t.split() for t in texts]
    if len(toks) < 2:
        return list(texts)
    pre = 0
    while all(len(t) > pre for t in toks) and len({t[pre].lower() for t in toks}) == 1:
        pre += 1
    suf = 0
    while all(len(t) - pre > suf for t in toks) and len({t[-1 - suf].lower() for t in toks}) == 1:
        suf += 1
    out = []
    for t, orig in zip(toks, texts):
        core = t[pre:len(t) - suf] if suf else t[pre:]
        out.append(" ".join(core).strip(" ?,-:") or orig)
    return out


STOP_WORDS = {
    "will", "the", "in", "by", "to", "be", "a", "of", "on", "at", "is", "there",
    "how", "many", "his", "her", "this", "that", "an", "or", "and", "for", "does",
    "do", "what", "which", "who", "are", "than", "it", "its",
}

MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}
MONTH_ABBR = {"jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}

DIRECTION = {
    "above": "up", "over": "up", "exceed": "up", "exceeds": "up", "higher": "up",
    "greater": "up", "more": "up", "least": "up",
    "below": "down", "under": "down", "lower": "down", "less": "down", "most": "down",
    "before": "before", "after": "after",
}

# Words that change WHAT is being asked. Both questions must contain the same set.
#   "announce an IPO" != "IPO happens";  "win the election" != "be on the ticket";
#   "lead underwriter" != "lead-left underwriter";  "meet" != "meet together (3-way)".
CONCEPTS = {
    "nominee": ("nomin",),
    "election": ("elect",),
    "ticket": ("ticket",),
    "together": ("together", "trilateral"),
    "announce": ("announc",),
    "leadleft": ("lead-left",),
    "meet": ("meet",),
    "speak": ("speak", "spoke", "talk"),
    "run": ("run",),
    "vice": ("vice",),
    "primary": ("primar",),
}

NEGATIONS = {"not", "never", "won't", "cannot", "fail", "fails"}

GENERIC_ENT = (
    {"will", "yes", "no", "who", "what", "which", "how", "does", "is", "are", "the", "a", "an",
     "before", "after", "by", "in", "on", "when", "where", "us", "usa", "united", "states",
     "presidency", "presidential", "president", "presidents", "party", "republican",
     "democratic", "election", "nomination", "nominee", "nominees", "ipo", "jr", "sr",
     "ii", "iii"}
    | MONTHS | MONTH_ABBR
)

# [GUARD] the suffix letter must end at a word boundary: "5 most" used to be read as 5,000,000 ("m" = million)
NUM_RE = re.compile(r"\$?\s?(\d[\d,]*\.?\d*)(?:\s?(k|m|b|bn|million|billion|thousand)\b|%)?", re.I)
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
        "b": 1e9, "bn": 1e9, "billion": 1e9, "%": 1.0}


@lru_cache(maxsize=None)      # [SPEED]
def normalize(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"initial public offering", "IPO", t, flags=re.I)
    t = re.sub(r"\b(\w+)'s\b", r"\1", t)                       # Anthropic's -> Anthropic
    t = re.sub(r"(?<=\s)['\"][^'\"]+['\"](?=\s)", " ", t)      # Dwayne 'The Rock' Johnson
    return t


@lru_cache(maxsize=None)      # [SPEED]
def clean_words(text: str) -> str:
    t = normalize(text).lower()
    for old, new in (("united states", "us"), ("u.s.", "us"), ("step down", "resign"),
                     ("steps down", "resign"), ("stepping down", "resign")):
        t = t.replace(old, new)               # scoring-only synonyms (hard checks use the raw titles)
    cleaned = re.sub(r"[^\w\s]", " ", t)
    return " ".join(w for w in cleaned.split() if w not in STOP_WORDS)


@lru_cache(maxsize=None)      # [SPEED]
def numbers(text: str) -> set[float]:
    vals: set[float] = set()
    for num, suffix in NUM_RE.findall(text):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        vals.add(round(v * MULT.get((suffix or "").lower(), 1.0), 6))
    return vals


@lru_cache(maxsize=None)      # [SPEED]
def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z\-']*", normalize(text).lower())


@lru_cache(maxsize=None)      # [SPEED]
def directions(text: str) -> set[str]:
    return {DIRECTION[w] for w in _tokens(text) if w in DIRECTION}


@lru_cache(maxsize=None)      # [SPEED]
def months(text: str) -> set[str]:
    return set(_tokens(text)) & MONTHS


@lru_cache(maxsize=None)      # [SPEED]
def concepts(text: str) -> set[str]:
    toks = _tokens(text)
    return {
        name
        for name, stems in CONCEPTS.items()
        if any(t.startswith(st) for t in toks for st in stems)
    }


@lru_cache(maxsize=None)      # [SPEED]
def is_negated(text: str) -> bool:
    return any(t in NEGATIONS for t in _tokens(text))


@lru_cache(maxsize=None)      # [SPEED]
def _words(text: str) -> set[str]:
    return {w.lower().replace(".", "").replace("'", "")
            for w in re.findall(r"[A-Za-z][A-Za-z\.']*", normalize(text))}


@lru_cache(maxsize=None)      # [SPEED]
def entities(text: str) -> set[str]:
    out = set()
    for tok in re.findall(r"[A-Z][A-Za-z\.']*", normalize(text)):
        t = tok.lower().replace(".", "").replace("'", "")
        if len(t) > 1 and t not in GENERIC_ENT:
            out.add(t)
    return out


def _has_close(e: str, words: set[str]) -> bool:
    return e in words or any(fuzz.ratio(e, w) >= 85 for w in words)   # Hilary ~ Hillary


def _entity_extras(a: str, b: str):
    """Capitalised names present on one side but missing on the other: (extras in a, extras in b)."""
    wa, wb = _words(a), _words(b)
    return ([e for e in entities(a) if not _has_close(e, wb)],
            [e for e in entities(b) if not _has_close(e, wa)])
