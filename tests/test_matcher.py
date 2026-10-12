"""path2_candidates must (a) never skip a pair the date rule allows and (b) never let a pair through that it forbids."""
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest
from rapidfuzz import fuzz, process

from prediction_market_rv.core.models import Market
from prediction_market_rv.core.text import clean_words
from prediction_market_rv.engine import matcher
from prediction_market_rv.engine.rules import _times, reject_reason
from prediction_market_rv import config

BASE = datetime(2026, 10, 12, tzinfo=timezone.utc)


def mk(venue, i, title, days, kind="binary", alt_days=None):
    t = lambda d: BASE + timedelta(days=d) if d is not None else None
    return Market(venue=venue, market_id=f"{venue[0]}{i}", title=title, close_time=t(days), alt_time=t(alt_days),
                  yes_bid=0.4, yes_ask=0.45, kind=kind)


def reference(poly, kalshi, k_clean):
    """Plain one-question-at-a-time loop, with the pool first limited by the date rule."""
    out = []
    flat = [i for i, k in enumerate(kalshi) if k.kind != "option"]
    for pi, p in enumerate(poly):
        pool = [i for i in (flat if p.kind == "option" else range(len(kalshi)))
                if not (_times(p) and _times(kalshi[i]) and
                        min(abs((x - y).total_seconds()) for x in _times(p) for y in _times(kalshi[i])) / 86400
                        > config.MAX_CLOSE_GAP_DAYS)]
        pc = clean_words(p.title)
        for _, _, j in process.extract(pc, [k_clean[i] for i in pool], scorer=fuzz.token_set_ratio,
                                       limit=config.CANDIDATES_PER_POLY, score_cutoff=config.MIN_SCORE - 10):
            ki = pool[j]
            score = (fuzz.token_set_ratio(pc, k_clean[ki]) + fuzz.token_sort_ratio(pc, k_clean[ki])) / 2
            if score >= config.MIN_SCORE and reject_reason(p, kalshi[ki], score) is None:
                out.append((score, pi, ki))
    return sorted(out)


def run(poly, kalshi):
    cands = []
    matcher.path2_candidates(poly, kalshi, [clean_words(k.title) for k in kalshi], cands, [], defaultdict(int))
    return sorted(cands)


def synthetic(seed=3, n_poly=120, n_kal=400):
    rnd = random.Random(seed)
    names = ["Rubio", "Newsom", "Shapiro", "Beshear", "Harris", "Vance", "Whitmer", "Pritzker", "Cuomo", "Booker"]
    poly = [mk("POLY", i, f"Will {rnd.choice(names)} win the 2028 {rnd.choice(['Democratic', 'Republican'])} nomination v{i % 7}?",
               rnd.choice([None, 5, 6, 40, 400, 410]), rnd.choice(["binary", "option"])) for i in range(n_poly)]
    kal = [mk("KAL", i, f"{rnd.choice(['Democratic', 'Republican'])} nominee - Will {rnd.choice(names)} be the nominee v{i % 7}?",
              rnd.choice([None, 4, 7, 41, 398, 420]), rnd.choice(["binary", "option"]),
              alt_days=rnd.choice([None, None, 6, 405])) for i in range(n_kal)]
    return poly, kal


@pytest.mark.parametrize("chunk,span", [(192, 14), (7, 3), (1, 1)])
def test_same_candidates_as_the_plain_loop(monkeypatch, chunk, span):
    monkeypatch.setattr(matcher, "P2_CHUNK", chunk)
    monkeypatch.setattr(matcher, "P2_SPAN_DAYS", span)
    poly, kal = synthetic()
    assert run(poly, kal) == reference(poly, kal, [clean_words(k.title) for k in kal])


def test_right_day_market_is_not_pushed_out_by_lookalikes_on_other_days():
    kal = [mk("KAL", i, "Will Bayern Munich win todays Bundesliga match?", i) for i in range(40)]
    poly = [mk("POLY", 0, "Will Bayern Munich win todays Bundesliga match?", 25)]
    assert 25 in {ki for _, _, ki in run(poly, kal)}


def test_dates_are_still_enforced():
    kal = [mk("KAL", 0, "Will Bayern Munich win the cup?", 30)]
    poly = [mk("POLY", 0, "Will Bayern Munich win the cup?", 0)]
    assert run(poly, kal) == []                                 # 30 days apart > MAX_CLOSE_GAP_DAYS
    poly[0] = mk("POLY", 0, "Will Bayern Munich win the cup?", 29)
    assert len(run(poly, kal)) == 1                             # 1 day apart is fine
