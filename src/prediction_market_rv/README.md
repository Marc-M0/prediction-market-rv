# `src/prediction_market_rv/` – current version

The modular, maintained version of the scanner.

## Run

From the project root:

```bash
uv run python -m prediction_market_rv
```

It downloads both venues, matches the questions, computes fee-adjusted arbitrage and writes
`arbitrage_opportunities_<date>_<time>.xlsx` and `arbitrage_run_log.txt` to the current folder.
A full scan downloads thousands of markets and can take a long time; progress goes to the log file,
not the terminal.

## Contents

| Path | Role | Details |
|---|---|---|
| `__main__.py` | Lets you run `python -m prediction_market_rv` | |
| `cli.py` | Pipeline: download → match → arbitrage → Excel | |
| `config.py` | **All settings** (thresholds, page limits, fee rates, output dir) | edit here first |
| `clients/` | Download Polymarket / Kalshi data | [README](clients/README.md) |
| `core/` | Shared data model, text helpers, team names, fee maths | [README](core/README.md) |
| `engine/` | Matching, contract guard, arbitrage maths | [README](engine/README.md) |
| `report/` | Excel report writer | [README](report/README.md) |

## Most useful settings (`config.py`)

| Setting | Meaning |
|---|---|
| `POLY_PAGES`, `KALSHI_PAGES` | How many pages of events to download (more = slower, wider coverage) |
| `ALLOW_MULTI_CHOICE`, `ALLOW_LADDERS` | Also scan "Who will win…" lists and date/threshold ladders |
| `MIN_SCORE`, `EVENT_MIN_SCORE` | Fuzzy-match thresholds |
| `MIN_NET_EDGE` | Only report opportunities with net edge (after fees) above this |
| `MAX_SPREAD`, `SUSPICIOUS_EDGE`, `DUST_PRICE` | Warning-flag limits |
| `CAPITAL` | Example bankroll for the "profit on $X" column |
| `OUTPUT_DIR` | Where the Excel file and log are written |
| `USE_CUSTOM_DNS` | Optional DNS workaround (needs `dnspython`; set `False` if not needed) |
| `FETCH_WORKERS`, `P2_WORKERS` | Parallelism for downloads / fuzzy scoring |
