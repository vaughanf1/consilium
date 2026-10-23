"""Correlation — how much of the book's risk is really one bet.

The independent estimate √(Σ (w·σ)²) treats ten tech longs as ten separate
risks. They are not: they share a factor, and on a bad day they fall together.
Portfolio vol is √(wᵀ Σ w) with Σ = D·R·D — the same per-name vols the sizer
uses on the diagonal, a correlation matrix R between them. With R = I this is
exactly the independent estimate, so the change is only ever about correlation.

R is an exponentially weighted correlation of daily log returns (recent days
count more, so a regime change shows up within weeks), then shrunk toward the
identity. Shrinkage is the guard against false confidence: 60 noisy days can
produce a matrix that claims two names hedge each other perfectly. Pulling every
off-diagonal toward zero by CORR_SHRINKAGE means no estimated correlation can
exceed ±(1 − CORR_SHRINKAGE). Below CORR_MIN_OBS overlapping days there is no
matrix at all and the caller falls back to the independent estimate — and says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

CORR_LOOKBACK = 120    # trading days of return history considered
CORR_HALFLIFE = 20     # days for an observation's weight to halve
CORR_SHRINKAGE = 0.3   # λ in (1 − λ)·R + λ·I
CORR_MIN_OBS = 40      # fewer overlapping days than this: no matrix, fall back
DEFAULT_VOL = 0.25     # a name with no measured vol is assumed ordinary, not safe


@dataclass(frozen=True)
class CorrelationEstimate:
    tickers: tuple[str, ...]
    matrix: np.ndarray | None   # shrunk correlation; None when history was too thin
    n_obs: int


def ewma_correlation(returns: np.ndarray, halflife: float = CORR_HALFLIFE) -> np.ndarray:
    """T×N returns (oldest row first) → N×N exponentially weighted correlation.
    A series with no variance correlates with nothing."""
    t, n = returns.shape
    age = np.arange(t - 1, -1, -1, dtype=float)
    w = 0.5 ** (age / halflife)
    w /= w.sum()
    dev = returns - w @ returns
    cov = (dev * w[:, None]).T @ dev
    sd = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    live = sd > 0
    corr = np.zeros((n, n))
    corr[np.ix_(live, live)] = cov[np.ix_(live, live)] / np.outer(sd[live], sd[live])
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    return corr


def shrink(corr: np.ndarray, shrinkage: float = CORR_SHRINKAGE) -> np.ndarray:
    """(1 − λ)·R + λ·I: a convex blend of two valid correlation matrices, so the
    result is still one, with every off-diagonal pulled toward zero."""
    return (1 - shrinkage) * corr + shrinkage * np.eye(len(corr))


def estimate_correlation(tickers: list[str], returns: np.ndarray) -> CorrelationEstimate:
    """`returns` is T×len(tickers), date-aligned, oldest first."""
    names = tuple(tickers)
    n_obs = int(returns.shape[0]) if returns.ndim == 2 else 0
    if n_obs < CORR_MIN_OBS or len(names) == 0:
        return CorrelationEstimate(tickers=names, matrix=None, n_obs=n_obs)
    return CorrelationEstimate(tickers=names, matrix=shrink(ewma_correlation(returns[-CORR_LOOKBACK:])),
                               n_obs=min(n_obs, CORR_LOOKBACK))


def independent_vol(weights: dict[str, float], vols: dict[str, float | None]) -> float:
    return math.sqrt(sum((x * (vols.get(t) or DEFAULT_VOL)) ** 2 for t, x in weights.items()))


def correlated_vol(weights: dict[str, float], vols: dict[str, float | None], corr: CorrelationEstimate) -> float | None:
    """√(wᵀ D R D w) over the names actually held. None when the estimate has no
    matrix or does not cover every one of them — the caller must fall back."""
    active = sorted(t for t, x in weights.items() if x != 0)
    if not active:
        return 0.0
    if corr.matrix is None:
        return None
    idx = {t: i for i, t in enumerate(corr.tickers)}
    if any(t not in idx for t in active):
        return None
    sel = [idx[t] for t in active]
    r = corr.matrix[np.ix_(sel, sel)]
    wd = np.array([weights[t] * (vols.get(t) or DEFAULT_VOL) for t in active])
    return math.sqrt(max(0.0, float(wd @ r @ wd)))
