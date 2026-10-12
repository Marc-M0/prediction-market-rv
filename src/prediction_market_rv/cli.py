"""Entry point: download both venues -> match -> arbitrage -> Excel report."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import contextlib
import io
import time

from prediction_market_rv.clients.http import make_client
from prediction_market_rv.clients.kalshi import fetch_kalshi, kalshi_event_url
from prediction_market_rv.clients.polymarket import fetch_polymarket
from prediction_market_rv.config import FETCH_WORKERS, MIN_NET_EDGE, OUTPUT_DIR
from prediction_market_rv.core.models import Market
from prediction_market_rv.engine.arbitrage import best_arb, flags_for, invert_market
from prediction_market_rv.engine.matcher import find_matches
from prediction_market_rv.report.excel import build_all_row, build_row, write_report


def main() -> None:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "arbitrage_run_log.txt"

    buf = io.StringIO()                    # all progress/debug output goes to a log file, not the terminal
    poly: list[Market] = []
    kalshi: list[Market] = []
    pairs: list = []
    near: list = []
    unpaired: list = []
    T0 = time.perf_counter()
    t_fetch = t_match = t_urls = t_write = 0.0
    with contextlib.redirect_stdout(buf):
        with make_client() as client:
            def _timed(label, fn):                                          # [SPEED] the two venues are independent
                _t = time.perf_counter()
                res = fn(client)
                print(f"   [time] {label} download: {time.perf_counter() - _t:.1f}s")
                return res
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_poly = ex.submit(_timed, "Polymarket", fetch_polymarket)
                f_kal = ex.submit(_timed, "Kalshi", fetch_kalshi)
                poly, kalshi = f_poly.result(), f_kal.result()
        t_fetch = time.perf_counter() - T0
        if poly and kalshi:
            _t = time.perf_counter()
            pairs, near = find_matches(poly, kalshi, debug=True, event_report=unpaired)
            t_match = time.perf_counter() - _t
            print(f"   [time] matching total: {t_match:.1f}s")
    log_path.write_text(buf.getvalue(), encoding="utf-8")

    if not poly or not kalshi:
        print(f"Could not load market data (Polymarket: {len(poly)}, Kalshi: {len(kalshi)}). "
              f"See {log_path}")
        return

    rows, all_rows = [], []
    for p, k, score, inverted, warns in pairs:
        p_view = invert_market(p) if inverted else p
        arb = best_arb(p_view, k)
        flags = flags_for(p, k, arb, inverted, warns)
        all_rows.append(build_all_row(arb, p, k, score, inverted, flags))
        if arb["net_edge"] > MIN_NET_EDGE:
            rows.append(build_row(arb, p, k, score, inverted, flags))

    if rows:                                                    # exact Kalshi event links (few rows only)
        _t = time.perf_counter()
        cache: dict = {}
        with contextlib.redirect_stdout(buf), make_client() as client:
            series = sorted({r["k_series"] for r in rows})        # [SPEED] one request per series, in parallel
            with ThreadPoolExecutor(max_workers=FETCH_WORKERS * 2) as ex:
                list(ex.map(lambda s_: kalshi_event_url(client, s_, "x", cache), series))
            for r in rows:
                r["k_url"] = kalshi_event_url(client, r["k_series"], r["k_event"], cache)
        t_urls = time.perf_counter() - _t
        log_path.write_text(buf.getvalue(), encoding="utf-8")

    rows.sort(key=lambda r: (not r["ok"], -r["roi"]))          # clean ones first, best return first
    all_rows.sort(key=lambda r: -(r["net"] if r["net"] is not None else -9))
    unpaired.sort(key=lambda r: -r[5])                          # closest near-misses first
    _t = time.perf_counter()
    path = write_report(rows, out_dir / f"arbitrage_opportunities_{stamp}.xlsx", all_rows, near, unpaired)
    t_write = time.perf_counter() - _t

    summary = (f"{len(pairs)} matched pairs, {len(rows)} with an arbitrage edge "
               f"({sum(r['ok'] for r in rows)} clean), {len(unpaired)} unpaired multi-choice/ladder events")
    if rows:
        print(f"✅ File ready: {path}  ({summary}; top return {rows[0]['roi']:.2%})")
    else:
        print(f"✅ File ready: {path}  ({summary})")
    print(f"   timing: download {t_fetch:.0f}s | matching {t_match:.0f}s | Kalshi links {t_urls:.0f}s | "
          f"Excel {t_write:.1f}s | total {time.perf_counter() - T0:.0f}s   (details in {log_path.name})")
