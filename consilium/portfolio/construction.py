"""Portfolio construction — final convictions into target weights.

vol_scaled (default): weight_t ∝ conviction_t × (reference_vol / vol_t). A +0.6
in a 60%-vol name gets a third of the dollars a +0.6 in a 20%-vol name gets, so
every position contributes comparable risk. The book is then normalized so
Σ|w| = gross_target × exposure_multiplier (the CIO's regime dial).

Convictions below `min_conviction` are zeroed: a lone weak view should not
receive capital just because it is the only view. `max_names` keeps the book
concentrated in the highest-conviction ideas.
"""

from __future__ import annotations

from pydantic import BaseModel

from consilium.core.spec import PortfolioPolicy


class SizingResult(BaseModel):
    weights: dict[str, float]
    dropped: dict[str, str]     # ticker -> why it got no capital


def size_book(convictions: dict[str, float], vols: dict[str, float | None], policy: PortfolioPolicy,
              gross_target: float, exposure_multiplier: float) -> SizingResult:
    dropped: dict[str, str] = {}
    raw: dict[str, float] = {}
    for t, c in convictions.items():
        if abs(c) < policy.min_conviction:
            dropped[t] = f"conviction {c:+.2f} below floor {policy.min_conviction}"
            continue
        scale = 1.0
        if policy.sizing == "vol_scaled":
            v = vols.get(t)
            if v is None or v <= 0:
                v = policy.reference_vol
            scale = min(2.5, policy.reference_vol / v)   # never lever a sleepy name more than 2.5x
        raw[t] = c * scale
    if policy.max_names and len(raw) > policy.max_names:
        keep = sorted(raw, key=lambda t: -abs(convictions[t]))[:policy.max_names]
        for t in list(raw):
            if t not in keep:
                dropped[t] = f"outside top {policy.max_names} by conviction"
                del raw[t]
    gross = sum(abs(w) for w in raw.values())
    target = gross_target * exposure_multiplier
    if gross < 1e-9:
        return SizingResult(weights={t: 0.0 for t in convictions}, dropped=dropped)
    weights = {t: (raw.get(t, 0.0) / gross) * target for t in convictions}
    return SizingResult(weights=weights, dropped=dropped)


def apply_rebalance_band(targets: dict[str, float], current: dict[str, float], band: float) -> dict[str, float]:
    """No-trade zone: keep the current weight where the change is smaller than
    `band`, unless the target is an exit (targets of ~0 always execute)."""
    out = {}
    for t in set(targets) | set(current):
        want, have = targets.get(t, 0.0), current.get(t, 0.0)
        if abs(want) < 1e-9 or abs(want - have) >= band:
            out[t] = want
        else:
            out[t] = have
    return out
