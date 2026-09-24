"""SyntheticProvider — a deterministic, offline market.

Every ticker gets a seeded geometric Brownian motion with a sector factor, a
slow drift in its "quality" (which drives fundamentals), and an occasional
regime shock so backtests have drawdowns to survive. Same ticker + same dates
-> byte-identical data, every time. This is what `consilium demo` and the
test-suite run on: no keys, no network, no flakiness.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date as _date
from datetime import timedelta

import numpy as np

from consilium.core.models import Bar, Fundamentals, Profile

_SECTORS = ["Technology", "Healthcare", "Financials", "Energy", "Consumer", "Industrials", "Utilities"]
_KNOWN = {
    "AAPL": ("Apple", "Technology"), "MSFT": ("Microsoft", "Technology"), "NVDA": ("NVIDIA", "Technology"),
    "GOOGL": ("Alphabet", "Technology"), "AMZN": ("Amazon", "Consumer"), "META": ("Meta Platforms", "Technology"),
    "TSLA": ("Tesla", "Consumer"), "JPM": ("JPMorgan Chase", "Financials"), "UNH": ("UnitedHealth", "Healthcare"),
    "XOM": ("Exxon Mobil", "Energy"), "JNJ": ("Johnson & Johnson", "Healthcare"), "PG": ("Procter & Gamble", "Consumer"),
    "V": ("Visa", "Financials"), "CAT": ("Caterpillar", "Industrials"), "NEE": ("NextEra Energy", "Utilities"),
    "SPY": ("S&P 500 ETF", "Index"), "QQQ": ("Nasdaq 100 ETF", "Index"),
}
_EPOCH = _date(2015, 1, 1)


def _seed(ticker: str, salt: str = "") -> int:
    return int(hashlib.sha256(f"{ticker}|{salt}".encode()).hexdigest()[:8], 16)


class SyntheticProvider:
    name = "synthetic"
    point_in_time = True  # the synthetic world has no filing lag by construction

    def __init__(self) -> None:
        self._paths: dict[str, tuple[list[_date], np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}

    # ------------------------------------------------------------------
    def _path(self, ticker: str):
        if ticker in self._paths:
            return self._paths[ticker]
        rng = np.random.default_rng(_seed(ticker))
        n_days = (_date(2027, 12, 31) - _EPOCH).days
        days = [_EPOCH + timedelta(days=i) for i in range(n_days)]
        days = [d for d in days if d.weekday() < 5]
        n = len(days)

        sector = self.profile(ticker).sector or "Technology"
        srng = np.random.default_rng(_seed(sector, "sector"))
        mrng = np.random.default_rng(1164)  # seed chosen for a realistic market profile: mostly up, one bear year, 10-20% corrections
        market = mrng.normal(0.0004, 0.0095, n)
        # regime shocks: a few multi-week drawdowns baked into the market factor
        for start in mrng.integers(0, n - 60, 4):
            market[start:start + 30] -= 0.0035
        sector_f = srng.normal(0.0001, 0.006, n)

        is_index = ticker in ("SPY", "QQQ")
        beta = 1.0 if is_index else float(np.clip(rng.normal(1.0, 0.35), 0.3, 2.0))
        idio_vol = 0.002 if is_index else float(np.clip(rng.normal(0.016, 0.006), 0.006, 0.035))
        alpha = 0.0 if is_index else float(rng.normal(0.0, 0.00025))
        idio = rng.normal(alpha, idio_vol, n)
        rets = beta * market + (0.0 if is_index else 0.6 * sector_f) + idio
        closes = np.exp(np.cumsum(rets))
        # Anchor the level so prices sit in a realistic range: the index near
        # $450 and single names between $25 and $600 on the first day of 2024.
        anchor_idx = next(i for i, d in enumerate(days) if d >= _date(2024, 1, 1))
        level = 450.0 if is_index else float(np.exp(rng.uniform(np.log(25), np.log(600))))
        closes = closes * (level / closes[anchor_idx])

        # quality drifts slowly: an AR(1) in [−1, 1] driving the fundamentals
        q = np.zeros(n)
        q[0] = rng.uniform(-0.6, 0.6)
        for i in range(1, n):
            q[i] = 0.999 * q[i - 1] + rng.normal(0, 0.02)
        q = np.tanh(q)
        vol_series = np.abs(rets)
        self._paths[ticker] = (days, closes, rets, q, vol_series)
        return self._paths[ticker]

    # ------------------------------------------------------------------
    def prices(self, ticker: str, start: str, end: str) -> list[Bar]:
        days, closes, rets, _, _ = self._path(ticker)
        s, e = _date.fromisoformat(start), _date.fromisoformat(end)
        rng = np.random.default_rng(_seed(ticker, "ohlc"))
        out: list[Bar] = []
        for i, d in enumerate(days):
            if d < s or d > e:
                continue
            c = float(closes[i])
            o = float(closes[i - 1]) if i > 0 else c
            spread = abs(c - o) + c * 0.004
            out.append(Bar(
                date=d.isoformat(), open=round(o, 4),
                high=round(max(o, c) + spread * float(rng.uniform(0, 0.6)), 4),
                low=round(min(o, c) - spread * float(rng.uniform(0, 0.6)), 4),
                close=round(c, 4), volume=float(1_000_000 + 4_000_000 * rng.uniform()),
            ))
        return out

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals | None:
        if ticker in ("SPY", "QQQ"):
            return None
        days, closes, _, q, _ = self._path(ticker)
        d = _date.fromisoformat(as_of)
        idx = max(0, min(len(days) - 1, sum(1 for x in days if x <= d) - 1))
        # fundamentals only "update" quarterly — snap to the last quarter end
        quarter_idx = idx - (idx % 63)
        qq = float(q[quarter_idx])
        rng = np.random.default_rng(_seed(ticker, f"fund{quarter_idx}"))
        price = float(closes[idx])
        shares = 1e9 * float(np.random.default_rng(_seed(ticker, "sh")).uniform(0.2, 5))
        eps = max(0.05, price / (18 + 12 * (-qq)) ) * (1 + rng.normal(0, 0.05))
        roe = float(np.clip(0.14 + 0.18 * qq + rng.normal(0, 0.02), -0.2, 0.6))
        return Fundamentals(
            ticker=ticker, as_of=as_of, point_in_time=True,
            market_cap=price * shares,
            pe=float(np.clip(price / eps, 4, 120)),
            pb=float(np.clip(3 + 4 * qq + rng.normal(0, 0.3), 0.5, 25)),
            ps=float(np.clip(2.5 + 3 * qq, 0.3, 30)),
            ev_ebitda=float(np.clip(12 + 8 * (-qq) + rng.normal(0, 1), 3, 60)),
            roe=roe, roic=roe * 0.8,
            gross_margin=float(np.clip(0.42 + 0.2 * qq, 0.05, 0.9)),
            operating_margin=float(np.clip(0.18 + 0.15 * qq, -0.2, 0.6)),
            net_margin=float(np.clip(0.12 + 0.12 * qq, -0.25, 0.5)),
            revenue_growth=float(np.clip(0.08 + 0.2 * qq + rng.normal(0, 0.03), -0.4, 0.8)),
            earnings_growth=float(np.clip(0.10 + 0.3 * qq + rng.normal(0, 0.05), -0.8, 1.5)),
            debt_to_equity=float(np.clip(0.9 - 0.7 * qq + rng.normal(0, 0.1), 0.0, 5)),
            current_ratio=float(np.clip(1.5 + 0.8 * qq, 0.4, 5)),
            fcf_yield=float(np.clip(0.04 + 0.03 * qq, -0.05, 0.15)),
            dividend_yield=float(np.clip(0.012 + 0.01 * qq, 0, 0.08)),
            beta=None,
        )

    def profile(self, ticker: str) -> Profile | None:
        if ticker in _KNOWN:
            name, sector = _KNOWN[ticker]
        else:
            name, sector = f"{ticker} Corp", _SECTORS[_seed(ticker, "sec") % len(_SECTORS)]
        return Profile(ticker=ticker, name=name, sector=sector, industry=sector)
