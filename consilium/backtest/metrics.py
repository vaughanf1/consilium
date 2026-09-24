"""Performance statistics a PM actually looks at — not just return and Sharpe."""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel

PERIODS = {"daily": 252, "weekly": 52, "biweekly": 26, "monthly": 12}


class Metrics(BaseModel):
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_days: int
    benchmark_return: float
    benchmark_cagr: float
    benchmark_max_drawdown: float
    excess_return: float
    beta: float
    alpha: float                  # annualized Jensen's alpha
    information_ratio: float
    tracking_error: float
    hit_rate: float               # fraction of periods with positive return
    best_period: float
    worst_period: float
    avg_gross_exposure: float
    avg_turnover: float           # per period
    total_costs: float
    cost_drag: float              # total costs / starting capital
    n_periods: int
    n_orders: int
    years: float


def _returns(curve: np.ndarray) -> np.ndarray:
    return curve[1:] / curve[:-1] - 1


def max_drawdown(curve: np.ndarray) -> tuple[float, int]:
    peak, mdd, mdd_len, run = curve[0], 0.0, 0, 0
    for v in curve:
        if v >= peak:
            peak, run = v, 0
        else:
            run += 1
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd, mdd_len = dd, run
    return float(mdd), int(mdd_len)


def compute_metrics(capital: float, nav: list[float], bench: list[float], cadence: str, years: float,
                    gross_exposures: list[float], turnovers: list[float], total_costs: float, n_orders: int,
                    rf_annual: float = 0.02) -> Metrics:
    ppy = PERIODS[cadence]
    curve = np.array([capital] + nav, dtype=float)
    bcurve = np.array([capital] + bench, dtype=float)
    r, br = _returns(curve), _returns(bcurve)
    rf = rf_annual / ppy
    years = max(years, 1e-6)
    total = curve[-1] / capital - 1
    btotal = bcurve[-1] / capital - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    bcagr = (1 + btotal) ** (1 / years) - 1 if btotal > -1 else -1.0
    vol = float(r.std(ddof=1) * math.sqrt(ppy)) if len(r) > 1 else 0.0
    ex = r - rf
    sharpe = float(ex.mean() / r.std(ddof=1) * math.sqrt(ppy)) if len(r) > 1 and r.std(ddof=1) > 0 else 0.0
    downside = r[r < rf] - rf
    dstd = float(np.sqrt((downside ** 2).sum() / max(1, len(r)))) if len(downside) else 0.0
    sortino = float(ex.mean() / dstd * math.sqrt(ppy)) if dstd > 0 else 0.0
    mdd, mdd_len = max_drawdown(curve)
    bmdd, _ = max_drawdown(bcurve)
    calmar = cagr / mdd if mdd > 0 else 0.0
    if len(r) > 2 and br.std(ddof=1) > 0:
        beta = float(np.cov(r, br, ddof=1)[0, 1] / br.var(ddof=1))
    else:
        beta = 0.0
    alpha = float((r.mean() - rf - beta * (br.mean() - rf)) * ppy)
    active = r - br
    te = float(active.std(ddof=1) * math.sqrt(ppy)) if len(active) > 1 else 0.0
    ir = float(active.mean() * ppy / te) if te > 0 else 0.0
    return Metrics(
        total_return=round(float(total), 6), cagr=round(float(cagr), 6), volatility=round(vol, 6),
        sharpe=round(sharpe, 4), sortino=round(sortino, 4), calmar=round(float(calmar), 4),
        max_drawdown=round(mdd, 6), max_drawdown_days=mdd_len,
        benchmark_return=round(float(btotal), 6), benchmark_cagr=round(float(bcagr), 6), benchmark_max_drawdown=round(bmdd, 6),
        excess_return=round(float(total - btotal), 6), beta=round(beta, 4), alpha=round(alpha, 6),
        information_ratio=round(ir, 4), tracking_error=round(te, 6),
        hit_rate=round(float((r > 0).mean()) if len(r) else 0.0, 4),
        best_period=round(float(r.max()) if len(r) else 0.0, 6), worst_period=round(float(r.min()) if len(r) else 0.0, 6),
        avg_gross_exposure=round(float(np.mean(gross_exposures)) if gross_exposures else 0.0, 4),
        avg_turnover=round(float(np.mean(turnovers)) if turnovers else 0.0, 4),
        total_costs=round(float(total_costs), 2), cost_drag=round(float(total_costs / capital), 6),
        n_periods=len(r), n_orders=n_orders, years=round(years, 4),
    )


def rolling_sharpe(capital: float, nav: list[float], cadence: str, window: int | None = None, rf_annual: float = 0.02) -> list[float | None]:
    ppy = PERIODS[cadence]
    window = window or max(6, ppy // 2)
    r = _returns(np.array([capital] + nav, dtype=float))
    rf = rf_annual / ppy
    out: list[float | None] = []
    for i in range(len(r)):
        if i + 1 < window:
            out.append(None)
            continue
        seg = r[i + 1 - window:i + 1]
        sd = seg.std(ddof=1)
        out.append(round(float((seg.mean() - rf) / sd * math.sqrt(ppy)), 3) if sd > 0 else 0.0)
    return out


def monte_carlo(capital: float, nav: list[float], n_paths: int = 500, seed: int = 7) -> dict:
    """Bootstrap the realized per-period returns (with replacement) into
    n_paths alternative histories of the same length. Reports the 5/25/50/75/95
    percentile terminal-NAV bands and the probability of loss — the honest
    answer to "how lucky was this backtest?"."""
    r = _returns(np.array([capital] + nav, dtype=float))
    if len(r) < 4:
        return {"paths": 0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(r), size=(n_paths, len(r)))
    curves = capital * np.cumprod(1 + r[idx], axis=1)
    pct = np.percentile(curves, [5, 25, 50, 75, 95], axis=0)
    terminal = curves[:, -1]
    mdds = [max_drawdown(np.concatenate([[capital], c]))[0] for c in curves[:: max(1, n_paths // 200)]]
    return {
        "paths": n_paths,
        "bands": {"p5": pct[0].round(2).tolist(), "p25": pct[1].round(2).tolist(), "p50": pct[2].round(2).tolist(),
                  "p75": pct[3].round(2).tolist(), "p95": pct[4].round(2).tolist()},
        "terminal": {"p5": float(np.percentile(terminal, 5)), "p50": float(np.percentile(terminal, 50)),
                     "p95": float(np.percentile(terminal, 95))},
        "prob_loss": float((terminal < capital).mean()),
        "median_max_drawdown": float(np.median(mdds)) if mdds else 0.0,
    }
