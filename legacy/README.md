# `legacy/` – old single-file version

The original all-in-one version of the scanner, kept for reference and comparison.
**The maintained version is in `src/prediction_market_rv/`.**

## Run

From the project root:

```bash
uv run python legacy/run_simple.py
```

Writes `arbitrage_opportunities_<date>_<time>.xlsx` and `arbitrage_run_log.txt` to the current
folder (`OUTPUT_DIR` at the top of the script).

## Contents

| File | What it is |
|---|---|
| `run_simple.py` | Whole pipeline in one file: download, match, price, Excel report. Settings are at the top of the file |
| `contract_guard.py` | Same-contract safety checks. **Must stay in the same folder as `run_simple.py`** (imported as `contract_guard`) |

## Dependencies

`httpx`, `rapidfuzz`, `numpy`, `openpyxl`, and optionally `dnspython`
(custom DNS, see `USE_CUSTOM_DNS`).

> `contract_guard.py` here is a copy of `src/prediction_market_rv/engine/contract_guard.py`.
> If you fix one, fix the other, or stop using the legacy script.
