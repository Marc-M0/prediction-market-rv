"""Kalshi client: downloads open events, turns them into Market records, builds event links."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import time

import httpx

from prediction_market_rv.config import (
    ALLOW_LADDERS,
    ALLOW_MULTI_CHOICE,
    FETCH_WORKERS,
    KALSHI_PAGES,
    KALSHI_SERIES_HINTS,
    KALSHI_URL,
)
from prediction_market_rv.core.models import Market, parse_dt, to_prob
from prediction_market_rv.core.teams import parse_teams
from prediction_market_rv.core.text import infer_subjects


def _kalshi_price(m: dict, field: str) -> float | None:
    """Kalshi returns cents (int) and/or '<field>_dollars' strings depending on API version."""
    dollars = to_prob(m.get(f"{field}_dollars"))
    if dollars is not None:
        return dollars
    cents = m.get(field)
    if isinstance(cents, (int, float)) and 0 <= cents <= 100:
        return cents / 100.0
    return None


def _kalshi_series_events(client: httpx.Client, ticker: str) -> list[dict]:
    try:
        r = client.get(KALSHI_URL, params={"series_ticker": ticker, "status": "open",
                                           "with_nested_markets": "true", "limit": 200})
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception:
        return []


def _kalshi_collect_events(client: httpx.Client) -> list[dict]:
    events: dict[str, dict] = {}

    # (1) known game-winner series (cheap, one request each)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:           # [SPEED]
        series_results = list(ex.map(lambda t_: _kalshi_series_events(client, t_), KALSHI_SERIES_HINTS))
    for t, evs in zip(KALSHI_SERIES_HINTS, series_results):
        for ev in evs:
            events[ev.get("event_ticker", "")] = ev
        print(f"   [kalshi series {t}] {len(evs)} events")

    # (2) generic scan of every open event that still has an open market
    cursor: str | None = None
    now_ts = int(time.time())
    for page in range(KALSHI_PAGES):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true",
                  "min_close_ts": now_ts}
        if cursor:
            params["cursor"] = cursor
        try:
            r = client.get(KALSHI_URL, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"   Kalshi page {page} failed: {str(e).splitlines()[0][:120]}")
            break
        for ev in data.get("events", []):
            events.setdefault(ev.get("event_ticker", ""), ev)
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.15)
    print(f"   [kalshi] {len(events)} unique events collected ({page + 1} pages scanned)")
    return list(events.values())


def fetch_kalshi(client: httpx.Client, pages: int = KALSHI_PAGES) -> list[Market]:
    """Same admission rule as Polymarket: 1-question events (+ 2-sided mutually exclusive games)."""
    out: list[Market] = []
    stats = {"events": 0, "markets": 0, "multi_choice_events": 0, "inactive": 0, "no_book": 0, "options": 0}
    for ev in _kalshi_collect_events(client):
        stats["events"] += 1
        ev_title = ev.get("title", "")
        ev_ticker = ev.get("event_ticker", "")
        mk = ev.get("markets") or []
        mutex = bool(ev.get("mutually_exclusive"))
        ev_teams = parse_teams(ev_title)
        has_draw = len(mk) >= 2 and any(
            re.search(r"\b(tie|draw)\b", f"{m.get('yes_sub_title') or ''} {m.get('title') or ''}", re.I)
            for m in mk)
        three_way = ev_teams is not None and (has_draw or (len(mk) == 3 and mutex))   # team / draw / team
        if ev_ticker.upper().startswith("KXMVE") or (has_draw and not three_way):
            stats["multi_choice_events"] += 1
            continue
        if three_way or (len(mk) == 2 and mutex):
            kind = "game"                                                # one match / one 2-sided game
        elif len(mk) == 1:
            kind = "binary"                                              # ONE question
        elif (ALLOW_MULTI_CHOICE and mutex) or (ALLOW_LADDERS and not mutex):
            kind = "option"                                              # list of options / ladder of rungs
        else:
            stats["multi_choice_events"] += 1
            continue
        opt_subjects: dict = {}
        if kind == "option":
            subs = [m.get("yes_sub_title") or "" for m in mk]
            if all(subs) and len(set(subs)) == len(subs):
                opt_subjects = {m.get("ticker", ""): sb for m, sb in zip(mk, subs)}
            else:
                inferred = infer_subjects([str(m.get("title") or "") for m in mk])
                opt_subjects = {m.get("ticker", ""): sb for m, sb in zip(mk, inferred)}

        for m in mk:
            stats["markets"] += 1
            if m.get("status") not in (None, "active", "open"):
                stats["inactive"] += 1
                continue
            bid = _kalshi_price(m, "yes_bid")
            ask = _kalshi_price(m, "yes_ask")
            if bid is None or ask is None:
                stats["no_book"] += 1

            sub = m.get("yes_sub_title") or ""
            if kind == "option":
                stats["options"] += 1
            parts: list[str] = []
            for t in (ev_title, m.get("title"), sub):
                if t and t.lower() not in " ".join(parts).lower():
                    parts.append(t)

            out.append(
                Market(
                    venue="KALSHI",
                    market_id=m.get("ticker", ""),
                    title=" - ".join(parts),
                    close_time=parse_dt(m.get("close_time")),
                    yes_bid=bid,
                    yes_ask=ask,
                    url="",
                    series_ticker=ev.get("series_ticker") or ev_ticker.split("-")[0],
                    event_ticker=ev_ticker,
                    group=ev_title,
                    subject=opt_subjects.get(m.get("ticker", ""), sub) if kind == "option" else sub,
                    kind=kind,
                    event_id=ev_ticker if kind == "option" else "",
                    teams=ev_teams if kind == "game" else None,
                    alt_time=parse_dt(m.get("expected_expiration_time")),
                )
            )
    print(f"   [kalshi stats] {stats}")
    return out


def kalshi_event_url(client: httpx.Client, series: str, event: str, cache: dict) -> str:
    """https://kalshi.com/markets/<series>/<slug>/<event>, all lowercase. The slug is the series
    title slugified (e.g. 'NFL Game' -> 'nfl-game'), fetched once per series."""
    if series not in cache:
        slug = "market"
        try:
            r = client.get(f"{KALSHI_URL.rsplit('/', 1)[0]}/series/{series}")
            r.raise_for_status()
            title = (r.json().get("series") or {}).get("title") or ""
            if title:
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "market"
        except Exception:
            pass
        cache[series] = slug
    return f"https://kalshi.com/markets/{series.lower()}/{cache[series]}/{event.lower()}"
