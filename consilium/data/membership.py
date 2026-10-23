"""Membership — which names were actually in the index on a given date.

A universe typed in today is a list of survivors: the names still standing.
Backtesting it over past dates quietly drops everything that was delisted,
acquired or demoted along the way, and those are disproportionately the
losers. That is survivorship bias, and it flatters every result it touches.

A membership file fixes the mechanism. It is a CSV of spells:

    ticker,start_date,end_date
    AAPL,1982-11-30,
    XYZ,2010-03-01,2019-06-21

A blank end_date means still a member. A ticker may have several spells (left
and rejoined). On a date a name is a member if some spell has
start_date <= date <= end_date. Bad rows raise: this is data, and data fails loud.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

SURVIVORSHIP_WARNING = (
    "The universe is a fixed list of tickers applied to every past date. Names that were delisted, acquired or "
    "dropped from the index during the backtest are missing, and they are disproportionately the losers, so "
    "results are flattered (survivorship bias). Supply a membership file (ticker,start_date,end_date) to fix it.")


@dataclass(frozen=True)
class Spell:
    start: str
    end: str | None


class Membership:
    def __init__(self, spells: dict[str, list[Spell]], source: str = "") -> None:
        self._spells = spells
        self.source = source

    @classmethod
    def from_csv(cls, path: str | Path) -> "Membership":
        path = Path(path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"membership file not found: {path}")
        spells: dict[str, list[Spell]] = {}
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            need = {"ticker", "start_date", "end_date"}
            if not reader.fieldnames or not need <= {h.strip() for h in reader.fieldnames}:
                raise ValueError(f"{path}: header must be ticker,start_date,end_date (got {reader.fieldnames})")
            for n, row in enumerate(reader, start=2):
                row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                t = row["ticker"].upper()
                if not t:
                    raise ValueError(f"{path}:{n}: empty ticker")
                start, end = _iso(row["start_date"], path, n), (_iso(row["end_date"], path, n) if row["end_date"] else None)
                if end is not None and end < start:
                    raise ValueError(f"{path}:{n}: {t} end_date {end} is before start_date {start}")
                spells.setdefault(t, []).append(Spell(start, end))
        if not spells:
            raise ValueError(f"{path}: no membership rows")
        return cls(spells, source=str(path))

    @property
    def tickers(self) -> list[str]:
        return sorted(self._spells)

    def is_member(self, ticker: str, as_of: str) -> bool:
        return any(s.start <= as_of and (s.end is None or as_of <= s.end) for s in self._spells.get(ticker.upper(), ()))

    def members(self, tickers: list[str], as_of: str) -> list[str]:
        return [t for t in tickers if self.is_member(t, as_of)]


def _iso(v: str, path: Path, n: int) -> str:
    try:
        return _date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError(f"{path}:{n}: {v!r} is not a YYYY-MM-DD date") from None
