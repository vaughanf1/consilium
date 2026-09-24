"""Engine tests: determinism, point-in-time, risk, costs, ledger, committee mechanics."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.analysts.base import AnalysisContext
from consilium.backtest import backtest_fund, rebalance_grid
from consilium.committee.blend import blend_views
from consilium.committee.redteam import rules_red_team
from consilium.core.models import Position, View
from consilium.core.spec import BlendPolicy, CostModel, Fund, FundSpec, RiskLimits, load_spec, normalize_universe
from consilium.data import SyntheticProvider
from consilium.data.features import PriceBook, market_snapshot
from consilium.execution import SimBroker, build_orders
from consilium.core.models import Order
from consilium.portfolio.construction import apply_rebalance_band, size_book
from consilium.core.spec import PortfolioPolicy
from consilium.risk import apply_limits

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"
UNI = ["AAPL", "MSFT", "JPM", "XOM", "PG"]


def _fund(name="systematic-trend"):
    return Fund(load_spec(MANDATES / f"{name}.yaml"))


# --- data --------------------------------------------------------------

def test_synthetic_is_deterministic():
    a = SyntheticProvider().prices("AAPL", "2024-01-01", "2024-03-01")
    b = SyntheticProvider().prices("AAPL", "2024-01-01", "2024-03-01")
    assert [x.close for x in a] == [x.close for x in b]
    assert all(x.low <= x.close <= x.high for x in a)


def test_pricebook_is_point_in_time():
    book = PriceBook(SyntheticProvider(), "2024-01-01", "2024-12-31")
    closes = book.closes_until("AAPL", "2024-06-14")
    bars = book.bars_until("AAPL", "2024-06-14", 1)
    assert bars[-1].date <= "2024-06-14"
    assert len(book.closes_until("AAPL", "2024-06-14")) < len(book.closes_until("AAPL", "2024-06-21"))
    assert book.mark("AAPL", "2024-06-15") == float(closes[-1])   # Saturday -> Friday close


def test_market_snapshot_buckets():
    book = PriceBook(SyntheticProvider(), "2024-01-01", "2024-12-31")
    m = market_snapshot(book, "AAPL", "2024-12-31")
    assert m is not None and m.n_bars > 252
    assert m.trend_bucket in ("strong uptrend", "uptrend", "downtrend", "strong downtrend")
    assert m.coarse_hash() == market_snapshot(book, "AAPL", "2024-12-31").coarse_hash()


# --- committee mechanics ----------------------------------------------

def _v(analyst, ticker, c, conf=0.5, h=60, abst=False):
    return View(analyst=analyst, ticker=ticker, date="2024-01-01", conviction=c, confidence=conf, horizon_days=h, abstained=abst)


def test_blend_excludes_abstentions_and_weights_confidence():
    views = [_v("a", "X", 1.0, conf=1.0), _v("b", "X", 0.0, abst=True), _v("c", "X", -1.0, conf=0.25)]
    r = blend_views(views, {"a": 1, "b": 1, "c": 1}, BlendPolicy(), rebalance_days=5)
    assert r.n_votes["X"] == 2
    assert r.convictions["X"] == pytest.approx((1.0 - 0.25) / 1.25)


def test_blend_horizon_awareness_downweights_short_views():
    long_only = blend_views([_v("a", "X", 1.0, h=60)], {"a": 1}, BlendPolicy(), 21)
    mixed = blend_views([_v("a", "X", 1.0, h=60), _v("b", "X", -1.0, h=5)], {"a": 1, "b": 1}, BlendPolicy(), 21)
    assert long_only.convictions["X"] == pytest.approx(1.0)
    assert mixed.convictions["X"] > 0.5   # the 5-day view carries 5/21 of the weight


def test_market_neutral_blend_sums_to_zero():
    views = [_v("a", "X", 0.8), _v("a", "Y", 0.2), _v("a", "Z", -0.1)]
    r = blend_views(views, {"a": 1}, BlendPolicy(market_neutral=True), 5)
    assert sum(r.convictions.values()) == pytest.approx(0.0)


def test_rules_red_team_haircuts_disagreement_more_than_consensus():
    agree = [_v("a", "X", 0.8), _v("b", "X", 0.7)]
    fight = [_v("a", "Y", 0.8), _v("b", "Y", -0.6)]
    notes = rules_red_team({"X": 0.75, "Y": 0.1}, {}, agree + fight, top_n=2, max_haircut=0.6)
    by = {n.ticker: n.haircut for n in notes}
    assert by["Y"] > by["X"]
    assert all(0 <= n.haircut <= 0.6 for n in notes)


# --- sizing & risk -------------------------------------------------------

def test_vol_scaled_sizing_gives_less_to_volatile_names():
    r = size_book({"CALM": 0.6, "WILD": 0.6}, {"CALM": 0.15, "WILD": 0.60}, PortfolioPolicy(), gross_target=1.0, exposure_multiplier=1.0)
    assert abs(r.weights["CALM"]) > 3 * abs(r.weights["WILD"])
    assert sum(abs(w) for w in r.weights.values()) == pytest.approx(1.0)


def test_sizing_drops_weak_convictions_and_respects_multiplier():
    r = size_book({"A": 0.05, "B": 0.5}, {}, PortfolioPolicy(min_conviction=0.1), 1.0, 0.5)
    assert r.weights["A"] == 0 and "A" in r.dropped
    assert abs(r.weights["B"]) == pytest.approx(0.5)


def test_rebalance_band_skips_small_changes_but_always_exits():
    out = apply_rebalance_band({"A": 0.105, "B": 0.0, "C": 0.2}, {"A": 0.10, "B": 0.05, "C": 0.1}, band=0.02)
    assert out["A"] == 0.10 and out["B"] == 0.0 and out["C"] == 0.2


def test_risk_limits_order_and_events():
    lim = RiskLimits(max_position_pct=0.2, max_gross_exposure=1.0, max_sector_pct=0.3, drawdown_soft=0.1, drawdown_hard=0.2)
    r = apply_limits({"A": 0.5, "B": 0.4, "C": -0.3}, lim, sectors={"A": "Tech", "B": "Tech", "C": "Energy"})
    assert all(abs(w) <= 0.2 + 1e-9 for w in r.weights.values())
    assert sum(abs(w) for t, w in r.weights.items() if t in ("A", "B")) <= 0.3 + 1e-9
    assert {e.limit for e in r.events} >= {"max_position_pct", "max_sector_pct"}


def test_drawdown_circuit_breaker():
    lim = RiskLimits(drawdown_soft=0.1, drawdown_hard=0.2, max_sector_pct=None)
    soft = apply_limits({"A": 0.1, "B": 0.1}, lim, drawdown=-0.12)
    hard = apply_limits({"A": 0.1, "B": 0.1}, lim, drawdown=-0.25)
    assert sum(abs(w) for w in soft.weights.values()) == pytest.approx(0.1)
    assert sum(abs(w) for w in hard.weights.values()) == 0.0
    assert hard.events[0].limit == "drawdown_hard"


def test_net_exposure_band():
    lim = RiskLimits(max_net_exposure=0.2, min_net_exposure=-0.2, max_sector_pct=None, max_position_pct=1.0, max_gross_exposure=5)
    r = apply_limits({"A": 0.6, "B": -0.1}, lim)
    assert sum(r.weights.values()) <= 0.2 + 1e-9


# --- execution -----------------------------------------------------------

def test_broker_charges_costs_and_tracks_avg_cost():
    b = SimBroker(cash=10_000, costs=CostModel(commission_bps=10, slippage_bps=10, half_spread_bps=0))
    f = b.place_order(Order(ticker="A", side="buy", quantity=10, reference_price=100.0))
    assert f.price == pytest.approx(100.1)
    assert f.commission == pytest.approx(1.001)
    assert b.cash() == pytest.approx(10_000 - 1001 - 1.001)
    assert b.positions()["A"].avg_cost == pytest.approx(100.1)
    b.place_order(Order(ticker="A", side="sell", quantity=10, reference_price=100.0))
    assert "A" not in b.positions()


def test_build_orders_sells_first_and_floors_shares():
    orders = build_orders({"A": 0.5, "B": 0.0}, {"B": Position(ticker="B", shares=10)}, {"A": 33.0, "B": 10.0}, equity=1000)
    assert [o.side for o in orders] == ["sell", "buy"]
    assert orders[1].quantity == 15   # floor(500 / 33)


# --- end to end -----------------------------------------------------------

def test_rebalance_grid():
    days = ["2024-01-02", "2024-01-03", "2024-01-05", "2024-01-08", "2024-01-12", "2024-02-01"]
    assert rebalance_grid(days, "weekly") == ["2024-01-05", "2024-01-12", "2024-02-01"]
    assert rebalance_grid(days, "monthly") == ["2024-01-12", "2024-02-01"]


def test_backtest_is_deterministic_and_carries_the_book():
    a = backtest_fund(_fund(), "2024-01-01", "2024-09-30", SyntheticProvider(), UNI, monte_carlo_paths=50)
    b = backtest_fund(_fund(), "2024-01-01", "2024-09-30", SyntheticProvider(), UNI, monte_carlo_paths=50)
    assert a.nav == b.nav
    assert a.metrics.n_periods == len(a.dates) > 20
    assert a.metrics.total_costs > 0
    assert a.in_sample is not None and a.out_of_sample is not None
    assert a.monte_carlo["paths"] == 50
    # positions carry: not every cycle starts from cash
    assert any(r.cash_before != a.capital for r in a.records[1:])
    # every record is honest about its own accounting
    for r in a.records:
        assert r.nav == pytest.approx(r.cash + sum(s * r.marks[t] for t, s in r.positions.items()))
        assert sum(abs(w) for w in r.final_weights.values()) <= 1.0 + 1e-9   # mandate's max_gross_exposure
        assert all(abs(w) <= 0.20 + 1e-9 for w in r.final_weights.values())  # mandate's max_position_pct


def test_llm_mandate_degrades_gracefully_without_keys():
    res = backtest_fund(_fund("committee-balanced"), "2024-01-01", "2024-06-30", SyntheticProvider(), UNI, monte_carlo_paths=20)
    rec = res.records[-1]
    llm_views = [v for s in rec.strategies for v in s.views if v.analyst in ("owner", "skeptic", "catalyst", "bear", "macro")]
    assert llm_views and all(v.abstained for v in llm_views)
    assert rec.verdict.cio.source == "rules"
    assert res.llm_used is False
    assert rec.nav > 0


def test_ledger_roundtrip_and_carry(tmp_path):
    from consilium.ledger import Ledger
    res = backtest_fund(_fund(), "2024-01-01", "2024-04-30", SyntheticProvider(), UNI, monte_carlo_paths=10)
    led = Ledger(tmp_path / "l.sqlite")
    for r in res.records:
        led.record_cycle(r, mode="paper")
    cash, positions, last = led.latest_book("systematic-trend")
    assert last == res.records[-1].as_of and cash == pytest.approx(res.records[-1].cash)
    assert {t: p.shares for t, p in positions.items()} == res.records[-1].positions
    assert led.cycle(res.records[0].id).as_of == res.records[0].as_of
    led.record_backtest(res, None)
    assert led.backtests("systematic-trend")[0]["n_periods"] == res.metrics.n_periods


def test_spec_rejects_typos_and_unknown_analysts():
    with pytest.raises(Exception):
        FundSpec(name="x", strategies=[{"name": "a", "analysts": [{"name": "nope"}]}])
    with pytest.raises(Exception):
        FundSpec(name="x", strategies=[{"name": "a", "analysts": [{"name": "trend"}]}], risk={"max_positon_pct": 0.1})
    assert normalize_universe("aapl, msft aapl") == ["AAPL", "MSFT"]
