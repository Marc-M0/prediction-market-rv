# `clients/` – talking to the platforms

Public APIs only, no login or API key.

| File | What it does |
|---|---|
| `http.py` | `make_client()` – shared `httpx` client; also installs the optional DNS workaround (`USE_CUSTOM_DNS`) |
| `polymarket.py` | `fetch_polymarket()` – downloads events from the Gamma API and converts them to `Market` records (including the fee category / rate) |
| `kalshi.py` | `fetch_kalshi()` – downloads open events, converts to `Market` records; `kalshi_event_url()` builds the exact event page link |

Used by `cli.py`; not meant to be run directly.
The live connectivity check is in `tests/test_apis.py`.
