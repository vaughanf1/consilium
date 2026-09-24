"""CIO — the committee chair. Synthesizes pods + red team into the final book.

Inputs: netted pod convictions, red-team haircuts, the benchmark's regime.
Outputs: final conviction per name, a regime stance, an exposure multiplier in
[0, 1], and a memo. The CIO may move a name's conviction by at most ±0.25 and
may never raise it above the haircut-adjusted consensus by more than that —
"the LLM never touches the trade" holds because sizing and risk come after.

Rules fallback: apply haircuts, then set the multiplier from the benchmark's
200-day trend and vol bucket (the simplest regime model that actually works).
"""

from __future__ import annotations

import logging

from consilium.core.models import CIOMemo, RedTeamNote
from consilium.data.features import MarketSnapshot
from consilium.llm import LLMClient, PromptCache, extract_json
from consilium.llm.cache import prompt_key

logger = logging.getLogger(__name__)
_MAX_MOVE = 0.25

_SYSTEM = """You are the CHIEF INVESTMENT OFFICER chairing an investment committee. You have
the desk's netted convictions per name, the Red Team's haircuts and objections,
and the market regime. Produce the FINAL convictions and an exposure stance.

Principles:
- Start from the haircut-adjusted consensus. Move a name only with a stated reason,
  and never by more than 0.25.
- Regime sets exposure: risk-on -> multiplier near 1.0; neutral -> 0.7-0.9;
  risk-off (downtrend + elevated vol, or deep drawdown) -> 0.3-0.6.
- Prefer fewer, higher-quality positions over many marginal ones: zero out names
  with weak, contested theses.
- Write a short memo a portfolio manager could act on.

Respond with JSON only:
{"regime": "risk-on" | "neutral" | "risk-off", "exposure_multiplier": <0-1>,
 "final_convictions": {"TICKER": <-1..1>, ...},
 "overrides": {"TICKER": "<why you moved it>", ...},
 "memo": "<3-6 sentences>"}"""


def regime_from_market(m: MarketSnapshot | None) -> tuple[str, float]:
    if m is None or m.ma200_rel is None:
        return "neutral", 0.85
    trend_up = m.ma200_rel > 0
    vol = m.vol_bucket
    dd = m.drawdown or 0.0
    if trend_up and vol in ("low", "normal"):
        return "risk-on", 1.0
    if (not trend_up and vol in ("elevated", "extreme")) or dd < -0.20:
        return "risk-off", 0.45
    if not trend_up:
        return "neutral", 0.7
    return "neutral", 0.85


def apply_haircuts(convictions: dict[str, float], notes: list[RedTeamNote]) -> dict[str, float]:
    h = {n.ticker: n.haircut for n in notes}
    return {t: c * (1.0 - h.get(t, 0.0)) for t, c in convictions.items()}


def rules_cio(convictions: dict[str, float], notes: list[RedTeamNote], bench: MarketSnapshot | None) -> CIOMemo:
    regime, mult = regime_from_market(bench)
    final = apply_haircuts(convictions, notes)
    longs = sorted([(t, c) for t, c in final.items() if c > 0.1], key=lambda x: -x[1])[:3]
    shorts = sorted([(t, c) for t, c in final.items() if c < -0.1], key=lambda x: x[1])[:3]
    memo = (f"Regime {regime} ({bench.render_coarse() if bench else 'benchmark regime unknown'}). "
            f"Exposure multiplier {mult:.2f}. Red Team applied {len(notes)} haircut(s)"
            + (f", largest on {max(notes, key=lambda n: n.haircut).ticker} ({max(n.haircut for n in notes):.0%})." if notes else ".")
            + (f" Leading longs: {', '.join(f'{t} {c:+.2f}' for t, c in longs)}." if longs else "")
            + (f" Leading shorts: {', '.join(f'{t} {c:+.2f}' for t, c in shorts)}." if shorts else ""))
    return CIOMemo(regime=regime, exposure_multiplier=mult, final_convictions=final, memo=memo, source="rules")


def llm_cio(client: LLMClient, cache: PromptCache, as_of: str, convictions: dict[str, float],
            notes: list[RedTeamNote], bench: MarketSnapshot | None, pod_summaries: list[str]) -> CIOMemo:
    fallback = rules_cio(convictions, notes, bench)
    adjusted = fallback.final_convictions
    lines = [f"Benchmark regime: {bench.render_coarse() if bench else 'unknown'}", "",
             "Pods:", *pod_summaries, "",
             "Haircut-adjusted consensus (ticker: raw -> adjusted):"]
    for t in sorted(adjusted, key=lambda x: -abs(adjusted[x])):
        lines.append(f"  {t}: {convictions[t]:+.2f} -> {adjusted[t]:+.2f}")
    if notes:
        lines += ["", "Red Team:"]
        for n in notes:
            lines.append(f"  {n.ticker} haircut {n.haircut:.0%}: {n.strongest_objection}")
    user = "\n".join(lines)
    key = prompt_key("cio", client.model, _SYSTEM, user)
    cached = cache.get(key)
    parsed = cached.get("parsed") if cached else None
    if parsed is None:
        try:
            response = client.complete(_SYSTEM, user)
            parsed = extract_json(response)
            cache.put(key, {"role": "cio", "model": client.model, "as_of": as_of, "system": _SYSTEM,
                            "user": user, "response": response, "parsed": parsed})
        except Exception as exc:
            logger.warning("CIO LLM failed (%s); using rules", exc)
            return fallback
    regime = parsed.get("regime") if parsed.get("regime") in ("risk-on", "neutral", "risk-off") else fallback.regime
    try:
        mult = min(1.0, max(0.0, float(parsed.get("exposure_multiplier", fallback.exposure_multiplier))))
    except (TypeError, ValueError):
        mult = fallback.exposure_multiplier
    final: dict[str, float] = {}
    proposed = parsed.get("final_convictions") or {}
    for t, base in adjusted.items():
        try:
            want = float(proposed.get(t, base))
        except (TypeError, ValueError):
            want = base
        # bounded authority: within ±MAX_MOVE of the adjusted consensus, never sign-flipping past zero
        lo, hi = base - _MAX_MOVE, base + _MAX_MOVE
        v = min(hi, max(lo, want))
        if base > 0:
            v = max(0.0, min(v, 1.0))
        elif base < 0:
            v = min(0.0, max(v, -1.0))
        else:
            v = 0.0
        final[t] = v
    overrides = {str(k).upper(): str(v)[:300] for k, v in (parsed.get("overrides") or {}).items() if str(k).upper() in final}
    return CIOMemo(regime=regime, exposure_multiplier=mult, final_convictions=final,
                   memo=str(parsed.get("memo", ""))[:1500] or fallback.memo, overrides=overrides, source="llm")
