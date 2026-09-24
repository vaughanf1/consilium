"""Analyst — anything that forms a View on a ticker at a date.

    Analyst (ABC)
      ├─ QuantAnalyst  — pure math over the PriceBook / fundamentals
      └─ LLMAnalyst    — a persona reasoning over rendered snapshots

Analysts form VIEWS. They never size positions, never see the book, never
touch an order. `AnalysisContext` is the only thing they may read from, and
every accessor on it is point-in-time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from consilium.core.models import Fundamentals, Profile, View
from consilium.data.features import MarketSnapshot, PriceBook, market_snapshot


@dataclass
class AnalysisContext:
    book: PriceBook
    date: str
    benchmark: str
    _market: dict[str, MarketSnapshot | None] = field(default_factory=dict)
    _fund: dict[str, Fundamentals | None] = field(default_factory=dict)

    def market(self, ticker: str) -> MarketSnapshot | None:
        if ticker not in self._market:
            self._market[ticker] = market_snapshot(self.book, ticker, self.date)
        return self._market[ticker]

    def fundamentals(self, ticker: str) -> Fundamentals | None:
        if ticker not in self._fund:
            self._fund[ticker] = self.book.provider.fundamentals(ticker, self.date)
        return self._fund[ticker]

    def profile(self, ticker: str) -> Profile | None:
        return self.book.profile(ticker)

    def benchmark_market(self) -> MarketSnapshot | None:
        return self.market(self.benchmark)


class Analyst(ABC):
    name: str = "analyst"
    display: str = "Analyst"
    kind: str = "quant"           # "quant" | "llm"
    horizon_days: int = 60
    needs_fundamentals: bool = False

    @abstractmethod
    def view(self, ticker: str, ctx: AnalysisContext) -> View: ...

    # helpers ---------------------------------------------------------
    def _abstain(self, ticker: str, date: str, reason: str) -> View:
        return View(analyst=self.name, ticker=ticker, date=date, conviction=0.0, confidence=0.0,
                    horizon_days=self.horizon_days, thesis=f"abstained: {reason}", abstained=True,
                    metadata={"abstain_reason": reason})

    def _view(self, ticker: str, date: str, conviction: float, confidence: float, thesis: str,
              risks: list[str] | None = None, components: dict | None = None, **meta) -> View:
        return View(analyst=self.name, ticker=ticker, date=date,
                    conviction=max(-1.0, min(1.0, conviction)), confidence=max(0.0, min(1.0, confidence)),
                    horizon_days=self.horizon_days, thesis=thesis, risks=risks or [],
                    components=components or {}, metadata=meta)
