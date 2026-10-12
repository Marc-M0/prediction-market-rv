"""Live smoke test: can we still reach the public Kalshi and Polymarket APIs and read prices from them?

Needs internet (no API key or login). If the network is unreachable the tests are skipped, not failed.
    uv run pytest -s tests/test_apis.py          (-s shows the printed markets)
    uv run python tests/test_apis.py             (same checks without pytest)
"""
import httpx

from prediction_market_rv.clients.http import make_client      # same client + DNS workaround the screener uses
from prediction_market_rv.clients.kalshi import _kalshi_price   # reads cents or *_dollars, whichever the API sends
from prediction_market_rv.config import KALSHI_URL, POLY_URL

KALSHI_MARKETS_API = KALSHI_URL.rsplit("/", 1)[0] + "/markets"
LINE = "=" * 60


class Unreachable(Exception):
    """The API could not be reached at all (offline, DNS, timeout)."""


def _get(url: str, params: dict):
    try:
        with make_client() as client:
            res = client.get(url, params=params)
            res.raise_for_status()          # 4xx / 5xx -> a real failure, not a skip
            return res.json()
    except httpx.TransportError as e:       # connect error, DNS, timeout
        raise Unreachable(f"{type(e).__name__}: {e}") from e


def check_kalshi() -> None:
    print(LINE)
    print("Fetching LIVE Kalshi markets (public read)...")
    data = _get(KALSHI_MARKETS_API, {"limit": 5, "status": "open"})
    markets = data.get("markets", [])
    assert markets, "Kalshi returned no open markets"
    print(f"Kalshi: fetched {len(markets)} live markets.\n")
    for m in markets[:3]:
        assert m.get("ticker") and m.get("title"), f"market without ticker/title: {m}"
        bid, ask = _kalshi_price(m, "yes_bid"), _kalshi_price(m, "yes_ask")
        print(f"  [{m['ticker']}] {m['title']}")
        print(f"    Yes bid: {bid} | Yes ask: {ask}   (0..1 dollars; None = no live book)")


def check_polymarket() -> None:
    print("\n" + LINE)
    print("Fetching LIVE Polymarket events...")
    events = _get(POLY_URL, {"limit": 5, "active": "true", "closed": "false"})
    assert events, "Polymarket returned no active events"
    print(f"Polymarket: fetched {len(events)} live events.\n")
    for event in events[:3]:
        assert event.get("title"), f"event without title: {event}"
        markets = event.get("markets", [])
        print(f"  [Event] {event['title']}")
        for m in markets[:1]:
            print(f"    Market: {m.get('question')}")
            print(f"    Outcome prices: {m.get('outcomePrices')} | best bid/ask: {m.get('bestBid')} / {m.get('bestAsk')}")


def _run(check) -> None:
    try:
        check()
    except Unreachable as e:
        try:
            import pytest
        except ImportError:
            print(f"Could not reach the API: {e}")
        else:
            pytest.skip(f"API not reachable: {e}")


def test_kalshi() -> None:
    _run(check_kalshi)


def test_polymarket() -> None:
    _run(check_polymarket)


if __name__ == "__main__":
    for fn in (check_kalshi, check_polymarket):
        try:
            fn()
        except Unreachable as e:
            print(f"Could not reach the API: {e}")
        except Exception as e:                  # AssertionError, HTTP status errors, bad JSON ...
            print(f"{fn.__name__} FAILED: {type(e).__name__}: {e}")