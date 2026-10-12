"""
Polymarket <-> Kalshi single-question Yes/No matcher + price comparison.

pip install httpx rapidfuzz dnspython openpyxl   (dnspython is optional, see USE_CUSTOM_DNS)

Needs contract_guard.py in the same folder. Edits are marked "# [GUARD]" (matching correctness), "# [SPEED]" (performance) and "# [TIMING]" (run-time log).
Also needs numpy (rapidfuzz.process.cdist returns numpy arrays):  pip install numpy
"""
from __future__ import annotations

import contextlib
import io
import json
import math
from pathlib import Path
import re
import socket
import unicodedata
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor      # [SPEED] parallel network calls

import httpx
import numpy as np                                          # [SPEED] path 2 works on arrays
from rapidfuzz import fuzz, process

import legacy.contract_guard as cg      # [GUARD] semantic "is it really the same contract?" checks

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
USE_CUSTOM_DNS = True          # your original DNS workaround; set False if not needed
POLY_PAGES = 30                # 30 x 100 = 3000 Polymarket events (low-volume markets matter too)
KALSHI_PAGES = 60              # generic scan of ALL open events (stops when the API runs out)
KALSHI_SERIES_HINTS = ["KXNHLGAME", "KXNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNCAAFGAME",
                       "KXNCAAMBGAME", "KXATPMATCH", "KXEPLGAME"]   # game-winner series; unknown ones just return nothing
POLY_TAG_SLUGS = ["sports", "politics", "economy", "crypto", "finance", "geopolitics",
                  "business", "tech", "culture"]   # unknown slugs just return nothing
POLY_PAGES_PER_TAG = 10
ALLOW_MULTI_CHOICE = True      # True = also scan 'Who will win...' lists: each option (France, Spain...) is matched
                               # to the SAME option on the other platform, after the two questions are matched
EVENT_MIN_SCORE = 62.0         # min title similarity to treat two multi-choice / ladder events as the same question
EVENT_CANDIDATES = 10          # how many Kalshi events are tried per Polymarket event
MIN_OPTION_COVERAGE = 0.25     # >= this share of the smaller event's options must line up (>=2 options matched)
DEBUG_MIN_SCORE = 78.0         # only show rejected pairs at least this similar (less noise)
CANDIDATES_PER_POLY = 25       # fuzzy shortlist size per Polymarket question
MIN_SCORE = 70.0               # fuzzy score threshold (constraints do the real filtering)
ENTITY_SOFT_MIN_SCORE = 80.0   # one-sided extra names (Benjamin Netanyahu vs Netanyahu) allowed from this score, flagged WARN
MAX_NEAR_MISSES = 300          # rows kept in the 'Not matched' sheet
MAX_CLOSE_GAP_DAYS = 7         # reject pairs whose close dates differ more than this
ALLOW_LADDERS = True           # True = also scan date / threshold ladders ("Merz out before X", "BTC above $100k"),
                               # each rung matched to the same rung on the other platform
MAX_SPREAD = 0.10              # flag a leg as illiquid if its bid-ask spread is wider
SUSPICIOUS_EDGE = 0.08         # edges this big are usually a rules mismatch, not free money
NAME_MAX_GAP_DAYS = 60         # [GUARD] name options (a team, a candidate): reject if the two close dates are further apart
                               # than this; between MAX_CLOSE_GAP_DAYS and this the pair is kept but flagged WARN
P2_CHUNK = 192                 # [SPEED] Polymarket questions scored per rapidfuzz.cdist call (memory ~ chunk x candidates x 4 bytes)
P2_SPAN_DAYS = 14              # [SPEED] a chunk only spans this many days of close dates, so its Kalshi pool stays small
P2_WORKERS = -1                # [SPEED] CPU threads for the fuzzy scoring; -1 = all cores, 1 = single thread
FETCH_WORKERS = 4              # [SPEED] parallel HTTP requests (Polymarket passes, Kalshi series, Kalshi links); 1 = sequential
DUST_PRICE = 0.01              # [GUARD] a leg priced under 1c (or over 99c) is a dust / stale quote, not a real book
ORDER_CONTRACTS = 100          # order size used to apply Kalshi's round-up-to-the-cent rule
KALSHI_FEE_COEF = 0.07         # taker fee = ceil_to_cent(0.07 * C * P * (1-P)); some products are cheaper
# Polymarket taker fee per share = rate * p * (1-p) (maker = 0). Rates by category, from Polymarket's
# fee docs / Mar-2026 rollout coverage; they change, so VERIFY at docs.polymarket.com/trading/fees.
CAPITAL = 1000.0               # example bankroll used for the "profit on $X" column
MIN_NET_EDGE = 0.0             # only report opportunities with net edge (after fees) above this, per $1 payout
OUTPUT_DIR = "."               # where the Excel file is written
POLY_FEE_RATES = {
    "crypto": 0.072, "sports": 0.03,
    "finance": 0.04, "politics": 0.04, "mentions": 0.04, "tech": 0.04,
    "economics": 0.05, "culture": 0.05, "weather": 0.05, "other": 0.05,
    "geopolitics": 0.0,        # geopolitics / world events are fee-free
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

POLY_URL = "https://gamma-api.polymarket.com/events"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/events"

# ----------------------------------------------------------------------------
# Optional in-memory DNS fix
# ----------------------------------------------------------------------------
if USE_CUSTOM_DNS:
    try:
        import dns.resolver

        _resolver = dns.resolver.Resolver()
        _resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
        _orig_getaddrinfo = socket.getaddrinfo

        @lru_cache(maxsize=256)
        def _resolve(host: str) -> str | None:
            try:
                return str(_resolver.resolve(host, "A")[0])
            except Exception:
                return None

        def _patched_getaddrinfo(host, port, *args, **kwargs):
            ip = _resolve(host) if isinstance(host, str) else None
            return _orig_getaddrinfo(ip or host, port, *args, **kwargs)

        socket.getaddrinfo = _patched_getaddrinfo
    except ImportError:
        pass


# ----------------------------------------------------------------------------
# Normalized market
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# Fetchers
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Sports helpers: parse "A vs B", compare teams (incl. city <-> nickname), sub-market qualifiers
# ----------------------------------------------------------------------------
TEAM_ALIAS_RAW = """
arizona|cardinals;atlanta|falcons;baltimore|ravens;buffalo|bills;carolina|panthers;chicago|bears;cincinnati|bengals;
cleveland|browns;dallas|cowboys;denver|broncos;detroit|lions;green bay|packers;houston|texans;indianapolis|colts;
jacksonville|jaguars;kansas city|chiefs;las vegas|raiders;los angeles|chargers;los angeles|rams;miami|dolphins;
minnesota|vikings;new england|patriots;new orleans|saints;new york|giants;new york|jets;philadelphia|eagles;
pittsburgh|steelers;san francisco|49ers;seattle|seahawks;tampa bay|buccaneers;tennessee|titans;washington|commanders;
boston|celtics;brooklyn|nets;charlotte|hornets;cleveland|cavaliers;dallas|mavericks;denver|nuggets;detroit|pistons;
golden state|warriors;houston|rockets;indiana|pacers;los angeles|clippers;los angeles|lakers;memphis|grizzlies;
milwaukee|bucks;minnesota|timberwolves;new orleans|pelicans;new york|knicks;oklahoma city|thunder;orlando|magic;
philadelphia|76ers;phoenix|suns;portland|trail blazers;sacramento|kings;san antonio|spurs;toronto|raptors;
utah|jazz;washington|wizards;atlanta|hawks;chicago|bulls;miami|heat;
arizona|diamondbacks;atlanta|braves;baltimore|orioles;boston|red sox;chicago|cubs;chicago|white sox;cincinnati|reds;
colorado|rockies;detroit|tigers;houston|astros;kansas city|royals;los angeles|angels;los angeles|dodgers;miami|marlins;
milwaukee|brewers;minnesota|twins;new york|mets;new york|yankees;philadelphia|phillies;pittsburgh|pirates;
san diego|padres;san francisco|giants;seattle|mariners;st louis|cardinals;tampa bay|rays;texas|rangers;
toronto|blue jays;washington|nationals;
anaheim|ducks;boston|bruins;buffalo|sabres;calgary|flames;carolina|hurricanes;chicago|blackhawks;colorado|avalanche;
columbus|blue jackets;dallas|stars;detroit|red wings;edmonton|oilers;florida|panthers;los angeles|kings;
minnesota|wild;montreal|canadiens;nashville|predators;new jersey|devils;new york|islanders;new york|rangers;
ottawa|senators;philadelphia|flyers;pittsburgh|penguins;san jose|sharks;seattle|kraken;st louis|blues;
tampa bay|lightning;toronto|maple leafs;utah|mammoth;vancouver|canucks;vegas|golden knights;
washington|capitals;winnipeg|jets
"""
SOCCER_SYNONYMS_RAW = """
manchester united=man united=man utd=mufc; manchester city=man city; tottenham hotspur=tottenham=spurs;
newcastle united=newcastle; west ham united=west ham; wolverhampton wanderers=wolverhampton=wolves;
nottingham forest=nottm forest=forest; brighton and hove albion=brighton hove albion=brighton; aston villa=villa;
crystal palace=palace; leeds united=leeds; leicester city=leicester; ipswich town=ipswich; afc bournemouth=bournemouth;
arsenal; chelsea; liverpool; everton; fulham; brentford; burnley; southampton; sunderland;
real madrid; barcelona=fc barcelona=barca; atletico madrid=atletico de madrid=atletico; sevilla; valencia;
villarreal; real sociedad=sociedad; athletic club=athletic bilbao=bilbao; real betis=betis; celta vigo=celta;
rayo vallecano=rayo; girona; osasuna; getafe; mallorca; alaves; espanyol; las palmas;
juventus=juve; ac milan=milan; inter=inter milan=internazionale; napoli=ssc napoli; roma=as roma; lazio=ss lazio;
atalanta; fiorentina; bologna; torino; udinese; genoa; cagliari; hellas verona=verona; lecce; parma; como; sassuolo;
bayern munich=bayern=bayern munchen; borussia dortmund=dortmund=bvb; bayer leverkusen=leverkusen; rb leipzig=leipzig;
eintracht frankfurt=frankfurt; borussia monchengladbach=gladbach=monchengladbach; vfb stuttgart=stuttgart;
vfl wolfsburg=wolfsburg; sc freiburg=freiburg; union berlin=1 fc union berlin; werder bremen=bremen; mainz 05=mainz;
tsg hoffenheim=hoffenheim; augsburg; heidenheim; st pauli=fc st pauli; hamburg=hamburger sv;
paris saint germain=psg=paris sg; olympique marseille=marseille=om; olympique lyonnais=lyon=ol; as monaco=monaco;
losc lille=lille; ogc nice=nice; stade rennais=rennes; rc lens=lens; nantes; strasbourg; toulouse; brest; angers;
benfica=sl benfica; porto=fc porto; sporting cp=sporting lisbon=sporting; braga=sc braga; ajax=afc ajax;
psv=psv eindhoven; feyenoord; az alkmaar=az; celtic; glasgow rangers=rangers; galatasaray; fenerbahce; besiktas;
la galaxy=los angeles galaxy; lafc=los angeles fc; inter miami=inter miami cf; new york red bulls; nycfc=new york city;
seattle sounders; portland timbers; atlanta united; columbus crew; philadelphia union; fc cincinnati=cincinnati
"""
_CLUB_TOKENS = {"fc", "cf", "afc", "sc", "ac", "cd", "ud", "fk", "sk", "bk", "if", "ssc", "ss", "as", "rc",
                "cfc", "ogc", "fsv", "vfl", "vfb", "tsg", "sv", "the"}
DRAW_WORDS = {"draw", "tie"}


@lru_cache(maxsize=None)      # [SPEED]
def _base(s: str) -> str:
    """lowercase, strip accents / (tags) / punctuation / club prefixes (FC, AFC, SC...)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(w for w in s.split() if w not in _CLUB_TOKENS)


_EQUIV: dict[str, set] = {}


def _add_equiv(a: str, b: str) -> None:
    a, b = _base(a), _base(b)
    if a and b and a != b:
        _EQUIV.setdefault(a, set()).add(b)
        _EQUIV.setdefault(b, set()).add(a)


for _entry in re.split(r"[;\n]", TEAM_ALIAS_RAW):
    if "|" in _entry:
        _city, _nick = (x.strip() for x in _entry.split("|", 1))
        _add_equiv(_city, _nick)
        _add_equiv(_city, f"{_city} {_nick}")
        _add_equiv(_nick, f"{_city} {_nick}")
for _group in re.split(r"[;\n]", SOCCER_SYNONYMS_RAW):
    _names = [x.strip() for x in _group.split("=") if x.strip()]
    for _i in range(len(_names)):
        for _j in range(_i + 1, len(_names)):
            _add_equiv(_names[_i], _names[_j])

_SEP = re.compile(r"\s+(?:vs\.?|v\.?|at|@)\s+", re.I)
_QUAL_WORDS = "set|map|game|round|period|quarter|half|inning|leg|stage|frame"
_ORD = {"1st": "1", "first": "1", "2nd": "2", "second": "2", "3rd": "3", "third": "3",
        "4th": "4", "fourth": "4", "5th": "5", "fifth": "5"}


def parse_teams(text: str):
    """'LoL: BNK FEARX vs JD Gaming - Game 4 Winner' -> ('BNK FEARX', 'JD Gaming'). None if not a 2-team title."""
    t = normalize(text or "")
    t = re.sub(r"\s*\bwinner\b\??\s*$", "", t, flags=re.I).strip(" ?")
    for part in t.split(":"):
        if _SEP.search(part):
            bits = _SEP.split(part.split(" - ")[0], maxsplit=1)
            if len(bits) == 2:
                a, b = (x.strip(" ?.-") for x in bits)
                if a and b:
                    return (a, b)
    return None


CLUB_NOISE = {"fc", "cf", "afc", "sc", "ssc", "ac", "as", "cd", "ud", "sv", "fk", "ca", "cr", "rc", "bk", "club"}
GENERIC_LAST = {"city", "united", "town", "club", "state", "university", "athletic", "fc"}

SOCCER_GROUPS = """
manchester city|man city|mancity; manchester united|man united|man utd|man u; tottenham hotspur|tottenham|spurs;
wolverhampton wanderers|wolves|wolverhampton; newcastle united|newcastle; west ham united|west ham;
brighton and hove albion|brighton|brighton hove albion; nottingham forest|forest|notts forest; leeds united|leeds;
leicester city|leicester; crystal palace|palace; aston villa|villa; sheffield united|sheffield utd;
bayern munich|bayern|bayern munchen|fc bayern munchen; borussia dortmund|dortmund|bvb;
borussia monchengladbach|gladbach|monchengladbach; bayer leverkusen|leverkusen|bayer 04 leverkusen;
rb leipzig|leipzig|rasenballsport leipzig; eintracht frankfurt|frankfurt;
paris saint germain|psg|paris sg|paris saint-germain; olympique marseille|marseille|olympique de marseille;
olympique lyonnais|lyon|olympique lyon; as monaco|monaco; inter milan|inter|internazionale|internazionale milano;
ac milan|milan; juventus|juve; atletico madrid|atletico|atl madrid|club atletico de madrid;
athletic club|athletic bilbao|athletic; real sociedad|sociedad; real betis|betis;
sporting cp|sporting lisbon|sporting lisboa|sporting; benfica|sl benfica; fc porto|porto; psv eindhoven|psv;
feyenoord rotterdam|feyenoord; ajax amsterdam|ajax; celtic glasgow|celtic; rangers glasgow|rangers;
galatasaray sk|galatasaray; fenerbahce sk|fenerbahce; besiktas jk|besiktas; olympiacos|olympiakos;
la galaxy|galaxy; new york red bulls|ny red bulls|red bulls; inter miami|inter miami cf; los angeles fc|lafc;
seattle sounders|sounders
"""


@lru_cache(maxsize=None)      # [SPEED]
def _tn(name: str):
    """(base string, frozenset of '(tag)' qualifiers) e.g. 'Titans (SA)' -> ('titans', {'sa'})."""
    raw = normalize(name)
    tags = frozenset(x.strip().lower() for x in re.findall(r"\(([^)]*)\)", raw))
    return _base(raw), tags


_SOCCER_READY = False


def _ensure_aliases() -> None:
    """Register soccer club aliases once, on first use (needs normalize(), defined further down)."""
    global _SOCCER_READY
    if _SOCCER_READY:
        return
    _SOCCER_READY = True
    for group in SOCCER_GROUPS.replace("\n", " ").split(";"):
        names = [_tn(x.strip())[0] for x in group.split("|") if x.strip()]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if names[i] != names[j]:
                    _add_equiv(names[i], names[j])


@lru_cache(maxsize=None)      # [SPEED]
def team_equiv(a: str, b: str) -> bool:
    _ensure_aliases()
    (ba, pa), (bb, pb) = _tn(a), _tn(b)
    if pa != pb or not ba or not bb:
        return False                       # 'Titans (SA)' is NOT 'Titans'
    if ba == bb or bb in _EQUIV.get(ba, ()) or ba in _EQUIV.get(bb, ()):
        return True
    ta, tb = ba.split(), bb.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) == 1 and long_[-1] == short[0] and short[0] not in GENERIC_LAST:
        return True                        # 'Vacherot' ~ 'Valentin Vacherot', 'Titans' ~ 'Tennessee Titans'
    return fuzz.ratio(ba, bb) >= 88        # brown / browns, small typos


@lru_cache(maxsize=None)      # [SPEED]
def team_keys(name: str) -> set[str]:
    _ensure_aliases()
    base, _ = _tn(name)
    keys = {w for w in base.split() if len(w) >= 3}
    for eq in _EQUIV.get(base, ()):
        keys |= {w for w in eq.split() if len(w) >= 3}
    return keys


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


def same_matchup(ta, tb) -> bool:
    return ((team_equiv(ta[0], tb[0]) and team_equiv(ta[1], tb[1]))
            or (team_equiv(ta[0], tb[1]) and team_equiv(ta[1], tb[0])))


@lru_cache(maxsize=None)      # [SPEED]
def subject_equiv(a: str, b: str) -> bool:
    strip = lambda x: re.sub(r"\s+(wins?|to win)\b.*$", "", x or "", flags=re.I).strip()
    sa, sb = _base(strip(a)), _base(strip(b))
    if sa in DRAW_WORDS or sb in DRAW_WORDS:
        return sa in DRAW_WORDS and sb in DRAW_WORDS          # draw <-> tie
    return team_equiv(strip(a), strip(b))


SOCCER_PROP_SKIP = re.compile(r"both teams|btts|exact score|clean sheet|goal ?scorer|anytime|corners?|cards?|"
                              r"shots|total goals|\bover\b|\bunder\b|\+\d|-\d", re.I)


def poly_game_subject(q: str, teams) -> str | None:
    """'Will Arsenal FC win on 2026-10-18?' -> 'Arsenal FC';  '... end in a draw?' -> 'Draw'."""
    if re.search(r"\bdraw\b|\btie\b", q, re.I):
        return "Draw"
    bq = _base(q)
    hits = [(bq.find(_base(t)), t) for t in teams if _base(t) and bq.find(_base(t)) >= 0]
    return min(hits)[1] if hits else None


@lru_cache(maxsize=None)      # [SPEED]
def qualifiers(text: str) -> set:
    """Sub-market markers that must agree: ('game','4'), ('set','2'), ('map','1'), ('half','1')..."""
    t = normalize(text).lower()
    q = set(re.findall(rf"\b({_QUAL_WORDS})\s*#?(\d{{1,2}})\b", t))
    q |= {(w, _ORD[o]) for o, w in re.findall(
        rf"\b(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth)\s+({_QUAL_WORDS})\b", t)}
    return q


# ----------------------------------------------------------------------------
# Multi-choice / ladder helpers: subjects ("France", "$100,000"), event "question types"
# ----------------------------------------------------------------------------
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


def _poly_outcomes(m: dict) -> list[str]:
    o = m.get("outcomes")
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except ValueError:
            o = []
    return [str(x) for x in (o or [])]


def poly_option_subjects(markets: list[dict]) -> dict[str, str]:
    """market id -> option label. Uses Polymarket's groupItemTitle, else infers it from the questions."""
    yn = [m for m in markets
          if [x.lower() for x in _poly_outcomes(m)] == ["yes", "no"] and not m.get("closed")]
    inferred = infer_subjects([str(m.get("question", "")) for m in yn])
    out = {}
    for m, inf in zip(yn, inferred):
        label = str(m.get("groupItemTitle") or "").strip()
        out[str(m.get("id"))] = label or inf
    return out


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

VS_RE = re.compile(r"\bvs\.?\b|\bv\.\s", re.I)
GAME_SKIP_RE = re.compile(r"spread|o/u|over/under|\btotals?\b|handicap|\([+-]?\d", re.I)


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


def _poly_events(client: httpx.Client, extra: dict, pages: int):
    use_order = True            # sort by 24h volume; dropped automatically if the API rejects it
    for page in range(pages):
        events = None
        for _ in range(2):
            params = {"limit": 100, "offset": page * 100, "active": "true",
                      "closed": "false", **extra}
            if use_order:
                params["order"] = "volume24hr"
                params["ascending"] = "false"
            try:
                r = client.get(POLY_URL, params=params)
                r.raise_for_status()
                events = r.json()
                break
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code == 422 and use_order:
                    use_order = False       # retry this page without sorting
                    continue
                print(f"   Polymarket {extra or 'generic'} page {page} failed: "
                      f"{str(e).splitlines()[0][:120]}")
                return
        if not events:
            return
        yield from events


def fetch_polymarket(client: httpx.Client, pages: int = POLY_PAGES) -> list[Market]:
    """
    Single-question markets only (see ALLOW_LADDERS / ALLOW_MULTI_CHOICE), pulled from the top-volume
    list PLUS per-tag lists so sports/politics/economy/crypto are all covered.
    Team-vs-team moneylines are split into "Will <team> win?" Yes/No markets.
    """
    out: list[Market] = []
    stats = {"events": 0, "markets": 0, "multi_choice_skipped": 0, "games": 0,
             "not_yes_no": 0, "no_book": 0}
    seen: set = set()
    passes = [("top-volume", {}, pages)] + [
        (f"tag:{t}", {"tag_slug": t}, POLY_PAGES_PER_TAG) for t in POLY_TAG_SLUGS
    ]
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:           # [SPEED] pages inside one pass stay sequential
        futures = [ex.submit(lambda e=extra, n=n_pages: list(_poly_events(client, e, n))) for _, extra, n_pages in passes]
        fetched = [f.result() for f in futures]
    for (label, extra, n_pages), pass_events in zip(passes, fetched):   # same order as before -> same dedup result
        new_events = 0
        for ev in pass_events:
            eid = ev.get("id") or ev.get("slug")
            if eid in seen:
                continue
            seen.add(eid)
            new_events += 1
            stats["events"] += 1
            mk = ev.get("markets") or []
            neg = bool(ev.get("negRisk") or ev.get("enableNegRisk"))
            strict_ok = len(mk) == 1 or (len(mk) == 2 and neg)             # ONE question (or 2-sided game)
            opt_ok = (ALLOW_MULTI_CHOICE and neg) or (ALLOW_LADDERS and not neg)   # lists / ladders -> per option
            three_way = len(mk) >= 2 and any(
                re.search(r"\bdraw\b", str(m.get("question", "")), re.I) for m in mk)
            if three_way:                       # win / draw / win soccer events: handled as games below
                strict_ok = opt_ok = False
            slug = ev.get("slug") or ""
            ev_teams = parse_teams(ev.get("title", ""))
            ev_subjects = poly_option_subjects(mk) if (opt_ok and not strict_ok) else {}
            for m in mk:
                stats["markets"] += 1
                if m.get("closed") or m.get("active") is False:
                    continue
                outcomes = m.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except ValueError:
                        outcomes = []
                outcomes = [str(o) for o in (outcomes or [])]
                q = m.get("question") or ev.get("title", "")
                bid = to_prob(m.get("bestBid"))
                ask = to_prob(m.get("bestAsk"))
                base = dict(
                    venue="POLYMARKET",
                    close_time=parse_dt(m.get("endDate") or ev.get("endDate")),
                    url=f"https://polymarket.com/event/{slug}" if slug else "",
                    group=ev.get("title", ""),
                )
                low = [o.lower() for o in outcomes]
                if (low == ["yes", "no"] and ev_teams and len(mk) in (2, 3)
                        and not GAME_SKIP_RE.search(q) and not SOCCER_PROP_SKIP.search(q)):
                    subj = poly_game_subject(q, ev_teams)       # one match = one game (win / draw / win)
                    if subj:
                        g_rate, g_cat = poly_fee_info(ev, m, True)
                        stats["match_outcomes"] = stats.get("match_outcomes", 0) + 1
                        out.append(Market(market_id=str(m.get("id")), title=q, yes_bid=bid, yes_ask=ask,
                                          subject=subj, kind="game", teams=ev_teams,
                                          fee_rate=g_rate, fee_cat=g_cat, **base))
                        continue
                if low == ["yes", "no"]:
                    if strict_ok:
                        if bid is None or ask is None:
                            stats["no_book"] += 1
                        rate, cat = poly_fee_info(ev, m, False)
                        out.append(Market(market_id=str(m.get("id")), title=q, yes_bid=bid,
                                          yes_ask=ask, fee_rate=rate, fee_cat=cat, **base))
                    elif opt_ok:                # one option of a multi-choice list / one rung of a ladder
                        stats["options"] = stats.get("options", 0) + 1
                        rate, cat = poly_fee_info(ev, m, False)
                        out.append(Market(market_id=str(m.get("id")), title=q, yes_bid=bid, yes_ask=ask,
                                          subject=ev_subjects.get(str(m.get("id")), ""), kind="option",
                                          event_id=str(eid), fee_rate=rate, fee_cat=cat, **base))
                    else:
                        stats["multi_choice_skipped"] += 1
                elif (len(outcomes) == 2 and VS_RE.search(q) and not GAME_SKIP_RE.search(q)
                      and parse_teams(q)):
                    stats["games"] += 1     # bestBid/bestAsk quoted for FIRST outcome (assumption)
                    g_rate, g_cat = poly_fee_info(ev, m, True)
                    for i, o in enumerate(outcomes):
                        if i == 0:
                            b_, a_ = bid, ask
                        else:
                            b_ = None if ask is None else round(1.0 - ask, 4)
                            a_ = None if bid is None else round(1.0 - bid, 4)
                        out.append(Market(market_id=f"{m.get('id')}:{i}", title=f"Will {o} win? {q}",
                                          yes_bid=b_, yes_ask=a_, subject=o, kind="game",
                                          teams=parse_teams(q), fee_rate=g_rate, fee_cat=g_cat, **base))
                else:
                    stats["not_yes_no"] += 1
        print(f"   [poly pass {label}] +{new_events} new events")
    print(f"   [poly stats] {stats}")
    return out


def _kalshi_price(m: dict, field: str) -> float | None:
    """Kalshi returns cents (int) and/or '<field>_dollars' strings depending on API version."""
    dollars = to_prob(m.get(f"{field}_dollars"))
    if dollars is not None:
        return dollars
    cents = m.get(field)
    if isinstance(cents, (int, float)) and 0 <= cents <= 100:
        return cents / 100.0
    return None


def _kalshi_series_events(client: httpx.Client, ticker: str) -> list[dict]:
    try:
        r = client.get(KALSHI_URL, params={"series_ticker": ticker, "status": "open",
                                           "with_nested_markets": "true", "limit": 200})
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception:
        return []


def _kalshi_collect_events(client: httpx.Client) -> list[dict]:
    events: dict[str, dict] = {}

    # (1) known game-winner series (cheap, one request each)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:           # [SPEED]
        series_results = list(ex.map(lambda t_: _kalshi_series_events(client, t_), KALSHI_SERIES_HINTS))
    for t, evs in zip(KALSHI_SERIES_HINTS, series_results):
        for ev in evs:
            events[ev.get("event_ticker", "")] = ev
        print(f"   [kalshi series {t}] {len(evs)} events")

    # (2) generic scan of every open event that still has an open market
    cursor: str | None = None
    now_ts = int(time.time())
    for page in range(KALSHI_PAGES):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true",
                  "min_close_ts": now_ts}
        if cursor:
            params["cursor"] = cursor
        try:
            r = client.get(KALSHI_URL, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"   Kalshi page {page} failed: {str(e).splitlines()[0][:120]}")
            break
        for ev in data.get("events", []):
            events.setdefault(ev.get("event_ticker", ""), ev)
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.15)
    print(f"   [kalshi] {len(events)} unique events collected ({page + 1} pages scanned)")
    return list(events.values())


def fetch_kalshi(client: httpx.Client, pages: int = KALSHI_PAGES) -> list[Market]:
    """Same admission rule as Polymarket: 1-question events (+ 2-sided mutually exclusive games)."""
    out: list[Market] = []
    stats = {"events": 0, "markets": 0, "multi_choice_events": 0, "inactive": 0, "no_book": 0, "options": 0}
    for ev in _kalshi_collect_events(client):
        stats["events"] += 1
        ev_title = ev.get("title", "")
        ev_ticker = ev.get("event_ticker", "")
        mk = ev.get("markets") or []
        mutex = bool(ev.get("mutually_exclusive"))
        ev_teams = parse_teams(ev_title)
        has_draw = len(mk) >= 2 and any(
            re.search(r"\b(tie|draw)\b", f"{m.get('yes_sub_title') or ''} {m.get('title') or ''}", re.I)
            for m in mk)
        three_way = ev_teams is not None and (has_draw or (len(mk) == 3 and mutex))   # team / draw / team
        if ev_ticker.upper().startswith("KXMVE") or (has_draw and not three_way):
            stats["multi_choice_events"] += 1
            continue
        if three_way or (len(mk) == 2 and mutex):
            kind = "game"                                                # one match / one 2-sided game
        elif len(mk) == 1:
            kind = "binary"                                              # ONE question
        elif (ALLOW_MULTI_CHOICE and mutex) or (ALLOW_LADDERS and not mutex):
            kind = "option"                                              # list of options / ladder of rungs
        else:
            stats["multi_choice_events"] += 1
            continue
        opt_subjects: dict = {}
        if kind == "option":
            subs = [m.get("yes_sub_title") or "" for m in mk]
            if all(subs) and len(set(subs)) == len(subs):
                opt_subjects = {m.get("ticker", ""): sb for m, sb in zip(mk, subs)}
            else:
                inferred = infer_subjects([str(m.get("title") or "") for m in mk])
                opt_subjects = {m.get("ticker", ""): sb for m, sb in zip(mk, inferred)}

        for m in mk:
            stats["markets"] += 1
            if m.get("status") not in (None, "active", "open"):
                stats["inactive"] += 1
                continue
            bid = _kalshi_price(m, "yes_bid")
            ask = _kalshi_price(m, "yes_ask")
            if bid is None or ask is None:
                stats["no_book"] += 1

            sub = m.get("yes_sub_title") or ""
            if kind == "option":
                stats["options"] += 1
            parts: list[str] = []
            for t in (ev_title, m.get("title"), sub):
                if t and t.lower() not in " ".join(parts).lower():
                    parts.append(t)

            out.append(
                Market(
                    venue="KALSHI",
                    market_id=m.get("ticker", ""),
                    title=" - ".join(parts),
                    close_time=parse_dt(m.get("close_time")),
                    yes_bid=bid,
                    yes_ask=ask,
                    url="",
                    series_ticker=ev.get("series_ticker") or ev_ticker.split("-")[0],
                    event_ticker=ev_ticker,
                    group=ev_title,
                    subject=opt_subjects.get(m.get("ticker", ""), sub) if kind == "option" else sub,
                    kind=kind,
                    event_id=ev_ticker if kind == "option" else "",
                    teams=ev_teams if kind == "game" else None,
                    alt_time=parse_dt(m.get("expected_expiration_time")),
                )
            )
    print(f"   [kalshi stats] {stats}")
    return out


# ----------------------------------------------------------------------------
# Text normalization + hard constraints
# ----------------------------------------------------------------------------
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

# ----------------------------------------------------------------------------
# Multi-choice / ladder matching: match the EVENTS (questions) first, then option <-> same option
# ----------------------------------------------------------------------------
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



# ----------------------------------------------------------------------------
# [SPEED] Path 2 (single questions): same shortlist as before, found much faster
# ----------------------------------------------------------------------------
def _ts(dt: datetime | None) -> float:
    return dt.timestamp() if dt else float("nan")


def path2_candidates(poly: list[Market], kalshi: list[Market], k_clean: list[str],
                     cands: list, rejected: list, reasons: dict) -> None:
    """Fuzzy shortlist for every non-game Polymarket market (appends to cands / rejected / reasons).

    Same rule as the old per-question process.extract loop (top CANDIDATES_PER_POLY by token_set_ratio >= MIN_SCORE-10,
    then the average-score filter and reject_reason), with two changes that do not alter which pairs are valid:
      1. Kalshi markets whose close dates can never pass reject_reason's 'close dates differ' test (> MAX_CLOSE_GAP_DAYS
         from every Polymarket date) are never scored. The old loop scored them, filled its top-25 with them
         (e.g. the same 'Highest temperature in X' question for ten other days) and threw them away afterwards.
      2. Questions are scored in blocks with rapidfuzz.cdist on all CPU cores instead of one at a time on one core.
    """
    W = MAX_CLOSE_GAP_DAYS * 86400.0
    cutoff = MIN_SCORE - 10
    kc = np.array([_ts(k.close_time) for k in kalshi])
    ka = np.array([_ts(k.alt_time) for k in kalshi])
    k_notime = np.isnan(kc) & np.isnan(ka)              # no date at all -> reject_reason never rejects on dates
    k_flat = np.array([k.kind != "option" for k in kalshi], dtype=bool)

    def times(m: Market) -> list[float]:
        return [t for t in (_ts(m.close_time), _ts(m.alt_time)) if t == t]

    # a Polymarket option is matched to Kalshi options in path 3; here only to single-question Kalshi markets
    todo = [pi for pi, p in enumerate(poly) if not (p.kind == "game" and p.teams)]
    for is_option in (False, True):
        group = [pi for pi in todo if (poly[pi].kind == "option") == is_option]
        pool_mask = k_flat if is_option else np.ones(len(kalshi), dtype=bool)
        dated = sorted((pi for pi in group if times(poly[pi])), key=lambda pi: min(times(poly[pi])))
        undated = [pi for pi in group if not times(poly[pi])]       # no date -> compatible with everything
        blocks: list[tuple[list[int], np.ndarray]] = []
        cur: list[int] = []
        lo = hi = 0.0
        for pi in dated:
            t_min, t_max = min(times(poly[pi])), max(times(poly[pi]))
            if cur and (len(cur) >= P2_CHUNK or t_max - lo > P2_SPAN_DAYS * 86400.0):
                blocks.append((cur, np.flatnonzero(pool_mask & ((kc >= lo - W) & (kc <= hi + W)
                                                               | (ka >= lo - W) & (ka <= hi + W) | k_notime))))
                cur = []
            if not cur:
                lo, hi = t_min, t_max
            lo, hi = min(lo, t_min), max(hi, t_max)
            cur.append(pi)
        if cur:
            blocks.append((cur, np.flatnonzero(pool_mask & ((kc >= lo - W) & (kc <= hi + W)
                                                           | (ka >= lo - W) & (ka <= hi + W) | k_notime))))
        pool_all = np.flatnonzero(pool_mask)
        for i in range(0, len(undated), P2_CHUNK):
            blocks.append((undated[i:i + P2_CHUNK], pool_all))

        for chunk, cand_idx in blocks:
            if cand_idx.size == 0:
                continue
            p_clean = [clean_words(poly[pi].title) for pi in chunk]
            choices = [k_clean[j] for j in cand_idx]
            M = process.cdist(p_clean, choices, scorer=fuzz.token_set_ratio, dtype=np.float32,
                              score_cutoff=cutoff, workers=P2_WORKERS)
            tc, ta = kc[cand_idx], ka[cand_idx]
            nt = k_notime[cand_idx]
            for r, pi in enumerate(chunk):
                row = M[r]
                ok = row >= cutoff
                pt = times(poly[pi])
                if pt:                                                  # exact twin of reject_reason's date test
                    near = np.zeros(cand_idx.size, dtype=bool)
                    for t in pt:
                        near |= (np.abs(tc - t) <= W) | (np.abs(ta - t) <= W)
                    ok &= near | nt
                hit = np.flatnonzero(ok)
                if hit.size == 0:
                    continue
                hit = hit[np.argsort(-row[hit], kind="stable")][:CANDIDATES_PER_POLY]   # best first, ties by index
                p = poly[pi]
                pc = p_clean[r]
                for j in hit:
                    ki = int(cand_idx[j])
                    score = (fuzz.token_set_ratio(pc, k_clean[ki]) + fuzz.token_sort_ratio(pc, k_clean[ki])) / 2
                    if score < MIN_SCORE:
                        reasons["score too low"] += 1
                        continue
                    why = reject_reason(p, kalshi[ki], score)
                    if why:
                        reasons[why] += 1
                        rejected.append((score, why, p, kalshi[ki]))
                    else:
                        cands.append((score, pi, ki))


# ----------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------
def find_matches(poly: list[Market], kalshi: list[Market], debug: bool = True,
                 event_report: list | None = None):
    """
    Returns (pairs, rejected).
      pairs    = [(poly, kalshi, score, inverted, warnings)]   inverted = one side is a negation
      rejected = near-misses [(score, reason, poly, kalshi)] for the 'Not matched' sheet
    event_report (optional list) is filled with the multi-choice / ladder events that stayed unpaired.
    Two paths:
      * game vs game  -> matched on the TEAMS (both teams, any order, city/nickname aware) + which side YES is
      * everything else -> fuzzy title shortlist, then hard constraints
    """
    cands: list[tuple[float, int, int]] = []
    rejected: list[tuple[float, str, Market, Market]] = []
    reasons: dict[str, int] = defaultdict(int)
    _t0 = time.perf_counter()

    def _lap(label: str) -> None:                                     # [TIMING] where does the time go?
        nonlocal _t0
        now = time.perf_counter()
        print(f"   [time] {label}: {now - _t0:.1f}s")
        _t0 = now

    # ---- path 1: games --------------------------------------------------------------
    index: dict[str, set] = defaultdict(set)
    for ki, k in enumerate(kalshi):
        if k.kind == "game" and k.teams:
            for name in k.teams:
                for key in team_keys(name):
                    index[key].add(ki)
    for pi, p in enumerate(poly):
        if p.kind != "game" or not p.teams:
            continue
        pool: set = set()
        for name in p.teams:
            for key in team_keys(name):
                pool |= index.get(key, set())
        for ki in pool:
            k = kalshi[ki]
            why = reject_reason(p, k)
            if why is None:
                cands.append((100.0, pi, ki))
            else:
                reasons[why] += 1
                if not why.startswith("different matchup"):
                    rejected.append((95.0, why, p, k))

    _lap(f"path 1 games ({sum(1 for p in poly if p.kind == 'game')} poly games)")
    # ---- path 2: everything else (fuzzy) ------------------------------------------------
    k_clean = [clean_words(k.title) for k in kalshi]
    path2_candidates(poly, kalshi, k_clean, cands, rejected, reasons)      # [SPEED] was a 1-core process.extract loop

    _lap(f"path 2 fuzzy single questions ({len(poly)} poly x {len(kalshi)} kalshi markets)")
    # ---- path 3: multi-choice lists and ladders: match the events, then option <-> same option ----
    opt_cands, opt_warn = match_option_events(poly, kalshi, rejected, reasons, event_report)
    cands.extend(opt_cands)

    _lap("path 3 option events")
    # ---- [GUARD] semantic guard: drop every candidate that is NOT the same contract -----------------------
    # Runs BEFORE the 1:1 assignment below, so a rejected pair can never "use up" the right counterpart.
    guard_review: dict[tuple[int, int], list[str]] = {}
    guarded: list[tuple[float, int, int]] = []
    for score, pi, ki in cands:
        p, k = poly[pi], kalshi[ki]
        kind = "game" if (p.kind == "game" and k.kind == "game") else "binary"
        verdict, why = cg.check_pair(p.title, k.title, kind)
        if verdict == "reject":
            reasons["guard: " + why[0].split(" [")[0]] += 1
            rejected.append((score, "guard: " + why[0], p, k))
            continue
        guard_review[(pi, ki)] = why if verdict == "review" else []
        guarded.append((score, pi, ki))
    cands = guarded

    _lap(f"semantic guard ({len(cands) + sum(v for k_, v in reasons.items() if k_.startswith('guard:'))} candidates)")
    cands.sort(reverse=True)
    used_p: set[int] = set()
    used_k: set[int] = set()
    pairs = []
    for score, pi, ki in cands:
        if pi in used_p or ki in used_k:
            continue
        used_p.add(pi)
        used_k.add(ki)
        p, k = poly[pi], kalshi[ki]
        inverted = is_negated(p.title) != is_negated(k.title)
        warns = list(opt_warn.get((pi, ki)) or soft_warnings(p, k))
        warns += ["WARN: " + w for w in guard_review.get((pi, ki), [])]     # [GUARD] 'review' verdicts -> status CHECK
        pairs.append((p, k, score, inverted, warns))

    _lap("1:1 assignment")
    rejected.sort(key=lambda r: -r[0])
    if debug:
        print(f"   candidate rejections: {dict(reasons)}")
        print(f"   matched pairs: {len(pairs)}  (game pairs: {sum(1 for x in pairs if x[0].kind == 'game')}, "
              f"option pairs: {sum(1 for x in pairs if x[0].kind == 'option')})")
    return pairs, rejected[:MAX_NEAR_MISSES]


# ----------------------------------------------------------------------------
# Arbitrage math (buy both legs at the ASK, hold to resolution)
# ----------------------------------------------------------------------------
def kalshi_fee(price: float, contracts: int = ORDER_CONTRACTS) -> float:
    """Kalshi taker fee PER CONTRACT: ceil_to_cent(0.07*C*P*(1-P)) / C."""
    if price <= 0 or price >= 1:
        return 0.0
    total = KALSHI_FEE_COEF * contracts * price * (1.0 - price)
    return math.ceil(round(total * 100, 6)) / 100.0 / contracts


def poly_fee(price: float, rate: float) -> float:
    """Polymarket taker fee PER SHARE = rate * p * (1-p)   (same for YES at p and NO at 1-p)."""
    return rate * price * (1.0 - price)


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


# ----------------------------------------------------------------------------
# Output: arbitrage-only Excel report
# ----------------------------------------------------------------------------
def _fmt_px(bid: float | None, ask: float | None) -> str:
    return "n/a" if bid is None or ask is None else f"{bid:.3f} / {ask:.3f}"


def _yes_no_prices(m: Market) -> tuple[str, str]:
    """('YES bid / ask', 'NO bid / ask'). NO bid = 1 - YES ask, NO ask = 1 - YES bid."""
    yes = _fmt_px(m.yes_bid, m.yes_ask)
    no = "n/a" if m.yes_bid is None or m.yes_ask is None else _fmt_px(1 - m.yes_ask, 1 - m.yes_bid)
    return yes, no


def kalshi_event_url(client: httpx.Client, series: str, event: str, cache: dict) -> str:
    """https://kalshi.com/markets/<series>/<slug>/<event>, all lowercase. The slug is the series
    title slugified (e.g. 'NFL Game' -> 'nfl-game'), fetched once per series."""
    if series not in cache:
        slug = "market"
        try:
            r = client.get(f"{KALSHI_URL.rsplit('/', 1)[0]}/series/{series}")
            r.raise_for_status()
            title = (r.json().get("series") or {}).get("title") or ""
            if title:
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "market"
        except Exception:
            pass
        cache[series] = slug
    return f"https://kalshi.com/markets/{series.lower()}/{cache[series]}/{event.lower()}"


def build_row(arb: dict, p: Market, k: Market, score: float, inverted: bool, flags: list[str]) -> dict:
    """Turn one opportunity into a flat report row, with the position sizing as % of capital."""
    poly_is_first = arb["strategy"].startswith("YES Polymarket")
    if poly_is_first:                       # YES Polymarket + NO Kalshi
        poly_px, kal_px = arb["leg1"], arb["leg2"]
        poly_view_side, kal_side = "YES", "NO"
    else:                                   # YES Kalshi + NO Polymarket
        kal_px, poly_px = arb["leg1"], arb["leg2"]
        poly_view_side, kal_side = "NO", "YES"
    # if the Polymarket question is phrased as a negation, its YES is the other side's NO
    poly_side = poly_view_side if not inverted else ("NO" if poly_view_side == "YES" else "YES")

    poly_cost = poly_px + arb["poly_fee"]            # price + fee, per contract
    kal_cost = kal_px + arb["kalshi_fee"]
    total = poly_cost + kal_cost                      # capital needed per $1 of guaranteed payout
    net = arb["net_edge"]

    p_yes, p_no = _yes_no_prices(p)
    k_yes, k_no = _yes_no_prices(k)
    ok = not any(f == "CHECK-RULES" or f.startswith("WARN") or f.endswith("wide-spread")
                 or f.endswith("no-book") or f == "dust-leg" for f in flags)       # [GUARD] dust-leg added
    return {
        "status": "OK" if ok else "CHECK: " + ", ".join(flags),
        "net_per_unit": net,                          # $ profit per $1 payout (after fees)
        "roi": net / total,                           # return on capital deployed
        "profit_on_capital": CAPITAL * net / total,
        "poly_pct": poly_cost / total,
        "kal_pct": kal_cost / total,
        "action": f"Buy {poly_side} on Polymarket @ {poly_px:.3f}  +  Buy {kal_side} on Kalshi @ {kal_px:.3f}",
        "k_q": k.title, "k_url": k.url, "k_ticker": k.market_id,
        "k_yes": k_yes, "k_no": k_no, "k_series": k.series_ticker, "k_event": k.event_ticker,
        "p_q": p.title, "p_url": p.url, "p_yes": p_yes, "p_no": p_no,
        "gross": arb["gross_edge"], "fees_poly": arb["poly_fee"], "fees_kal": arb["kalshi_fee"],
        "poly_cat": p.fee_cat or "n/a",
        "closes": (p.close_time or k.close_time),
        "score": score, "ok": ok,
    }


def build_all_row(arb: dict, p: Market, k: Market, score: float, inverted: bool, flags: list[str]) -> dict:
    """One line per matched pair (arbitrage or not) for the 'All matches' sheet."""
    p_yes, _ = _yes_no_prices(p)
    k_yes, _ = _yes_no_prices(k)
    net = arb["net_edge"]
    kind = "multi-choice / ladder option" if "option" in (p.kind, k.kind) else (
        "game" if p.kind == "game" else "single question")
    return {"net": None if net == float("-inf") else net, "p_q": p.title, "p_yes": p_yes, "p_url": p.url,
            "k_q": k.title, "k_ticker": k.market_id, "k_yes": k_yes, "score": score, "type": kind,
            "flags": ", ".join(flags) if flags else "OK"}


HEADERS_XLSX = [
    ("#", 5), ("Status", 18), ("Net profit per $1 payout", 14), ("Return on capital", 11),
    (f"Profit on ${CAPITAL:,.0f}", 12), ("% capital → Polymarket", 12), ("% capital → Kalshi", 12),
    ("What to buy", 46),
    ("Kalshi question", 52), ("Kalshi link", 12), ("Kalshi market ticker", 26), ("Kalshi YES bid/ask", 15), ("Kalshi NO bid/ask", 15),
    ("Polymarket question", 52), ("Polymarket link", 14), ("Poly YES bid/ask", 15), ("Poly NO bid/ask", 15),
    ("Gross edge", 10), ("Fees Polymarket", 11), ("Fees Kalshi", 11), ("Poly fee category", 14),
    ("Closes", 12), ("Match score", 9),
]

NOTES = [
    "HOW TO READ THIS FILE",
    "Net profit per $1 payout: guaranteed profit per contract pair AFTER both venues' taker fees (buy one side on each venue, one leg always pays $1).",
    "Return on capital: net profit / total money spent on the pair (prices + fees).",
    "% capital: split your capital between the two legs in these proportions so both legs buy the same number of contracts.",
    "Rows are sorted: 'OK' rows first, then rows flagged CHECK; inside each group the best return on capital first.",
    "CHECK = huge edge (usually the two questions resolve differently), wide bid-ask spread, missing order book, a dust-priced leg (<1c), or a guard warning.",
    "The profit is only guaranteed if BOTH questions resolve identically. contract_guard.py rejects pairs that differ in a number / rank / threshold wording (at least vs above vs exactly) / calendar day / weather station / scope (US vs global) / game sub-market (1H, halftime, race to N); 'WARN' rows passed those checks but share little wording - read both rules.",
    "Kalshi link = the event page (kalshi.com/markets/<series>/<slug>/<event ticker>). The 'Kalshi market ticker' column identifies the exact contract inside that event.",
    "Prices are best bid/ask of the YES token. NO ask = 1 - YES bid, NO bid = 1 - YES ask.",
    "Sheet 'All matches' lists EVERY pair the matcher accepted (arbitrage or not); sheet 'Not matched' lists the closest pairs it rejected and why (including 'guard: ...' rejections).",
    "Multi-choice / ladder markets: the two EVENTS (questions) are matched first, then each option is paired with the SAME option on the other platform (France<->France, $100k rung<->$100k rung). Each pair is an independent Yes/No bet. The INFO flag shows which two events were paired - check they resolve the same way.",
    "Sheet 'Unpaired events' lists every multi-choice / ladder event (either platform) that found no counterpart, with its closest candidate on the other platform and why it was not paired. High similarity + 'no option matched' usually means a naming problem (add an alias); low similarity usually means the event exists on one platform only. Events dropped before matching (past the load limits, non Yes/No outcomes) are NOT listed - see the run log stats.",
    "WARN = accepted but names only partly match (e.g. 'Benjamin Netanyahu' vs 'Netanyahu'), or the guard found low wording overlap: confirm it is the same person/entity/question.",
    "NOT included: order-book depth (size you can actually fill), slippage, USDC/bridging costs, taxes. Gamma's best bid/ask can lag (especially live games): re-check both order books before trading.",
    "ALWAYS read both venues' resolution rules before trading: matching is automatic and can be wrong.",
    "Fee rates: Kalshi 0.07*P*(1-P) rounded up per order; Polymarket category rate * p*(1-p). Verify they are current.",
]


def write_report(rows: list[dict], path: Path, all_rows: list[dict] | None = None,
                 near: list | None = None, unpaired: list | None = None) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:                       # fallback: plain CSV
        import csv
        path = path.with_suffix(".csv")
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([h for h, _ in HEADERS_XLSX])
            for i, r in enumerate(rows, 1):
                w.writerow([i, r["status"], f"{r['net_per_unit']:.4f}", f"{r['roi']:.4%}",
                            f"{r['profit_on_capital']:.2f}", f"{r['poly_pct']:.1%}", f"{r['kal_pct']:.1%}",
                            r["action"], r["k_q"], r["k_url"], r["k_ticker"], r["k_yes"], r["k_no"],
                            r["p_q"], r["p_url"], r["p_yes"], r["p_no"], f"{r['gross']:.4f}",
                            f"{r['fees_poly']:.4f}", f"{r['fees_kal']:.4f}", r["poly_cat"],
                            r["closes"].strftime("%Y-%m-%d") if r["closes"] else "", f"{r['score']:.0f}"])
        return path

    wb = Workbook()
    ws = wb.active
    ws.title = "Arbitrage"
    head_fill = PatternFill("solid", fgColor="1F3864")
    for c, (name, width) in enumerate(HEADERS_XLSX, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[1].height = 44
    ws.freeze_panes = "C2"

    ok_fill = PatternFill("solid", fgColor="E2F0D9")
    chk_fill = PatternFill("solid", fgColor="FFF2CC")
    wrap = Alignment(wrap_text=True, vertical="top")
    link_font = Font(color="0563C1", underline="single")

    for i, r in enumerate(rows, 1):
        vals = [i, r["status"], r["net_per_unit"], r["roi"], r["profit_on_capital"],
                r["poly_pct"], r["kal_pct"], r["action"],
                r["k_q"], "Open on Kalshi", r["k_ticker"], r["k_yes"], r["k_no"],
                r["p_q"], "Open on Polymarket", r["p_yes"], r["p_no"],
                r["gross"], r["fees_poly"], r["fees_kal"], r["poly_cat"],
                r["closes"].strftime("%Y-%m-%d") if r["closes"] else "", round(r["score"])]
        ws.append(vals)
        row = i + 1
        for c in range(1, len(vals) + 1):
            ws.cell(row=row, column=c).alignment = wrap
        ws.cell(row=row, column=2).fill = ok_fill if r["ok"] else chk_fill
        ws.cell(row=row, column=3).number_format = "$0.0000"
        ws.cell(row=row, column=4).number_format = "0.00%"
        ws.cell(row=row, column=5).number_format = "$#,##0.00"
        ws.cell(row=row, column=6).number_format = "0.0%"
        ws.cell(row=row, column=7).number_format = "0.0%"
        for c in (18, 19, 20):
            ws.cell(row=row, column=c).number_format = "$0.0000"
        for col, url in ((10, r["k_url"]), (15, r["p_url"])):
            if url:
                cell = ws.cell(row=row, column=col)
                cell.hyperlink = url
                cell.font = link_font

    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS_XLSX))}{max(2, len(rows) + 1)}"

    def _sheet(title, headers, data, widths, wrap_cols=()):
        sh = wb.create_sheet(title)
        for c, (h, w) in enumerate(zip(headers, widths), 1):
            cell = sh.cell(row=1, column=c, value=h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
            sh.column_dimensions[get_column_letter(c)].width = w
        for row_vals in data:
            sh.append(row_vals)
        for row_cells in sh.iter_rows(min_row=2):
            for cell in row_cells:
                cell.alignment = wrap
        sh.freeze_panes = "A2"
        sh.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(2, len(data) + 1)}"
        return sh

    if all_rows is not None:
        data = [[i, r["net"], r["type"], r["p_q"], r["p_yes"], r["k_q"], r["k_ticker"], r["k_yes"],
                 round(r["score"]), r["flags"]] for i, r in enumerate(all_rows, 1)]
        sh = _sheet("All matches",
                    ["#", "Net edge per $1 (after fees)", "Type", "Polymarket question", "Poly YES bid/ask",
                     "Kalshi question", "Kalshi market ticker", "Kalshi YES bid/ask", "Match score", "Flags"],
                    data, [5, 14, 16, 52, 15, 52, 26, 15, 9, 40])
        for rr in range(2, len(data) + 2):
            sh.cell(row=rr, column=2).number_format = "$0.0000"
        for rr, r in enumerate(all_rows, 2):
            if r["p_url"]:
                sh.cell(row=rr, column=4).hyperlink = r["p_url"]

    if near is not None:
        data = [[round(sc), why, p.title, k.title, k.market_id] for sc, why, p, k in near]
        _sheet("Not matched (near misses)",
               ["Similarity", "Why rejected", "Polymarket question", "Kalshi question", "Kalshi market ticker"],
               data, [10, 40, 55, 55, 26])

    if unpaired is not None:
        _sheet("Unpaired events",
               ["Platform", "Event (multi-choice / ladder)", "# options", "First options",
                "Closest event on the other platform", "Similarity", "Why not paired", "Link / ticker"],
               unpaired, [11, 50, 9, 45, 50, 10, 52, 30])

    notes = wb.create_sheet("Notes")
    notes.column_dimensions["A"].width = 140
    for i, line in enumerate(NOTES, 1):
        c = notes.cell(row=i, column=1, value=line)
        c.alignment = Alignment(wrap_text=True)
        if i == 1:
            c.font = Font(bold=True, size=13)
    wb.save(path)
    return path


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> None:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "arbitrage_run_log.txt"

    buf = io.StringIO()                    # all progress/debug output goes to a log file, not the terminal
    poly: list[Market] = []
    kalshi: list[Market] = []
    pairs: list = []
    near: list = []
    unpaired: list = []
    T0 = time.perf_counter()
    t_fetch = t_match = t_urls = t_write = 0.0
    with contextlib.redirect_stdout(buf):
        with httpx.Client(headers=HEADERS, timeout=15.0, follow_redirects=True) as client:
            def _timed(label, fn):                                          # [SPEED] the two venues are independent
                _t = time.perf_counter()
                res = fn(client)
                print(f"   [time] {label} download: {time.perf_counter() - _t:.1f}s")
                return res
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_poly = ex.submit(_timed, "Polymarket", fetch_polymarket)
                f_kal = ex.submit(_timed, "Kalshi", fetch_kalshi)
                poly, kalshi = f_poly.result(), f_kal.result()
        t_fetch = time.perf_counter() - T0
        if poly and kalshi:
            _t = time.perf_counter()
            pairs, near = find_matches(poly, kalshi, debug=True, event_report=unpaired)
            t_match = time.perf_counter() - _t
            print(f"   [time] matching total: {t_match:.1f}s")
    log_path.write_text(buf.getvalue(), encoding="utf-8")

    if not poly or not kalshi:
        print(f"Could not load market data (Polymarket: {len(poly)}, Kalshi: {len(kalshi)}). "
              f"See {log_path}")
        return

    rows, all_rows = [], []
    for p, k, score, inverted, warns in pairs:
        p_view = invert_market(p) if inverted else p
        arb = best_arb(p_view, k)
        flags = flags_for(p, k, arb, inverted, warns)
        all_rows.append(build_all_row(arb, p, k, score, inverted, flags))
        if arb["net_edge"] > MIN_NET_EDGE:
            rows.append(build_row(arb, p, k, score, inverted, flags))

    if rows:                                                    # exact Kalshi event links (few rows only)
        _t = time.perf_counter()
        cache: dict = {}
        with contextlib.redirect_stdout(buf), httpx.Client(headers=HEADERS, timeout=15.0,
                                                          follow_redirects=True) as client:
            series = sorted({r["k_series"] for r in rows})        # [SPEED] one request per series, in parallel
            with ThreadPoolExecutor(max_workers=FETCH_WORKERS * 2) as ex:
                list(ex.map(lambda s_: kalshi_event_url(client, s_, "x", cache), series))
            for r in rows:
                r["k_url"] = kalshi_event_url(client, r["k_series"], r["k_event"], cache)
        t_urls = time.perf_counter() - _t
        log_path.write_text(buf.getvalue(), encoding="utf-8")

    rows.sort(key=lambda r: (not r["ok"], -r["roi"]))          # clean ones first, best return first
    all_rows.sort(key=lambda r: -(r["net"] if r["net"] is not None else -9))
    unpaired.sort(key=lambda r: -r[5])                          # closest near-misses first
    _t = time.perf_counter()
    path = write_report(rows, out_dir / f"arbitrage_opportunities_{stamp}.xlsx", all_rows, near, unpaired)
    t_write = time.perf_counter() - _t

    summary = (f"{len(pairs)} matched pairs, {len(rows)} with an arbitrage edge "
               f"({sum(r['ok'] for r in rows)} clean), {len(unpaired)} unpaired multi-choice/ladder events")
    if rows:
        print(f"✅ File ready: {path}  ({summary}; top return {rows[0]['roi']:.2%})")
    else:
        print(f"✅ File ready: {path}  ({summary})")
    print(f"   timing: download {t_fetch:.0f}s | matching {t_match:.0f}s | Kalshi links {t_urls:.0f}s | "
          f"Excel {t_write:.1f}s | total {time.perf_counter() - T0:.0f}s   (details in {log_path.name})")


if __name__ == "__main__":
    main()