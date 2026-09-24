"""DataProvider — the interface every data source implements (structural typing).

Contract: an empty list / None means the data genuinely does not exist.
Infrastructure failures (network, auth, rate limits) must RAISE. A provider that
silently returns empty on failure poisons backtests.

`prices` must return only bars with date <= end. `fundamentals(ticker, as_of)`
should be point-in-time when the source allows; when it cannot (yfinance), it
must set `Fundamentals.point_in_time=False` so the engine can warn honestly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from consilium.core.models import Bar, Fundamentals, Profile


@runtime_checkable
class DataProvider(Protocol):
    name: str
    point_in_time: bool

    def prices(self, ticker: str, start: str, end: str) -> list[Bar]: ...

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals | None: ...

    def profile(self, ticker: str) -> Profile | None: ...
