# `core/` – shared building blocks

| File | What it does |
|---|---|
| `models.py` | `Market` record used by every module (venue, title, close time, YES bid/ask, fee rate, …) and parsing helpers |
| `fee_engine.py` | Fees: Kalshi taker fee (`0.07·C·P·(1−P)`, rounded up to the cent) and Polymarket category fee (`rate·p·(1−p)`) |
| `text.py` | Text normalisation: cleaning, numbers, months, directions, entities |
| `teams.py` | Team / club aliases, "A vs B" parsing, "are these the same team" checks |

Fee rates themselves are in `../config.py` – they change, so verify them against each
platform's fee documentation.
