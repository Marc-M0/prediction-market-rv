"""Finds matching Polymarket <-> Kalshi pairs (games, single questions, option events, guard, 1:1)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import time

from rapidfuzz import fuzz
from rapidfuzz import process
import numpy as np

from prediction_market_rv.config import (
    CANDIDATES_PER_POLY,
    MAX_CLOSE_GAP_DAYS,
    MAX_NEAR_MISSES,
    MIN_SCORE,
    P2_CHUNK,
    P2_SPAN_DAYS,
    P2_WORKERS,
)
from prediction_market_rv.core.models import Market
from prediction_market_rv.core.teams import team_keys
from prediction_market_rv.core.text import clean_words, is_negated
from prediction_market_rv.engine.options import match_option_events
from prediction_market_rv.engine.rules import reject_reason, soft_warnings
from prediction_market_rv.engine import contract_guard as cg


def _ts(dt: datetime | None) -> float:
    return dt.timestamp() if dt else float("nan")


def path2_candidates(poly: list[Market], kalshi: list[Market], k_clean: list[str],
                     cands: list, rejected: list, reasons: dict) -> None:
    """Fuzzy shortlist for every non-game Polymarket market (appends to cands / rejected / reasons).

    Same rule as the old per-question process.extract loop (top CANDIDATES_PER_POLY by token_set_ratio >= MIN_SCORE-10,
    then the average-score filter and reject_reason), with two changes that do not alter which pairs are valid:
      1. Kalshi markets whose close dates can never pass reject_reason's 'close dates differ' test (> MAX_CLOSE_GAP_DAYS
         from every Polymarket date) are never scored. The old loop scored them, filled its top-25 with them
         (e.g. the same 'Highest temperature in X' question for ten other days) and threw them away afterwards.
      2. Questions are scored in blocks with rapidfuzz.cdist on all CPU cores instead of one at a time on one core.
    """
    W = MAX_CLOSE_GAP_DAYS * 86400.0
    cutoff = MIN_SCORE - 10
    kc = np.array([_ts(k.close_time) for k in kalshi])
    ka = np.array([_ts(k.alt_time) for k in kalshi])
    k_notime = np.isnan(kc) & np.isnan(ka)              # no date at all -> reject_reason never rejects on dates
    k_flat = np.array([k.kind != "option" for k in kalshi], dtype=bool)

    def times(m: Market) -> list[float]:
        return [t for t in (_ts(m.close_time), _ts(m.alt_time)) if t == t]

    # a Polymarket option is matched to Kalshi options in path 3; here only to single-question Kalshi markets
    todo = [pi for pi, p in enumerate(poly) if not (p.kind == "game" and p.teams)]
    for is_option in (False, True):
        group = [pi for pi in todo if (poly[pi].kind == "option") == is_option]
        pool_mask = k_flat if is_option else np.ones(len(kalshi), dtype=bool)
        dated = sorted((pi for pi in group if times(poly[pi])), key=lambda pi: min(times(poly[pi])))
        undated = [pi for pi in group if not times(poly[pi])]       # no date -> compatible with everything
        blocks: list[tuple[list[int], np.ndarray]] = []
        cur: list[int] = []
        lo = hi = 0.0
        for pi in dated:
            t_min, t_max = min(times(poly[pi])), max(times(poly[pi]))
            if cur and (len(cur) >= P2_CHUNK or t_max - lo > P2_SPAN_DAYS * 86400.0):
                blocks.append((cur, np.flatnonzero(pool_mask & ((kc >= lo - W) & (kc <= hi + W)
                                                               | (ka >= lo - W) & (ka <= hi + W) | k_notime))))
                cur = []
            if not cur:
                lo, hi = t_min, t_max
            lo, hi = min(lo, t_min), max(hi, t_max)
            cur.append(pi)
        if cur:
            blocks.append((cur, np.flatnonzero(pool_mask & ((kc >= lo - W) & (kc <= hi + W)
                                                           | (ka >= lo - W) & (ka <= hi + W) | k_notime))))
        pool_all = np.flatnonzero(pool_mask)
        for i in range(0, len(undated), P2_CHUNK):
            blocks.append((undated[i:i + P2_CHUNK], pool_all))

        for chunk, cand_idx in blocks:
            if cand_idx.size == 0:
                continue
            p_clean = [clean_words(poly[pi].title) for pi in chunk]
            choices = [k_clean[j] for j in cand_idx]
            M = process.cdist(p_clean, choices, scorer=fuzz.token_set_ratio, dtype=np.float32,
                              score_cutoff=cutoff, workers=P2_WORKERS)
            tc, ta = kc[cand_idx], ka[cand_idx]
            nt = k_notime[cand_idx]
            for r, pi in enumerate(chunk):
                row = M[r]
                ok = row >= cutoff
                pt = times(poly[pi])
                if pt:                                                  # exact twin of reject_reason's date test
                    near = np.zeros(cand_idx.size, dtype=bool)
                    for t in pt:
                        near |= (np.abs(tc - t) <= W) | (np.abs(ta - t) <= W)
                    ok &= near | nt
                hit = np.flatnonzero(ok)
                if hit.size == 0:
                    continue
                hit = hit[np.argsort(-row[hit], kind="stable")][:CANDIDATES_PER_POLY]   # best first, ties by index
                p = poly[pi]
                pc = p_clean[r]
                for j in hit:
                    ki = int(cand_idx[j])
                    score = (fuzz.token_set_ratio(pc, k_clean[ki]) + fuzz.token_sort_ratio(pc, k_clean[ki])) / 2
                    if score < MIN_SCORE:
                        reasons["score too low"] += 1
                        continue
                    why = reject_reason(p, kalshi[ki], score)
                    if why:
                        reasons[why] += 1
                        rejected.append((score, why, p, kalshi[ki]))
                    else:
                        cands.append((score, pi, ki))


def find_matches(poly: list[Market], kalshi: list[Market], debug: bool = True,
                 event_report: list | None = None):
    """
    Returns (pairs, rejected).
      pairs    = [(poly, kalshi, score, inverted, warnings)]   inverted = one side is a negation
      rejected = near-misses [(score, reason, poly, kalshi)] for the 'Not matched' sheet
    event_report (optional list) is filled with the multi-choice / ladder events that stayed unpaired.
    Two paths:
      * game vs game  -> matched on the TEAMS (both teams, any order, city/nickname aware) + which side YES is
      * everything else -> fuzzy title shortlist, then hard constraints
    """
    cands: list[tuple[float, int, int]] = []
    rejected: list[tuple[float, str, Market, Market]] = []
    reasons: dict[str, int] = defaultdict(int)
    _t0 = time.perf_counter()

    def _lap(label: str) -> None:                                     # [TIMING] where does the time go?
        nonlocal _t0
        now = time.perf_counter()
        print(f"   [time] {label}: {now - _t0:.1f}s")
        _t0 = now

    # ---- path 1: games --------------------------------------------------------------
    index: dict[str, set] = defaultdict(set)
    for ki, k in enumerate(kalshi):
        if k.kind == "game" and k.teams:
            for name in k.teams:
                for key in team_keys(name):
                    index[key].add(ki)
    for pi, p in enumerate(poly):
        if p.kind != "game" or not p.teams:
            continue
        pool: set = set()
        for name in p.teams:
            for key in team_keys(name):
                pool |= index.get(key, set())
        for ki in pool:
            k = kalshi[ki]
            why = reject_reason(p, k)
            if why is None:
                cands.append((100.0, pi, ki))
            else:
                reasons[why] += 1
                if not why.startswith("different matchup"):
                    rejected.append((95.0, why, p, k))

    _lap(f"path 1 games ({sum(1 for p in poly if p.kind == 'game')} poly games)")
    # ---- path 2: everything else (fuzzy) ------------------------------------------------
    k_clean = [clean_words(k.title) for k in kalshi]
    path2_candidates(poly, kalshi, k_clean, cands, rejected, reasons)      # [SPEED] was a 1-core process.extract loop

    _lap(f"path 2 fuzzy single questions ({len(poly)} poly x {len(kalshi)} kalshi markets)")
    # ---- path 3: multi-choice lists and ladders: match the events, then option <-> same option ----
    opt_cands, opt_warn = match_option_events(poly, kalshi, rejected, reasons, event_report)
    cands.extend(opt_cands)

    _lap("path 3 option events")
    # ---- [GUARD] semantic guard: drop every candidate that is NOT the same contract -----------------------
    # Runs BEFORE the 1:1 assignment below, so a rejected pair can never "use up" the right counterpart.
    guard_review: dict[tuple[int, int], list[str]] = {}
    guarded: list[tuple[float, int, int]] = []
    for score, pi, ki in cands:
        p, k = poly[pi], kalshi[ki]
        kind = "game" if (p.kind == "game" and k.kind == "game") else "binary"
        verdict, why = cg.check_pair(p.title, k.title, kind)
        if verdict == "reject":
            reasons["guard: " + why[0].split(" [")[0]] += 1
            rejected.append((score, "guard: " + why[0], p, k))
            continue
        guard_review[(pi, ki)] = why if verdict == "review" else []
        guarded.append((score, pi, ki))
    cands = guarded

    _lap(f"semantic guard ({len(cands) + sum(v for k_, v in reasons.items() if k_.startswith('guard:'))} candidates)")
    cands.sort(reverse=True)
    used_p: set[int] = set()
    used_k: set[int] = set()
    pairs = []
    for score, pi, ki in cands:
        if pi in used_p or ki in used_k:
            continue
        used_p.add(pi)
        used_k.add(ki)
        p, k = poly[pi], kalshi[ki]
        inverted = is_negated(p.title) != is_negated(k.title)
        warns = list(opt_warn.get((pi, ki)) or soft_warnings(p, k))
        warns += ["WARN: " + w for w in guard_review.get((pi, ki), [])]     # [GUARD] 'review' verdicts -> status CHECK
        pairs.append((p, k, score, inverted, warns))

    _lap("1:1 assignment")
    rejected.sort(key=lambda r: -r[0])
    if debug:
        print(f"   candidate rejections: {dict(reasons)}")
        print(f"   matched pairs: {len(pairs)}  (game pairs: {sum(1 for x in pairs if x[0].kind == 'game')}, "
              f"option pairs: {sum(1 for x in pairs if x[0].kind == 'option')})")
    return pairs, rejected[:MAX_NEAR_MISSES]
