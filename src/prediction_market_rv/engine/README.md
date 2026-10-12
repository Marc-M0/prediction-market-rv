# `engine/` – matching and arbitrage logic

| File | What it does |
|---|---|
| `matcher.py` | `find_matches()` – pairs Polymarket and Kalshi markets (games, single questions, option events), 1-to-1 |
| `options.py` | Multi-choice lists and ladders: match the *events* first, then each option to the *same* option |
| `rules.py` | Hard constraints – reasons why two markets are **not** the same contract (dates, teams, entities…) |
| `contract_guard.py` | Extra semantic safety checks (numbers, thresholds like "at least" vs "above", deadlines, leagues, game sub-markets…). Verdict: `ok` / `review` / `reject` |
| `arbitrage.py` | `best_arb()` – tests both hedges at the ask with fees included; `flags_for()` adds warning flags |

Typical flow: `matcher` → `rules` + `contract_guard` filter → `arbitrage` prices the survivors.
Tests for this folder: `tests/test_matcher.py`, `tests/test_fees.py`.
