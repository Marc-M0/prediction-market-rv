"""Team / club name knowledge: aliases, 'A vs B' parsing, 'are these the same team' checks."""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata

from rapidfuzz import fuzz

from prediction_market_rv.core.text import normalize


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


def poly_game_subject(q: str, teams) -> str | None:
    """'Will Arsenal FC win on 2026-10-18?' -> 'Arsenal FC';  '... end in a draw?' -> 'Draw'."""
    if re.search(r"\bdraw\b|\btie\b", q, re.I):
        return "Draw"
    bq = _base(q)
    hits = [(bq.find(_base(t)), t) for t in teams if _base(t) and bq.find(_base(t)) >= 0]
    return min(hits)[1] if hits else None
