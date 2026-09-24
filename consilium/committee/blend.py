"""Blend — one strategy's analyst views into one conviction per ticker.

    conviction_t = Σ w_a · c_a · h_a · view_a  /  Σ w_a · c_a · h_a

w = mandate blend weight, c = the view's confidence (if confidence_weighted),
h = horizon factor: a 10-day reversal view is worth min(1, 10 / rebalance_gap)
of a view that outlives the gap. Abstentions are excluded from numerator AND
denominator: "no opinion" never masquerades as "neutral".
"""

from __future__ import annotations

from pydantic import BaseModel

from consilium.core.models import View
from consilium.core.spec import BlendPolicy


class BlendResult(BaseModel):
    convictions: dict[str, float]
    dispersion: dict[str, float]     # std-dev of voting views per ticker (disagreement)
    n_votes: dict[str, int]


def blend_views(views: list[View], weights: dict[str, float], policy: BlendPolicy, rebalance_days: int) -> BlendResult:
    num: dict[str, float] = {}
    den: dict[str, float] = {}
    votes: dict[str, list[float]] = {}
    for v in views:
        votes.setdefault(v.ticker, [])
        if v.abstained:
            continue
        w = weights.get(v.analyst, 1.0)
        if policy.confidence_weighted:
            w *= max(0.05, v.confidence)
        if policy.horizon_aware:
            w *= min(1.0, v.horizon_days / max(1, rebalance_days))
        num[v.ticker] = num.get(v.ticker, 0.0) + w * v.conviction
        den[v.ticker] = den.get(v.ticker, 0.0) + w
        votes[v.ticker].append(v.conviction)

    tickers = sorted(votes)
    conv = {t: (num[t] / den[t]) if den.get(t) else 0.0 for t in tickers}
    if policy.market_neutral and tickers:
        mean = sum(conv.values()) / len(conv)
        conv = {t: c - mean for t, c in conv.items()}

    disp: dict[str, float] = {}
    for t in tickers:
        vs = votes[t]
        if len(vs) >= 2:
            m = sum(vs) / len(vs)
            disp[t] = (sum((x - m) ** 2 for x in vs) / (len(vs) - 1)) ** 0.5
        else:
            disp[t] = 0.0
    return BlendResult(convictions=conv, dispersion=disp, n_votes={t: len(votes[t]) for t in tickers})
