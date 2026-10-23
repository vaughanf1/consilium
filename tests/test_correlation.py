"""Correlation-aware vol targeting: the maths, the shrinkage guard, the fallback."""

from __future__ import annotations

import math

import numpy as np
import pytest

from consilium.core.spec import RiskLimits
from consilium.data import SyntheticProvider
from consilium.data.features import PriceBook
from consilium.risk import apply_limits
from consilium.risk.correlation import (CORR_LOOKBACK, CORR_MIN_OBS, CORR_SHRINKAGE, CorrelationEstimate,
                                        correlated_vol, estimate_correlation, ewma_correlation, independent_vol)

W = {"A": 0.5, "B": 0.5}
V = {"A": 0.20, "B": 0.20}


def _corr(rho):
    return CorrelationEstimate(tickers=("A", "B"), matrix=np.array([[1.0, rho], [rho, 1.0]]), n_obs=120)


def _lim(target):
    return RiskLimits(portfolio_vol_target=target, max_sector_pct=None, max_position_pct=1.0,
                      drawdown_soft=None, drawdown_hard=None)


# --- the maths ------------------------------------------------------------

def test_zero_correlation_reproduces_the_independent_estimate():
    assert correlated_vol(W, V, _corr(0.0)) == pytest.approx(independent_vol(W, V))


def test_perfect_correlation_doubles_variance_of_an_equal_two_name_book():
    indep, together = correlated_vol(W, V, _corr(0.0)), correlated_vol(W, V, _corr(1.0))
    assert together ** 2 == pytest.approx(2 * indep ** 2)
    assert together == pytest.approx(0.20)   # one bet at full size


def test_negative_correlation_nets():
    assert correlated_vol(W, V, _corr(-1.0)) == pytest.approx(0.0, abs=1e-12)
    # and the mirror image: a long/short pair in perfectly correlated names is flat
    assert correlated_vol({"A": 0.5, "B": -0.5}, V, _corr(1.0)) == pytest.approx(0.0, abs=1e-12)


def test_ewma_recovers_known_correlation():
    rng = np.random.default_rng(7)
    x = rng.normal(size=(120, 1))
    same = np.hstack([x, x])
    opposite = np.hstack([x, -x])
    assert ewma_correlation(same)[0, 1] == pytest.approx(1.0)
    assert ewma_correlation(opposite)[0, 1] == pytest.approx(-1.0)


def test_flat_series_correlates_with_nothing():
    rng = np.random.default_rng(3)
    r = np.hstack([rng.normal(size=(60, 1)), np.zeros((60, 1))])
    c = ewma_correlation(r)
    assert c[0, 1] == 0.0 and c[1, 1] == 1.0


# --- shrinkage --------------------------------------------------------------

def test_shrinkage_caps_every_off_diagonal():
    rng = np.random.default_rng(11)
    x = rng.normal(size=(CORR_LOOKBACK, 1))
    est = estimate_correlation(["A", "B", "C"], np.hstack([x, x, -x]))
    off = est.matrix[~np.eye(3, dtype=bool)]
    assert np.all(np.abs(off) <= 1 - CORR_SHRINKAGE + 1e-12)
    assert est.matrix[0, 1] == pytest.approx(1 - CORR_SHRINKAGE)
    assert est.matrix[0, 2] == pytest.approx(-(1 - CORR_SHRINKAGE))
    assert np.allclose(np.diag(est.matrix), 1.0)
    assert np.all(np.linalg.eigvalsh(est.matrix) >= -1e-12)   # still a valid correlation matrix


# --- apply_limits -------------------------------------------------------------

def test_correlation_alone_can_trigger_the_scale_down_and_says_so():
    # independent: 14.1% < 16% target, no clamp. Correlated at ρ=0.9 (shrunk 0.63): ~18%.
    assert independent_vol(W, V) < 0.16
    old = apply_limits(W, _lim(0.16), vols=V)
    new = apply_limits(W, _lim(0.16), vols=V, corr=_corr(0.63))
    assert old.weights == W and not old.events
    ev = [e for e in new.events if e.limit == "portfolio_vol_target"]
    assert len(ev) == 1 and "because of correlation" in ev[0].note
    assert correlated_vol(new.weights, V, _corr(0.63)) == pytest.approx(0.16)
    assert all(abs(new.weights[t]) < abs(W[t]) for t in W)   # only ever shrinks


def test_clamp_both_estimates_agree_on_is_not_blamed_on_correlation():
    r = apply_limits(W, _lim(0.10), vols=V, corr=_corr(0.5))
    ev = [e for e in r.events if e.limit == "portfolio_vol_target"][0]
    assert "because of correlation" not in ev.note and "correlation-aware" in ev.note


def test_thin_history_falls_back_and_logs_it():
    thin = estimate_correlation(["A", "B"], np.zeros((CORR_MIN_OBS - 1, 2)))
    assert thin.matrix is None
    r = apply_limits(W, _lim(0.10), vols=V, corr=thin)
    limits = [e.limit for e in r.events]
    assert limits == ["portfolio_vol_fallback", "portfolio_vol_target"]
    assert r.events[1].before == pytest.approx(independent_vol(W, V))
    assert f"< {CORR_MIN_OBS}" in r.events[0].note


def test_estimate_missing_a_held_name_falls_back():
    partial = CorrelationEstimate(tickers=("A",), matrix=np.eye(1), n_obs=120)
    assert correlated_vol(W, V, partial) is None
    r = apply_limits(W, _lim(0.30), vols=V, corr=partial)
    assert [e.limit for e in r.events] == ["portfolio_vol_fallback"]
    assert "every held name" in r.events[0].note


def test_without_an_estimate_behaviour_is_unchanged():
    r = apply_limits(W, _lim(0.10), vols=V)
    assert [e.limit for e in r.events] == ["portfolio_vol_target"]
    assert r.events[0].before == pytest.approx(math.sqrt(2) * 0.1)


# --- from the price book ----------------------------------------------------

def test_book_estimate_is_point_in_time_and_deterministic():
    names = ["AAPL", "MSFT", "XOM"]
    a = PriceBook(SyntheticProvider(), "2024-01-01", "2024-12-31")
    b = PriceBook(SyntheticProvider(), "2024-01-01", "2024-12-31")
    ra = a.aligned_log_returns(names, "2024-06-14", CORR_LOOKBACK)
    assert ra.shape == (CORR_LOOKBACK, 3)
    assert np.array_equal(ra, b.aligned_log_returns(names, "2024-06-14", CORR_LOOKBACK))
    last = float(np.log(a.closes_until("AAPL", "2024-06-14")[-1] / a.closes_until("AAPL", "2024-06-14")[-2]))
    assert ra[-1, 0] == pytest.approx(last)   # newest row is the as-of day, nothing after
    ea, eb = estimate_correlation(names, ra), estimate_correlation(names, b.aligned_log_returns(names, "2024-06-14", CORR_LOOKBACK))
    assert np.array_equal(ea.matrix, eb.matrix)


def test_run_cycle_uses_the_correlated_estimate_and_the_flag_restores_the_old_one():
    from pathlib import Path
    from consilium.backtest import backtest_fund
    from consilium.core.spec import Fund, load_spec
    spec = load_spec(Path(__file__).resolve().parent.parent / "consilium" / "mandates" / "systematic-trend.yaml")
    tight = spec.model_copy(update={"risk": spec.risk.model_copy(update={"portfolio_vol_target": 0.03})})
    off = tight.model_copy(update={"risk": tight.risk.model_copy(update={"vol_correlation": False})})
    uni = ["AAPL", "MSFT", "JPM", "XOM", "PG"]
    on_res = backtest_fund(Fund(tight), "2024-01-01", "2024-03-31", SyntheticProvider(), uni, monte_carlo_paths=10)
    off_res = backtest_fund(Fund(off), "2024-01-01", "2024-03-31", SyntheticProvider(), uni, monte_carlo_paths=10)
    on_notes = [e.note for r in on_res.records for e in r.risk_events if e.limit == "portfolio_vol_target"]
    off_notes = [e.note for r in off_res.records for e in r.risk_events if e.limit == "portfolio_vol_target"]
    assert on_notes and all("correlation-aware" in n for n in on_notes)
    assert off_notes and all("independent estimate" in n for n in off_notes)
    assert not any(e.limit == "portfolio_vol_fallback" for r in on_res.records for e in r.risk_events)
