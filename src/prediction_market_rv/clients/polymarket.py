"""Polymarket Gamma client: downloads events and turns them into Market records."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re

import httpx

from prediction_market_rv.config import (
    ALLOW_LADDERS,
    ALLOW_MULTI_CHOICE,
    FETCH_WORKERS,
    POLY_PAGES,
    POLY_PAGES_PER_TAG,
    POLY_TAG_SLUGS,
    POLY_URL,
)
from prediction_market_rv.core.fee_engine import poly_fee_info
from prediction_market_rv.core.models import Market, parse_dt, to_prob
from prediction_market_rv.core.teams import parse_teams, poly_game_subject
from prediction_market_rv.core.text import infer_subjects


SOCCER_PROP_SKIP = re.compile(r"both teams|btts|exact score|clean sheet|goal ?scorer|anytime|corners?|cards?|"
                              r"shots|total goals|\bover\b|\bunder\b|\+\d|-\d", re.I)


def _poly_outcomes(m: dict) -> list[str]:
    o = m.get("outcomes")
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except ValueError:
            o = []
    return [str(x) for x in (o or [])]


def poly_option_subjects(markets: list[dict]) -> dict[str, str]:
    """market id -> option label. Uses Polymarket's groupItemTitle, else infers it from the questions."""
    yn = [m for m in markets
          if [x.lower() for x in _poly_outcomes(m)] == ["yes", "no"] and not m.get("closed")]
    inferred = infer_subjects([str(m.get("question", "")) for m in yn])
    out = {}
    for m, inf in zip(yn, inferred):
        label = str(m.get("groupItemTitle") or "").strip()
        out[str(m.get("id"))] = label or inf
    return out

VS_RE = re.compile(r"\bvs\.?\b|\bv\.\s", re.I)
GAME_SKIP_RE = re.compile(r"spread|o/u|over/under|\btotals?\b|handicap|\([+-]?\d", re.I)


def _poly_events(client: httpx.Client, extra: dict, pages: int):
    use_order = True            # sort by 24h volume; dropped automatically if the API rejects it
    for page in range(pages):
        events = None
        for _ in range(2):
            params = {"limit": 100, "offset": page * 100, "active": "true",
                      "closed": "false", **extra}
            if use_order:
                params["order"] = "volume24hr"
                params["ascending"] = "false"
            try:
                r = client.get(POLY_URL, params=params)
                r.raise_for_status()
                events = r.json()
                break
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code == 422 and use_order:
                    use_order = False       # retry this page without sorting
                    continue
                print(f"   Polymarket {extra or 'generic'} page {page} failed: "
                      f"{str(e).splitlines()[0][:120]}")
                return
        if not events:
            return
        yield from events


def fetch_polymarket(client: httpx.Client, pages: int = POLY_PAGES) -> list[Market]:
    """
    Single-question markets only (see ALLOW_LADDERS / ALLOW_MULTI_CHOICE), pulled from the top-volume
    list PLUS per-tag lists so sports/politics/economy/crypto are all covered.
    Team-vs-team moneylines are split into "Will <team> win?" Yes/No markets.
    """
    out: list[Market] = []
    stats = {"events": 0, "markets": 0, "multi_choice_skipped": 0, "games": 0,
             "not_yes_no": 0, "no_book": 0}
    seen: set = set()
    passes = [("top-volume", {}, pages)] + [
        (f"tag:{t}", {"tag_slug": t}, POLY_PAGES_PER_TAG) for t in POLY_TAG_SLUGS
    ]
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:           # [SPEED] pages inside one pass stay sequential
        futures = [ex.submit(lambda e=extra, n=n_pages: list(_poly_events(client, e, n))) for _, extra, n_pages in passes]
        fetched = [f.result() for f in futures]
    for (label, extra, n_pages), pass_events in zip(passes, fetched):   # same order as before -> same dedup result
        new_events = 0
        for ev in pass_events:
            eid = ev.get("id") or ev.get("slug")
            if eid in seen:
                continue
            seen.add(eid)
            new_events += 1
            stats["events"] += 1
            mk = ev.get("markets") or []
            neg = bool(ev.get("negRisk") or ev.get("enableNegRisk"))
            strict_ok = len(mk) == 1 or (len(mk) == 2 and neg)             # ONE question (or 2-sided game)
            opt_ok = (ALLOW_MULTI_CHOICE and neg) or (ALLOW_LADDERS and not neg)   # lists / ladders -> per option
            three_way = len(mk) >= 2 and any(
                re.search(r"\bdraw\b", str(m.get("question", "")), re.I) for m in mk)
            if three_way:                       # win / draw / win soccer events: handled as games below
                strict_ok = opt_ok = False
            slug = ev.get("slug") or ""
            ev_teams = parse_teams(ev.get("title", ""))
            ev_subjects = poly_option_subjects(mk) if (opt_ok and not strict_ok) else {}
            for m in mk:
                stats["markets"] += 1
                if m.get("closed") or m.get("active") is False:
                    continue
                outcomes = m.get("outcomes")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except ValueError:
                        outcomes = []
                outcomes = [str(o) for o in (outcomes or [])]
                q = m.get("question") or ev.get("title", "")
                bid = to_prob(m.get("bestBid"))
                ask = to_prob(m.get("bestAsk"))
                base = dict(
                    venue="POLYMARKET",
                    close_time=parse_dt(m.get("endDate") or ev.get("endDate")),
                    url=f"https://polymarket.com/event/{slug}" if slug else "",
                    group=ev.get("title", ""),
                )
                low = [o.lower() for o in outcomes]
                if (low == ["yes", "no"] and ev_teams and len(mk) in (2, 3)
                        and not GAME_SKIP_RE.search(q) and not SOCCER_PROP_SKIP.search(q)):
                    subj = poly_game_subject(q, ev_teams)       # one match = one game (win / draw / win)
                    if subj:
                        g_rate, g_cat = poly_fee_info(ev, m, True)
                        stats["match_outcomes"] = stats.get("match_outcomes", 0) + 1
                        out.append(Market(market_id=str(m.get("id")), title=q, yes_bid=bid, yes_ask=ask,
                                          subject=subj, kind="game", teams=ev_teams,
                                          fee_rate=g_rate, fee_cat=g_cat, **base))
                        continue
                if low == ["yes", "no"]:
                    if strict_ok:
                        if bid is None or ask is None:
                            stats["no_book"] += 1
                        rate, cat = poly_fee_info(ev, m, False)
                        out.append(Market(market_id=str(m.get("id")), title=q, yes_bid=bid,
                                          yes_ask=ask, fee_rate=rate, fee_cat=cat, **base))
                    elif opt_ok:                # one option of a multi-choice list / one rung of a ladder
                        stats["options"] = stats.get("options", 0) + 1
                        rate, cat = poly_fee_info(ev, m, False)
                        out.append(Market(market_id=str(m.get("id")), title=q, yes_bid=bid, yes_ask=ask,
                                          subject=ev_subjects.get(str(m.get("id")), ""), kind="option",
                                          event_id=str(eid), fee_rate=rate, fee_cat=cat, **base))
                    else:
                        stats["multi_choice_skipped"] += 1
                elif (len(outcomes) == 2 and VS_RE.search(q) and not GAME_SKIP_RE.search(q)
                      and parse_teams(q)):
                    stats["games"] += 1     # bestBid/bestAsk quoted for FIRST outcome (assumption)
                    g_rate, g_cat = poly_fee_info(ev, m, True)
                    for i, o in enumerate(outcomes):
                        if i == 0:
                            b_, a_ = bid, ask
                        else:
                            b_ = None if ask is None else round(1.0 - ask, 4)
                            a_ = None if bid is None else round(1.0 - bid, 4)
                        out.append(Market(market_id=f"{m.get('id')}:{i}", title=f"Will {o} win? {q}",
                                          yes_bid=b_, yes_ask=a_, subject=o, kind="game",
                                          teams=parse_teams(q), fee_rate=g_rate, fee_cat=g_cat, **base))
                else:
                    stats["not_yes_no"] += 1
        print(f"   [poly pass {label}] +{new_events} new events")
    print(f"   [poly stats] {stats}")
    return out
