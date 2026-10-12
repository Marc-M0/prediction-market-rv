"""All tunable settings in one place (match thresholds, page limits, fee rates, ...)."""
from __future__ import annotations




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
