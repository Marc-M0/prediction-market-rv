"""Golden numbers taken from real rows of arbitrage_opportunities_2026-10-12_0917.xlsx."""
import pytest

from prediction_market_rv.core.fee_engine import kalshi_fee, poly_fee
from prediction_market_rv.core.models import Market
from prediction_market_rv.engine.arbitrage import best_arb


def mk(venue, bid, ask, fee_rate=0.0):
    return Market(venue=venue, market_id="x", title="t", close_time=None, yes_bid=bid, yes_ask=ask, fee_rate=fee_rate)


def test_kalshi_fee_rounds_up_to_the_cent():
    assert kalshi_fee(0.35) == pytest.approx(0.016)       # 0.07*100*0.35*0.65 = $1.5925 -> $1.60 per 100 contracts
    assert kalshi_fee(0.0) == 0.0 and kalshi_fee(1.0) == 0.0


def test_poly_fee_is_rate_times_p_times_one_minus_p():
    assert poly_fee(0.55, 0.04) == pytest.approx(0.0099)


def test_row1_openai_millennium_prize():
    arb = best_arb(mk("POLY", 0.54, 0.55, 0.04), mk("KALSHI", 0.65, 0.70))
    assert arb["strategy"] == "YES Polymarket + NO Kalshi"
    assert arb["gross_edge"] == pytest.approx(0.10)
    assert arb["net_edge"] == pytest.approx(0.0741)


def test_row2_openai_earbuds_buys_no_on_polymarket():
    arb = best_arb(mk("POLY", 0.16, 0.25, 0.04), mk("KALSHI", 0.07, 0.08))
    assert arb["strategy"] == "YES Kalshi + NO Polymarket"
    assert arb["poly_fee"] == pytest.approx(0.005376)
    assert arb["kalshi_fee"] == pytest.approx(0.0052)
    assert arb["net_edge"] == pytest.approx(0.069424)


def test_no_prices_means_no_trade():
    arb = best_arb(mk("POLY", None, None), mk("KALSHI", None, None))
    assert arb["net_edge"] == float("-inf")
