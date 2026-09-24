"""Quant analysts — pure math, deterministic, point-in-time by construction."""

from __future__ import annotations

import math

import numpy as np

from consilium.analysts.base import Analyst, AnalysisContext
from consilium.core.models import View


def _tanh(x: float, scale: float) -> float:
    return float(math.tanh(x / scale))


class TrendAnalyst(Analyst):
    """Time-series momentum: 12-1 momentum plus the 50/200-day structure.

    Conviction = tanh of a blended trend score; confidence rises with the
    agreement between the two horizons and falls with volatility (a trend in a
    50%-vol name is worth less than the same trend in a 15%-vol name).
    """
    name, display, kind, horizon_days = "trend", "Trend Follower", "quant", 63

    def view(self, ticker: str, ctx: AnalysisContext) -> View:
        m = ctx.market(ticker)
        if m is None or m.mom_12_1 is None or m.ma200_rel is None:
            return self._abstain(ticker, ctx.date, "need 12 months of prices")
        mom = _tanh(m.mom_12_1, 0.25)
        ma = _tanh(m.ma200_rel, 0.10)
        ma50 = _tanh(m.ma50_rel or 0.0, 0.06)
        score = 0.5 * mom + 0.35 * ma + 0.15 * ma50
        agree = 1.0 - min(1.0, abs(mom - ma))
        vol_pen = 1.0 if (m.vol_60 or 0.3) < 0.3 else max(0.4, 0.3 / (m.vol_60 or 0.3))
        conf = 0.35 + 0.45 * agree * vol_pen
        thesis = (f"12-1 momentum {m.mom_12_1:+.0%}, price {m.ma200_rel:+.1%} vs 200dma, "
                  f"{(m.ma50_rel or 0):+.1%} vs 50dma; {m.vol_bucket} vol.")
        risks = ["trend reversal / mean-reversion shock"] + (["elevated volatility"] if (m.vol_60 or 0) > 0.32 else [])
        return self._view(ticker, ctx.date, score, conf, thesis, risks,
                          {"mom_12_1": m.mom_12_1, "ma200_rel": m.ma200_rel, "ma50_rel": m.ma50_rel or 0.0})


class MeanReversionAnalyst(Analyst):
    """Short-term reversal: fades 5-day moves that are large relative to recent vol,
    confirmed by RSI extremes. Deliberately short horizon; low weight in most mandates."""
    name, display, kind, horizon_days = "meanrev", "Mean Reversion", "quant", 10

    def view(self, ticker: str, ctx: AnalysisContext) -> View:
        m = ctx.market(ticker)
        if m is None or m.zscore_5 is None or m.rsi_14 is None:
            return self._abstain(ticker, ctx.date, "need 60 days of prices")
        z = m.zscore_5
        rsi_term = (50.0 - m.rsi_14) / 30.0
        score = -0.7 * _tanh(z, 1.5) + 0.3 * _tanh(rsi_term, 1.0)
        # only act on genuine extremes; otherwise a soft neutral
        strength = min(1.0, max(abs(z) - 1.0, 0.0) / 1.5)
        conf = 0.2 + 0.5 * strength
        if strength == 0:
            score *= 0.25
        thesis = f"5-day move is {z:+.1f}σ of 60-day vol, RSI(14) {m.rsi_14:.0f}; fading the extreme."
        return self._view(ticker, ctx.date, score, conf, thesis, ["momentum continuation"],
                          {"zscore_5": z, "rsi_14": m.rsi_14})


class LowVolQualityAnalyst(Analyst):
    """Defensive tilt: prefers names whose current vol is low versus their own
    history and whose drawdown is shallow. Leans positive in calm names,
    negative when a name's own vol regime blows out (a classic risk-off tell)."""
    name, display, kind, horizon_days = "lowvol", "Low-Vol Defensive", "quant", 42

    def view(self, ticker: str, ctx: AnalysisContext) -> View:
        m = ctx.market(ticker)
        if m is None or m.vol_20 is None or m.vol_252 is None:
            return self._abstain(ticker, ctx.date, "need a year of prices")
        ratio = m.vol_20 / m.vol_252 if m.vol_252 > 0 else 1.0
        vol_term = _tanh(1.0 - ratio, 0.35)                # calm vs own history -> positive
        level_term = _tanh(0.25 - m.vol_252, 0.15)          # absolutely low vol -> positive
        dd_term = _tanh((m.drawdown or 0) + 0.10, 0.12)     # shallow drawdown -> positive
        score = 0.45 * vol_term + 0.30 * level_term + 0.25 * dd_term
        conf = 0.45
        thesis = f"20d vol {m.vol_20:.0%} vs 1y {m.vol_252:.0%} (ratio {ratio:.2f}); drawdown {(m.drawdown or 0):+.0%}."
        return self._view(ticker, ctx.date, score, conf, thesis, ["low-vol names lag in sharp rallies"],
                          {"vol_ratio": ratio, "vol_252": m.vol_252, "drawdown": m.drawdown or 0.0})


class ValueQualityAnalyst(Analyst):
    """Fundamental factor composite: cheapness (earnings/FCF yield, EV/EBITDA),
    quality (ROE, margins, leverage), and growth. Each sub-score is a bounded
    transform of the ratio against sensible priors, so the composite is
    interpretable and never dominated by one wild ratio."""
    name, display, kind, horizon_days = "value_quality", "Value & Quality Factors", "quant", 126
    needs_fundamentals = True

    def view(self, ticker: str, ctx: AnalysisContext) -> View:
        f = ctx.fundamentals(ticker)
        if f is None:
            return self._abstain(ticker, ctx.date, "no fundamentals")
        parts: dict[str, float] = {}
        if f.pe is not None and f.pe > 0:
            parts["earnings_yield"] = _tanh((1 / f.pe - 0.05) , 0.03)
        if f.fcf_yield is not None:
            parts["fcf_yield"] = _tanh(f.fcf_yield - 0.04, 0.03)
        if f.ev_ebitda is not None and f.ev_ebitda > 0:
            parts["ev_ebitda"] = _tanh((12 - f.ev_ebitda) / 12, 0.6)
        if f.roe is not None:
            parts["roe"] = _tanh(f.roe - 0.12, 0.10)
        if f.operating_margin is not None:
            parts["op_margin"] = _tanh(f.operating_margin - 0.12, 0.10)
        if f.debt_to_equity is not None:
            parts["leverage"] = _tanh(1.0 - f.debt_to_equity, 0.8)
        if f.revenue_growth is not None:
            parts["growth"] = _tanh(f.revenue_growth - 0.05, 0.12)
        if len(parts) < 3:
            return self._abstain(ticker, ctx.date, "too few fundamental fields")
        value = np.mean([v for k, v in parts.items() if k in ("earnings_yield", "fcf_yield", "ev_ebitda")] or [0])
        quality = np.mean([v for k, v in parts.items() if k in ("roe", "op_margin", "leverage")] or [0])
        growth = parts.get("growth", 0.0)
        score = 0.4 * value + 0.4 * quality + 0.2 * growth
        conf = 0.35 + 0.05 * len(parts) - (0.15 if not f.point_in_time else 0.0)
        thesis = (f"Value {value:+.2f} (P/E {f.pe:.0f}x)" if f.pe else f"Value {value:+.2f}") + \
                 f", quality {quality:+.2f} (ROE {(f.roe or 0):.0%}, op margin {(f.operating_margin or 0):.0%}), growth {growth:+.2f}."
        return self._view(ticker, ctx.date, float(score), conf, thesis,
                          ["value traps", "stale fundamentals" if not f.point_in_time else "multiple compression"],
                          {k: float(v) for k, v in parts.items()}, point_in_time=f.point_in_time)
