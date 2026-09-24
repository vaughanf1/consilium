"""Risk — hard limits the committee cannot override. Deterministic arithmetic.

Order (each stage only shrinks, so the sequence is idempotent):
1. Drawdown circuit breaker: beyond drawdown_soft the whole book is halved;
   beyond drawdown_hard it goes flat. The fund lives to fight another day.
2. Per-name cap.
3. Sector concentration cap: a sector's Σ|w| above max_sector_pct is scaled down.
4. Portfolio vol target: if √(Σ (w·σ)²) (the diversified-ish estimate that
   ignores correlation — conservative for longs, optimistic for L/S) exceeds
   the target, scale the book down.
5. Net exposure band, then gross cap.
Exposure removed by a clamp stays in cash — it is never redistributed.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from consilium.core.models import RiskEvent
from consilium.core.spec import RiskLimits


_TOL = 1e-4  # ignore float drift; a clamp should mean something


class RiskResult(BaseModel):
    weights: dict[str, float]
    events: list[RiskEvent]


def apply_limits(weights: dict[str, float], limits: RiskLimits, *, sectors: dict[str, str] | None = None,
                 vols: dict[str, float | None] | None = None, drawdown: float = 0.0) -> RiskResult:
    w = dict(weights)
    events: list[RiskEvent] = []
    sectors = sectors or {}
    vols = vols or {}

    def gross_of(d):
        return sum(abs(x) for x in d.values())

    # 1. circuit breaker
    dd = abs(drawdown)
    if limits.drawdown_hard and dd >= limits.drawdown_hard and gross_of(w) > 0:
        events.append(RiskEvent(limit="drawdown_hard", before=gross_of(w), after=0.0,
                                note=f"drawdown {dd:.1%} ≥ {limits.drawdown_hard:.0%}: book flattened"))
        w = {t: 0.0 for t in w}
    elif limits.drawdown_soft and dd >= limits.drawdown_soft and gross_of(w) > 0:
        g = gross_of(w)
        w = {t: x * 0.5 for t, x in w.items()}
        events.append(RiskEvent(limit="drawdown_soft", before=g, after=gross_of(w),
                                note=f"drawdown {dd:.1%} ≥ {limits.drawdown_soft:.0%}: exposure halved"))

    # 2. per-name cap
    for t in sorted(w):
        if abs(w[t]) > limits.max_position_pct * (1 + _TOL):
            new = math.copysign(limits.max_position_pct, w[t])
            events.append(RiskEvent(limit="max_position_pct", ticker=t, before=w[t], after=new))
            w[t] = new

    # 3. sector cap
    if limits.max_sector_pct:
        by_sector: dict[str, float] = {}
        for t, x in w.items():
            by_sector[sectors.get(t, "Unknown")] = by_sector.get(sectors.get(t, "Unknown"), 0.0) + abs(x)
        for sec, g in sorted(by_sector.items()):
            if g > limits.max_sector_pct * (1 + _TOL):
                scale = limits.max_sector_pct / g
                for t in w:
                    if sectors.get(t, "Unknown") == sec:
                        w[t] *= scale
                events.append(RiskEvent(limit="max_sector_pct", ticker=None, before=g, after=limits.max_sector_pct,
                                        note=f"{sec} scaled down"))

    # 4. portfolio vol target (correlation-free estimate)
    if limits.portfolio_vol_target:
        est = math.sqrt(sum((x * (vols.get(t) or 0.25)) ** 2 for t, x in w.items()))
        if est > limits.portfolio_vol_target and est > 0:
            scale = limits.portfolio_vol_target / est
            w = {t: x * scale for t, x in w.items()}
            events.append(RiskEvent(limit="portfolio_vol_target", before=est, after=limits.portfolio_vol_target,
                                    note="book scaled to vol target"))

    # 5. net band, then gross cap
    net = sum(w.values())
    if net > limits.max_net_exposure + _TOL or net < limits.min_net_exposure - _TOL:
        cap = limits.max_net_exposure if net > 0 else limits.min_net_exposure
        longs = {t: x for t, x in w.items() if (x > 0) == (net > 0)}
        excess = net - cap
        g_side = sum(abs(x) for x in longs.values())
        if g_side > 0:
            scale = max(0.0, (g_side - abs(excess)) / g_side)
            for t in longs:
                w[t] *= scale
        events.append(RiskEvent(limit="net_exposure", before=net, after=sum(w.values()), note="dominant side scaled"))

    gross = gross_of(w)
    if gross > limits.max_gross_exposure * (1 + _TOL):
        scale = limits.max_gross_exposure / gross
        w = {t: x * scale for t, x in w.items()}
        events.append(RiskEvent(limit="max_gross_exposure", before=gross, after=limits.max_gross_exposure))

    return RiskResult(weights=w, events=events)
