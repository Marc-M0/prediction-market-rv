# `tests/` – automated tests

Run everything from the project root:

```bash
uv run pytest -s
```

| File | What it checks | Needs internet? |
|---|---|---|
| `test_fees.py` | Kalshi / Polymarket fee maths and `best_arb`, using golden numbers from a real report | No |
| `test_matcher.py` | The fast candidate search never skips an allowed pair and never lets a forbidden pair (date rule) through | No |
| `test_apis.py` | Live smoke test: can both public APIs still be reached and prices read? Skipped (not failed) if offline | **Yes** |

Run a single file:

```bash
uv run pytest -s tests/test_fees.py
uv run pytest -s tests/test_matcher.py
uv run pytest -s tests/test_apis.py          # -s shows the printed markets
uv run python tests/test_apis.py             # same API checks without pytest
```

Offline only (skip the live API test):

```bash
uv run pytest tests/test_fees.py tests/test_matcher.py
```
