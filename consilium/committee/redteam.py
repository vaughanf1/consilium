"""Red Team — the committee's designated adversary.

Takes the desk's highest-conviction longs and shorts, reads every analyst's
thesis and risks for them, and returns a HAIRCUT in [0, max_haircut] per name
plus the strongest objection. The haircut multiplies the conviction; it can
never flip a sign or add exposure — the adversary can only make the fund
smaller and more honest.

With an LLM: one call for the whole slate. Without: a rules haircut from
analyst disagreement (dispersion) and abstention rate, so the mechanism still
exists in a keyless run.
"""

from __future__ import annotations

import logging

from consilium.core.models import RedTeamNote, View
from consilium.llm import LLMClient, PromptCache, extract_json
from consilium.llm.cache import prompt_key

logger = logging.getLogger(__name__)

_SYSTEM = """You are the RED TEAM of an investment committee. Your only job is to attack the
desk's consensus. For each position you are shown, find the single strongest
objection and decide how much the thesis should be HAIRCUT (0 = it survives
intact, 100 = it should be abandoned). Be specific and quantitative where the
data allows. Punish theses that rest on one analyst, that ignore a risk another
analyst named, or that lean on a stretched price regime. Do not be contrarian for
sport: a robust thesis deserves a small haircut.

Respond with JSON only:
{"notes": [{"ticker": "...", "haircut": <0-100>, "strongest_objection": "<1-2 sentences>",
            "what_would_change_my_mind": "<1 sentence>"}, ...]}"""


def _slate(convictions: dict[str, float], top_n: int) -> list[str]:
    ranked = sorted(convictions.items(), key=lambda kv: -abs(kv[1]))
    return [t for t, c in ranked[:top_n] if abs(c) > 0.05]


def rules_red_team(convictions: dict[str, float], dispersion: dict[str, float], views: list[View],
                   top_n: int, max_haircut: float) -> list[RedTeamNote]:
    notes = []
    for t in _slate(convictions, top_n):
        vs = [v for v in views if v.ticker == t]
        n_abst = sum(1 for v in vs if v.abstained)
        n_vote = len(vs) - n_abst
        voting = [v.conviction for v in vs if not v.abstained]
        if len(voting) >= 2:
            mu = sum(voting) / len(voting)
            disp = (sum((x - mu) ** 2 for x in voting) / (len(voting) - 1)) ** 0.5
        else:
            disp = dispersion.get(t, 0.0)
        # disagreement of 0.5 (one bullish, one bearish) -> big haircut; a lone vote -> moderate
        h = min(max_haircut, 0.8 * disp + (0.25 if n_vote <= 1 else 0.0) + 0.15 * (n_abst / max(1, len(vs))))
        dissent = [v for v in vs if not v.abstained and v.conviction * convictions[t] < 0]
        objection = (f"{len(dissent)} of {n_vote} voting analysts disagree with the consensus "
                     f"(dispersion {disp:.2f})" if dissent else
                     (f"only {n_vote} analyst(s) voted; thin support" if n_vote <= 1 else
                      f"consensus is unanimous; haircut reflects dispersion {disp:.2f}"))
        if dissent:
            objection += f" — e.g. {dissent[0].analyst}: {dissent[0].thesis[:140]}"
        notes.append(RedTeamNote(ticker=t, consensus=convictions[t], haircut=round(h, 3),
                                 strongest_objection=objection,
                                 what_would_change_my_mind="analyst agreement, or a second independent source",
                                 source="rules"))
    return notes


def llm_red_team(client: LLMClient, cache: PromptCache, as_of: str, convictions: dict[str, float],
                 views: list[View], top_n: int, max_haircut: float, fallback: list[RedTeamNote]) -> list[RedTeamNote]:
    slate = _slate(convictions, top_n)
    if not slate:
        return []
    blocks = []
    for t in slate:
        lines = [f"== {t}: desk consensus {convictions[t]:+.2f} ({'LONG' if convictions[t] > 0 else 'SHORT'}) =="]
        for v in views:
            if v.ticker != t:
                continue
            if v.abstained:
                lines.append(f"- {v.analyst}: abstained ({v.metadata.get('abstain_reason', '')[:80]})")
            else:
                lines.append(f"- {v.analyst} [{v.stance} {abs(v.conviction):.0%}, conf {v.confidence:.0%}, {v.horizon_days}d]: {v.thesis}"
                             + (f" Risks: {'; '.join(v.risks)}" if v.risks else ""))
        blocks.append("\n".join(lines))
    user = "\n\n".join(blocks)
    key = prompt_key("redteam", client.model, _SYSTEM, user)
    cached = cache.get(key)
    parsed = cached.get("parsed") if cached else None
    if parsed is None:
        try:
            response = client.complete(_SYSTEM, user)
            parsed = extract_json(response)
            cache.put(key, {"role": "redteam", "model": client.model, "as_of": as_of,
                            "system": _SYSTEM, "user": user, "response": response, "parsed": parsed})
        except Exception as exc:
            logger.warning("red team LLM failed (%s); using rules", exc)
            return fallback
    by_ticker = {n.ticker: n for n in fallback}
    out = []
    for item in parsed.get("notes", []):
        t = str(item.get("ticker", "")).upper()
        if t not in convictions:
            continue
        try:
            h = min(max_haircut, max(0.0, float(item.get("haircut", 0)) / 100.0))
        except (TypeError, ValueError):
            h = by_ticker[t].haircut if t in by_ticker else 0.0
        out.append(RedTeamNote(ticker=t, consensus=convictions[t], haircut=round(h, 3),
                               strongest_objection=str(item.get("strongest_objection", ""))[:600],
                               what_would_change_my_mind=str(item.get("what_would_change_my_mind", ""))[:300],
                               source="llm"))
    seen = {n.ticker for n in out}
    out.extend(n for n in fallback if n.ticker not in seen)
    return out
