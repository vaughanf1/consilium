"""The Investment Board — fund-manager archetypes who vote on the CIO's proposal.

Where the analysts argue about individual companies, the board argues about
PORTFOLIO RISK: how much of the book to deploy, and which positions are too
uncomfortable to carry at full size. That is the decision a real investment
committee actually makes.

Bounded authority, like every other LLM stage:
- A member votes an exposure in [0, 1] and may flag names to trim.
- The resolution is the MEDIAN vote (one outlier cannot hijack the board),
  clamped to within ±0.35 of the CIO's proposal.
- A name is trimmed only when at least half the voting members flag it, and a
  trim halves conviction — it never flips a sign and never adds a position.

Each seat can run on a DIFFERENT model, so the board is a genuine disagreement
between models rather than one model talking to itself. With no key at all,
every archetype has a deterministic rule version and the board still sits.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from statistics import median
from typing import Callable

from consilium.core.models import BoardResolution, BoardVote, CIOMemo, RedTeamNote
from consilium.data.features import MarketSnapshot
from consilium.llm import LLMError, PromptCache, extract_json, make_llm
from consilium.llm.cache import prompt_key

logger = logging.getLogger(__name__)

_MAX_MOVE = 0.35        # how far the board may move the CIO's exposure
_TRIM_FACTOR = 0.5      # a trimmed name keeps half its conviction
_DISSENT_GAP = 0.2      # a vote this far from the resolution is recorded as dissent


@dataclass
class Briefing:
    """What every board member is shown. Identical for all seats, so a
    disagreement is about judgement, not information."""

    regime: str
    cio_exposure: float
    cio_memo: str
    drawdown: float
    dispersion: float                 # average analyst disagreement this cycle
    convictions: dict[str, float]
    red_team: list[RedTeamNote]
    market: MarketSnapshot | None
    n_positions: int

    def render(self) -> str:
        top = sorted(self.convictions.items(), key=lambda kv: -abs(kv[1]))[:8]
        lines = [
            f"Market regime: {self.market.render_coarse() if self.market else 'unknown'}",
            f"CIO proposal: {self.regime}, deploy {self.cio_exposure:.0%} of the book.",
            f"CIO memo: {self.cio_memo[:400]}",
            f"Fund drawdown from high-water mark: {self.drawdown:+.1%}.",
            f"Average analyst disagreement (0 = unanimous, 1 = split): {self.dispersion:.2f}.",
            f"Proposed positions: {self.n_positions}.",
            "",
            "Proposed book (ticker: conviction after analyst blending, Red Team haircuts and the CIO):",
        ]
        lines += [f"  {t}: {c:+.2f}" for t, c in top]
        if self.red_team:
            lines += ["", "Red Team objections:"]
            lines += [f"  {n.ticker} (haircut {n.haircut:.0%}): {n.strongest_objection[:200]}" for n in self.red_team]
        return "\n".join(lines)


@dataclass
class Seat:
    name: str
    display: str
    persona: str
    rule: Callable[[Briefing], tuple[str, float, list[str], str]]


# ---------------------------------------------------------------------------
# Rule versions — deterministic, and genuinely different from each other, so a
# keyless board still produces a real argument.
# ---------------------------------------------------------------------------

def _rule_macro(b: Briefing):
    exposure = {"risk-on": 1.0, "neutral": 0.75, "risk-off": 0.35}[b.regime]
    if b.drawdown < -0.08:
        exposure *= 0.7
    stance = "add risk" if exposure > b.cio_exposure + 0.05 else "cut risk" if exposure < b.cio_exposure - 0.05 else "hold"
    return stance, exposure, [], (
        f"Regime is {b.regime}; I size to the regime and nothing else. "
        f"{'Drawdown argues for less' if b.drawdown < -0.08 else 'No drawdown constraint yet'}.")


def _rule_risk_budgeter(b: Briefing):
    exposure = 0.85 - 0.5 * b.dispersion - 2.0 * max(0.0, -b.drawdown)
    if b.n_positions and b.n_positions < 4:
        exposure -= 0.1                       # too few names to call it diversified
    exposure = max(0.15, min(1.0, exposure))
    trim = [t for t, c in sorted(b.convictions.items(), key=lambda kv: -abs(kv[1]))[:2] if abs(c) > 0.45]
    stance = "cut risk" if exposure < b.cio_exposure - 0.05 else "add risk" if exposure > b.cio_exposure + 0.05 else "hold"
    return stance, exposure, trim, (
        f"Disagreement is {b.dispersion:.2f} and drawdown {b.drawdown:+.1%}. "
        f"{'The largest positions breach my per-name budget.' if trim else 'Position sizes are inside budget.'}")


def _rule_concentrator(b: Briefing):
    exposure = 0.95 if b.drawdown > -0.15 else 0.6
    return ("add risk" if exposure > b.cio_exposure + 0.05 else "hold"), exposure, [], (
        "Our edge is conviction, not diversification. Trimming winners to feel comfortable is how returns die. "
        + ("Drawdown is deep enough to respect." if b.drawdown <= -0.15 else "This drawdown is noise."))


def _rule_systematizer(b: Briefing):
    return "hold", b.cio_exposure, [], (
        "The process produced this book. Overriding it on a narrative is how backtests stop predicting anything. "
        "I vote the model's number.")


def _rule_preserver(b: Briefing):
    vol_penalty = {"low": 0.0, "normal": 0.1, "elevated": 0.3, "extreme": 0.5, "unknown": 0.15}[
        b.market.vol_bucket if b.market else "unknown"]
    exposure = max(0.1, min(1.0, 0.8 - vol_penalty - 3.0 * max(0.0, -b.drawdown)))
    trim = [t for t, c in b.convictions.items() if c < -0.3][:2]     # shorts are the asymmetric risk
    stance = "cut risk" if exposure < b.cio_exposure - 0.05 else "hold"
    return stance, exposure, trim, (
        f"Volatility is {b.market.vol_bucket if b.market else 'unknown'} and we are {b.drawdown:+.1%} off the highs. "
        "Capital preserved is capital that can compound later.")


SEATS: dict[str, Seat] = {
    s.name: s for s in [
        Seat("macro", "The Macro Allocator", """You are THE MACRO ALLOCATOR on a hedge fund's investment board.
You size the whole book to the market regime and very little else. In a
confirmed risk-on regime you want the book fully deployed and you are
impatient with caution; in risk-off you cut hard and early, before the
drawdown forces you to. You distrust bottom-up stories that ignore the tape.
You rarely trim individual names — that is a stock-picker's fiddling.""", _rule_macro),

        Seat("risk_budgeter", "The Risk Budgeter", """You are THE RISK BUDGETER on a hedge fund's investment board, trained in a
multi-strategy shop. You think in risk budgets and drawdown limits, not
opinions. Analyst disagreement is a signal to size down. You are suspicious of
any single position large enough to matter on its own, and you will trim the
biggest ones to keep the book balanced. Your job is to still be here next
year.""", _rule_risk_budgeter),

        Seat("concentrator", "The Concentrator", """You are THE CONCENTRATOR on a hedge fund's investment board. You believe
returns come from a handful of high-conviction positions held through
discomfort. Over-diversification is closet indexing with a fee. You push back
on trimming winners and on cutting exposure because of volatility — volatility
is the price of the returns. You cut only when the thesis itself breaks or the
drawdown threatens the fund's survival.""", _rule_concentrator),

        Seat("systematizer", "The Systematizer", """You are THE SYSTEMATIZER on a hedge fund's investment board, from a
quantitative shop. You defend the process against human override. The book in
front of you was produced by a tested procedure; discretionary adjustments on
top of it are unmeasured risk. You vote to keep exposure where the process put
it unless you can name a specific, mechanical reason the process is broken
here. You distrust narrative and rhetoric, including your colleagues'.""", _rule_systematizer),

        Seat("preserver", "The Capital Preserver", """You are THE CAPITAL PRESERVER on a hedge fund's investment board. Your first
question is never "how much can we make" but "what is the worst case, and do
we survive it". Cash is a position. You cut exposure when volatility is
elevated or the fund is in drawdown, and you are especially wary of short
positions, whose losses are unbounded. You would rather miss a rally than take
a loss the fund cannot recover from.""", _rule_preserver),
    ]
}

_SCHEMA = """Respond with JSON only, exactly this schema:
{"stance": "add risk" | "hold" | "cut risk",
 "exposure_vote": <0-100, the percentage of the book you would deploy>,
 "trim": ["TICKER", ...],
 "concern": "<your argument to the board in 1-2 sentences, in your own voice>"}"""

_RULES = """Hard rules:
- You are voting on PORTFOLIO RISK, not picking stocks. Do not propose new positions.
- `trim` may only contain tickers from the proposed book shown to you.
- Reason only from the briefing. Do not invent numbers.
- Disagree with the CIO when your philosophy says so. A board that always agrees is useless."""


def _vote_with_llm(seat: Seat, briefing: Briefing, model: str | None, cache: PromptCache,
                   as_of: str, timeout: float) -> tuple[BoardVote | None, str | None]:
    system = f"{seat.persona}\n\n{_RULES}\n\n{_SCHEMA}"
    user = briefing.render()
    try:
        client = make_llm(model, timeout=timeout)
    except Exception as exc:
        # Any failure to reach a seat's model — missing key, bad slug, gateway
        # down — costs that seat its LLM vote, never the cycle.
        logger.warning("board seat %s has no client: %s", seat.name, exc)
        return None, f"no client: {exc}"
    key = prompt_key(f"board:{seat.name}", client.model, system, user)
    cached = cache.get(key)
    parsed = cached.get("parsed") if cached else None
    if parsed is None:
        try:
            response = client.complete(system, user)
            parsed = extract_json(response)
            cache.put(key, {"role": f"board:{seat.name}", "model": client.model, "as_of": as_of,
                            "system": system, "user": user, "response": response, "parsed": parsed})
        except Exception as exc:
            logger.warning("board seat %s failed (%s); using its rule", seat.name, exc)
            return None, str(exc)[:200]
    try:
        stance = str(parsed.get("stance", "")).lower()
        if stance not in ("add risk", "hold", "cut risk"):
            raise ValueError(f"bad stance {parsed.get('stance')!r}")
        exposure = float(parsed.get("exposure_vote", 0)) / 100.0
        if not 0.0 <= exposure <= 1.0:
            raise ValueError("exposure out of range")
        trim = [str(t).upper() for t in (parsed.get("trim") or []) if str(t).upper() in briefing.convictions][:5]
    except (TypeError, ValueError) as exc:
        logger.warning("board seat %s returned unusable JSON (%s); using its rule", seat.name, exc)
        return None, f"unusable answer: {exc}"
    return BoardVote(member=seat.name, display=seat.display, stance=stance, exposure_vote=exposure,
                     trim=trim, concern=str(parsed.get("concern", ""))[:400], model=client.model,
                     source="llm"), None


def convene_board(members: list, briefing: Briefing, cache: PromptCache, as_of: str,
                  use_llm: bool, default_model: str | None, workers: int = 5,
                  timeout: float = 150.0, on_vote=None) -> BoardResolution:
    """Poll every seat (in parallel — each may be a different model), then
    aggregate the votes into one resolution."""

    def cast(spec) -> BoardVote:
        seat = SEATS[spec.name]
        reason = None
        if use_llm:
            vote, reason = _vote_with_llm(seat, briefing, spec.model or default_model, cache, as_of, timeout)
            if vote is not None:
                return vote
        stance, exposure, trim, concern = seat.rule(briefing)
        return BoardVote(member=seat.name, display=seat.display, stance=stance,
                         exposure_vote=round(max(0.0, min(1.0, exposure)), 4),
                         trim=[t for t in trim if t in briefing.convictions], concern=concern,
                         source="rules", fallback_reason=reason,
                         model=(spec.model or default_model) if reason else None)

    order = {m.name: i for i, m in enumerate(members)}
    if use_llm and len(members) > 1:
        votes = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(members)))) as pool:
            futures = [pool.submit(cast, m) for m in members]
            for future in as_completed(futures):
                vote = future.result()
                votes.append(vote)
                if on_vote is not None:
                    on_vote(vote)          # fires in COMPLETION order — that is the live moment
        votes.sort(key=lambda v: order.get(v.member, 99))   # record keeps seat order
    else:
        votes = []
        for m in members:
            vote = cast(m)
            votes.append(vote)
            if on_vote is not None:
                on_vote(vote)

    return resolve(votes, briefing.cio_exposure)


def resolve(votes: list[BoardVote], cio_exposure: float) -> BoardResolution:
    """Median vote, clamped near the CIO's proposal; a name is trimmed only on
    a majority. The median is deliberate: one extreme seat cannot run the fund."""
    if not votes:
        return BoardResolution(exposure=cio_exposure, stance="hold", cio_proposal=cio_exposure,
                               summary="No board sat this cycle.")
    raw = median(v.exposure_vote for v in votes)
    exposure = max(0.0, min(1.0, max(cio_exposure - _MAX_MOVE, min(cio_exposure + _MAX_MOVE, raw))))

    needed = math.ceil(len(votes) / 2)
    flags: dict[str, int] = {}
    for v in votes:
        for t in v.trim:
            flags[t] = flags.get(t, 0) + 1
    trimmed = {t: n for t, n in flags.items() if n >= needed}

    for v in votes:
        v.dissent = abs(v.exposure_vote - exposure) > _DISSENT_GAP
    stance = ("add risk" if exposure > cio_exposure + 0.02
              else "cut risk" if exposure < cio_exposure - 0.02 else "hold")
    dissenters = [v.display for v in votes if v.dissent]
    counts: dict[str, int] = {}
    for v in votes:
        counts[v.stance] = counts.get(v.stance, 0) + 1
    split = ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    summary = (f"Board resolves to deploy {exposure:.0%} of the book "
               f"(CIO proposed {cio_exposure:.0%}) — {split}."
               + (f" Trimmed on a majority: {', '.join(sorted(trimmed))}." if trimmed else "")
               + (f" Dissenting: {', '.join(dissenters)}." if dissenters else " Vote was aligned."))
    return BoardResolution(exposure=round(exposure, 4), stance=stance, votes=votes, trimmed=trimmed,
                           trim_factor=_TRIM_FACTOR, cio_proposal=round(cio_exposure, 4),
                           unanimous=not dissenters, summary=summary)


def apply_resolution(convictions: dict[str, float], resolution: BoardResolution) -> dict[str, float]:
    """Majority-trimmed names keep half their conviction. Signs never change."""
    return {t: (c * resolution.trim_factor if t in resolution.trimmed else c) for t, c in convictions.items()}
