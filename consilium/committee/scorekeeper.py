"""Scorekeeper — who has actually been right. Deterministic arithmetic over records.

Every view the committee hears is a call: a sign, a size, and a horizon. Once
that horizon has run, the call is scored against the marks the fund itself
recorded — the first meeting on or after the due date, entry at the meeting
that heard it. Nothing is scored early and nothing is read from outside the
records, so the scorecard going into a meeting uses only what that meeting
could have known.

    hit             sign(conviction) matches the sign of the realised return
    contribution    conviction × realised return (being loudly right counts more)

Reliability turns the record into a blend multiplier, deliberately timid:

    p̂      = (hits + PRIOR_HIT · PRIOR_OBS) / (n + PRIOR_OBS)
    factor = clip(p̂ / PRIOR_HIT, FACTOR_MIN, FACTOR_MAX)

With no history the factor is exactly 1. Twenty calls at 70% move it to 1.2,
not 1.4: the prior is worth PRIOR_OBS calls of coin-flipping, so a hot streak
cannot buy an analyst the room. And whatever the record, an analyst can lose
half its voice or gain half, never more.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta

from consilium.core.models import AnalystScore, CycleRecord

PRIOR_OBS = 20          # the prior is worth this many calls...
PRIOR_HIT = 0.5         # ...at a coin-flip hit rate
FACTOR_MIN, FACTOR_MAX = 0.5, 1.5
MIN_DIRECTION = 0.1     # below this a view has no direction to score (View.stance calls it neutral)
CALENDAR_PER_TRADING = 365 / 252   # horizons are trading days; meetings are dated by the calendar
RECENT = 10             # calls in the reflection summary
THESIS_CHARS = 200


@dataclass(order=True)
class Call:
    due: str
    seq: int                         # tie-break: insertion order, so the heap is deterministic
    analyst: str = ""
    ticker: str = ""
    date: str = ""
    conviction: float = 0.0
    horizon_days: int = 0
    thesis: str = ""
    entry: float = 0.0
    outcome: float | None = None     # realised return over the horizon, once due


def _due(d: str, horizon_days: int) -> str:
    return (_date.fromisoformat(d) + timedelta(days=math.ceil(horizon_days * CALENDAR_PER_TRADING))).isoformat()


def reliability_factor(hits: int, n: int) -> float:
    p = (hits + PRIOR_HIT * PRIOR_OBS) / (n + PRIOR_OBS)
    return min(FACTOR_MAX, max(FACTOR_MIN, p / PRIOR_HIT))


class Scorekeeper:
    """Feed it meetings in date order: `resolve` with each meeting's marks
    before it deliberates, `add` the record once it has."""

    def __init__(self) -> None:
        self._pending: list[Call] = []
        self._seq = 0
        self._n: dict[str, int] = {}
        self._hits: dict[str, int] = {}
        self._contrib: dict[str, float] = {}
        self._recent: dict[str, deque[Call]] = {}
        self._waiting: dict[str, int] = {}

    @classmethod
    def from_records(cls, records: list[CycleRecord]) -> "Scorekeeper":
        k = cls()
        for rec in sorted(records, key=lambda r: r.as_of):
            k.resolve(rec.as_of, rec.marks)
            k.add(rec)
        return k

    def resolve(self, as_of: str, marks: dict[str, float]) -> None:
        """Score every call due by `as_of` whose name has a mark today. A name
        with no mark (it left the universe) waits; it is never guessed."""
        unpriced = []
        while self._pending and self._pending[0].due <= as_of:
            c = heapq.heappop(self._pending)
            exit_ = marks.get(c.ticker)
            if exit_ is None:
                unpriced.append(c)
                continue
            c.outcome = exit_ / c.entry - 1
            a = c.analyst
            self._waiting[a] -= 1
            self._n[a] = self._n.get(a, 0) + 1
            self._hits[a] = self._hits.get(a, 0) + (1 if c.conviction * c.outcome > 0 else 0)
            self._contrib[a] = self._contrib.get(a, 0.0) + c.conviction * c.outcome
            self._recent.setdefault(a, deque(maxlen=RECENT)).append(c)
        for c in unpriced:
            heapq.heappush(self._pending, c)

    def add(self, rec: CycleRecord) -> None:
        seen: set[tuple[str, str]] = set()   # an analyst sitting in two pods makes one call, not two
        for s in rec.strategies:
            for v in s.views:
                key = (v.analyst, v.ticker)
                if v.abstained or abs(v.conviction) < MIN_DIRECTION or key in seen or not rec.marks.get(v.ticker):
                    continue
                seen.add(key)
                self._seq += 1
                heapq.heappush(self._pending, Call(due=_due(rec.as_of, v.horizon_days), seq=self._seq,
                                                   analyst=v.analyst, ticker=v.ticker, date=rec.as_of,
                                                   conviction=v.conviction, horizon_days=v.horizon_days,
                                                   thesis=v.thesis, entry=rec.marks[v.ticker]))
                self._waiting[v.analyst] = self._waiting.get(v.analyst, 0) + 1

    # reading ---------------------------------------------------------------
    def scores(self, analysts: list[str]) -> dict[str, AnalystScore]:
        out = {}
        for a in analysts:
            n, h = self._n.get(a, 0), self._hits.get(a, 0)
            out[a] = AnalystScore(analyst=a, n=n, hits=h, hit_rate=(h / n if n else None),
                                  avg_contribution=(self._contrib[a] / n if n else None),
                                  pending=self._waiting.get(a, 0), reliability=reliability_factor(h, n))
        return out

    def factors(self, analysts: list[str]) -> dict[str, float]:
        return {a: reliability_factor(self._hits.get(a, 0), self._n.get(a, 0)) for a in analysts}

    def summary(self, analyst: str) -> str | None:
        """What an analyst is told about itself. Deterministic, rounded so it
        only changes when a new call resolves; None when nothing has."""
        recent = list(self._recent.get(analyst, ()))
        if not recent:
            return None
        hits = sum(1 for c in recent if c.conviction * c.outcome > 0)
        text = (f"Your track record on this fund (calls whose horizon has run): of your last {len(recent)} "
                f"call{'s' if len(recent) != 1 else ''}, {hits} {'was' if hits == 1 else 'were'} right.")
        misses = [c for c in recent if c.conviction * c.outcome <= 0]
        if misses:
            worst = min(misses, key=lambda c: (c.conviction * c.outcome, c.seq))
            stance = "bullish" if worst.conviction > 0 else "bearish"
            thesis = worst.thesis if len(worst.thesis) <= THESIS_CHARS else worst.thesis[:THESIS_CHARS - 1] + "…"
            text += (f" Your worst recent miss: {worst.ticker} on {worst.date}, {stance} {worst.conviction:+.2f}; "
                     f"thesis was \"{thesis}\"; outcome {worst.outcome:+.1%} over {worst.horizon_days} trading days.")
        return text
