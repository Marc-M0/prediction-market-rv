# Prediction Market Relative-Value Scanner (Polymarket ⇄ Kalshi)

Finds Yes/No questions that exist on **both** Polymarket and Kalshi, then checks whether buying
opposite sides on the two venues costs less than the $1 payout **after both platforms' taker fees**.
Results are written to an Excel report.

> ⚠️ **Not financial advice.** A match is found automatically and can be wrong. A profit is only
> locked in if both questions resolve *identically*. Always read both venues' resolution rules and
> re-check live order books before trading.

## How it works

```
download both venues  ->  match questions  ->  guard checks  ->  price + fees  ->  Excel report
   (clients/)             (engine/matcher)    (contract_guard)   (engine/arbitrage,   (report/)
                                                                   core/fee_engine)
```

1. **Download** open events from Polymarket (Gamma API) and Kalshi (public API) – no API key needed.
2. **Match** with fuzzy text scoring (rapidfuzz) plus hard rules (dates, teams, entities, numbers).
   Handles single Yes/No questions, team-vs-team games, and multi-choice / ladder events.
3. **Guard**: `contract_guard.py` rejects pairs that look similar but are *not* the same contract
   (different threshold, deadline, league, game sub-market, …).
4. **Price**: both hedges are tested (YES Poly + NO Kalshi, YES Kalshi + NO Poly) at the ask,
   with Kalshi and Polymarket fees included.
5. **Report**: an `.xlsx` file sorted by return on capital.

## Folder guide

| Folder | What it contains | README |
|---|---|---|
| [`src/prediction_market_rv/`](src/prediction_market_rv/) | **Current version** – the modular package | [README](src/prediction_market_rv/README.md) |
| [`tests/`](tests/) | Automated tests (fees, matcher, live API smoke test) | [README](tests/README.md) |
| [`legacy/`](legacy/) | **Old version** – everything in one script (`run_simple.py`) | [README](legacy/README.md) |

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install dependencies
uv run python -m prediction_market_rv    # current version (full scan -> Excel)
uv run python legacy/run_simple.py       # old single-file version
uv run pytest                            # run the tests
```

Dependencies used by the code: `httpx`, `rapidfuzz`, `numpy`, `openpyxl`, and optionally
`dnspython` (only for the custom-DNS workaround, `USE_CUSTOM_DNS` in the config), plus `pytest`
for the tests.

## Output

Each run writes to the current folder (change `OUTPUT_DIR` in the config):

- `arbitrage_opportunities_<date>_<time>.xlsx` with the sheets **Arbitrage**, **All matches**,
  **Not matched (near misses)**, **Unpaired events** and **Notes** (column explanations).
- `arbitrage_run_log.txt` – detailed progress / timing / rejection log (the terminal only shows the
  final summary line).

Reading the *Arbitrage* sheet: rows flagged `OK` come first, then rows flagged `CHECK`
(huge edge, wide spread, missing order book, dust-priced leg, or a guard warning). Be most
suspicious of very large edges – they are usually a rules mismatch, not free money.

## Configuration

All tunable settings (page limits, match thresholds, fee rates, spread/edge limits, output folder,
parallelism) live in one file: `src/prediction_market_rv/config.py`
(for the legacy script: the top of `legacy/run_simple.py`).
Fee rates change over time – verify them against each platform's official fee documentation.

## What is NOT modelled

Order-book depth (how much you can actually fill), slippage, deposit/bridging costs, taxes, and
platform rules about who may trade where. Check that using these platforms is legal for you.

## License

See [`LICENSE`](LICENSE). Commercial and profit use requires prior written permission.
To run uv run: prediction-market-rv