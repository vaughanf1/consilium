"""Features — what analysts are allowed to know, computed once per (ticker, date).

Two snapshots:
- MarketSnapshot: price-derived (returns, vol, trend, drawdown, RSI) plus coarse
  regime BUCKETS. LLM analysts see the buckets, not the raw numbers, so their
  prompt (and therefore the prompt cache key) only changes when the regime
  actually changes — not every day a price ticks.
- Fundamentals (core.models) comes straight from the provider.

PriceBook holds the whole history for a run so every model reads from memory.
All lookups are strictly point-in-time: `closes_until(date)` never returns a
bar after `date`.
"""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_right
from dataclasses import dataclass, field

import numpy as np

from consilium.core.models import Bar, Fundamentals, Profile
from consilium.data.protocol import DataProvider

TRADING_DAYS = 252


class PriceBook:
    """Per-run in-memory price store with point-in-time slicing."""

    def __init__(self, provider: DataProvider, start: str, end: str, lookback_days: int = 400) -> None:
        from datetime import date as _d, timedelta
        self.provider = provider
        self.start, self.end = start, end
        self.load_from = (_d.fromisoformat(start) - timedelta(days=lookback_days)).isoformat()
        self._bars: dict[str, list[Bar]] = {}
        self._dates: dict[str, list[str]] = {}
        self._closes: dict[str, np.ndarray] = {}
        self._profiles: dict[str, Profile | None] = {}

    def load(self, ticker: str) -> None:
        if ticker in self._bars:
            return
        bars = sorted(self.provider.prices(ticker, self.load_from, self.end), key=lambda b: b.date)
        self._bars[ticker] = bars
        self._dates[ticker] = [b.date for b in bars]
        self._closes[ticker] = np.array([b.close for b in bars], dtype=float)

    def has(self, ticker: str) -> bool:
        self.load(ticker)
        return bool(self._bars[ticker])

    def trading_days(self, ticker: str) -> list[str]:
        self.load(ticker)
        return [d for d in self._dates[ticker] if self.start <= d <= self.end]

    def closes_until(self, ticker: str, date: str, n: int | None = None) -> np.ndarray:
        self.load(ticker)
        i = bisect_right(self._dates[ticker], date)
        arr = self._closes[ticker][:i]
        return arr if n is None else arr[-n:]

    def bars_until(self, ticker: str, date: str, n: int) -> list[Bar]:
        self.load(ticker)
        i = bisect_right(self._dates[ticker], date)
        return self._bars[ticker][max(0, i - n):i]

    def mark(self, ticker: str, date: str, max_stale_days: int = 7) -> float | None:
        """Last close on or before date, if it is fresh enough."""
        from datetime import date as _d
        self.load(ticker)
        i = bisect_right(self._dates[ticker], date)
        if i == 0:
            return None
        last = self._dates[ticker][i - 1]
        if (_d.fromisoformat(date) - _d.fromisoformat(last)).days > max_stale_days:
            return None
        return float(self._closes[ticker][i - 1])

    def profile(self, ticker: str) -> Profile | None:
        if ticker not in self._profiles:
            try:
                self._profiles[ticker] = self.provider.profile(ticker)
            except Exception:
                self._profiles[ticker] = None
        return self._profiles[ticker]

    def sector(self, ticker: str) -> str:
        p = self.profile(ticker)
        return (p.sector if p and p.sector else "Unknown")


# ---------------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    ticker: str
    date: str
    close: float
    ret_1m: float | None
    ret_3m: float | None
    ret_6m: float | None
    ret_12m: float | None
    mom_12_1: float | None          # 12-month return skipping the last month
    vol_20: float | None            # annualized
    vol_60: float | None
    vol_252: float | None
    drawdown: float | None          # from 252-day high
    ma50_rel: float | None          # close / 50dma - 1
    ma200_rel: float | None
    rsi_14: float | None
    zscore_5: float | None          # 5-day return / 60-day daily vol
    n_bars: int = 0

    # Coarse buckets: the only market facts an LLM analyst is shown, so the
    # prompt is stable between regime changes.
    @property
    def trend_bucket(self) -> str:
        if self.ma200_rel is None:
            return "unknown"
        if self.ma200_rel > 0.08:
            return "strong uptrend"
        if self.ma200_rel > 0.0:
            return "uptrend"
        if self.ma200_rel > -0.08:
            return "downtrend"
        return "strong downtrend"

    @property
    def vol_bucket(self) -> str:
        if self.vol_60 is None:
            return "unknown"
        if self.vol_60 < 0.18:
            return "low"
        if self.vol_60 < 0.32:
            return "normal"
        if self.vol_60 < 0.50:
            return "elevated"
        return "extreme"

    @property
    def drawdown_bucket(self) -> str:
        if self.drawdown is None:
            return "unknown"
        if self.drawdown > -0.05:
            return "at highs"
        if self.drawdown > -0.15:
            return "modest pullback"
        if self.drawdown > -0.30:
            return "correction"
        return "deep drawdown"

    @property
    def momentum_bucket(self) -> str:
        if self.mom_12_1 is None:
            return "unknown"
        if self.mom_12_1 > 0.25:
            return "strong positive"
        if self.mom_12_1 > 0.0:
            return "positive"
        if self.mom_12_1 > -0.20:
            return "negative"
        return "strongly negative"

    def render_coarse(self) -> str:
        return (f"Price regime: {self.trend_bucket} vs 200-day average; 12-1 momentum {self.momentum_bucket}; "
                f"realized volatility {self.vol_bucket}; {self.drawdown_bucket} relative to 52-week high.")

    def coarse_hash(self) -> str:
        return hashlib.sha1(self.render_coarse().encode()).hexdigest()[:10]

    def as_dict(self) -> dict:
        return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _ret(closes: np.ndarray, days: int) -> float | None:
    if len(closes) <= days:
        return None
    return float(closes[-1] / closes[-1 - days] - 1)


def _vol(closes: np.ndarray, days: int) -> float | None:
    if len(closes) <= days:
        return None
    r = np.diff(np.log(closes[-days - 1:]))
    return float(r.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) > 2 else None


def _rsi(closes: np.ndarray, period: int = 14) -> float | None:
    if len(closes) <= period + 1:
        return None
    d = np.diff(closes[-period - 1:])
    gain, loss = d[d > 0].sum() / period, -d[d < 0].sum() / period
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100 - 100 / (1 + rs))


def market_snapshot(book: PriceBook, ticker: str, date: str) -> MarketSnapshot | None:
    closes = book.closes_until(ticker, date, 300)
    if len(closes) < 22:
        return None
    c = float(closes[-1])
    mom = None
    if len(closes) > 252:
        mom = float(closes[-22] / closes[-253] - 1)
    ma50 = float(closes[-50:].mean()) if len(closes) >= 50 else None
    ma200 = float(closes[-200:].mean()) if len(closes) >= 200 else None
    hi = float(closes[-252:].max())
    z5 = None
    if len(closes) > 61:
        daily = np.diff(np.log(closes[-61:]))
        sd = daily.std(ddof=1)
        if sd > 0:
            z5 = float((np.log(closes[-1] / closes[-6])) / (sd * math.sqrt(5)))
    return MarketSnapshot(
        ticker=ticker, date=date, close=c,
        ret_1m=_ret(closes, 21), ret_3m=_ret(closes, 63), ret_6m=_ret(closes, 126), ret_12m=_ret(closes, 252),
        mom_12_1=mom, vol_20=_vol(closes, 20), vol_60=_vol(closes, 60), vol_252=_vol(closes, 252),
        drawdown=c / hi - 1 if hi > 0 else None,
        ma50_rel=(c / ma50 - 1) if ma50 else None, ma200_rel=(c / ma200 - 1) if ma200 else None,
        rsi_14=_rsi(closes), zscore_5=z5, n_bars=len(closes),
    )


def render_fundamentals(f: Fundamentals, profile: Profile | None) -> str:
    def pct(v):
        return "-" if v is None else f"{v * 100:.1f}%"

    def x(v):
        return "-" if v is None else f"{v:.1f}x"

    def cap(v):
        if v is None:
            return "-"
        return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"

    head = f"Company: {profile.name if profile and profile.name else f.ticker} ({f.ticker})"
    if profile and profile.sector:
        head += f" | Sector: {profile.sector}" + (f" / {profile.industry}" if profile.industry else "")
    lines = [
        head,
        f"Market cap {cap(f.market_cap)} | P/E {x(f.pe)} | P/B {x(f.pb)} | P/S {x(f.ps)} | EV/EBITDA {x(f.ev_ebitda)}",
        f"ROE {pct(f.roe)} | ROIC {pct(f.roic)} | Gross margin {pct(f.gross_margin)} | Op margin {pct(f.operating_margin)} | Net margin {pct(f.net_margin)}",
        f"Revenue growth {pct(f.revenue_growth)} | Earnings growth {pct(f.earnings_growth)} | FCF yield {pct(f.fcf_yield)} | Dividend yield {pct(f.dividend_yield)}",
        f"Debt/Equity {x(f.debt_to_equity)} | Current ratio {x(f.current_ratio)}",
    ]
    if not f.point_in_time:
        lines.append("(Note: these ratios are the latest available, not as-of the analysis date.)")
    return "\n".join(lines)


def fundamentals_hash(f: Fundamentals | None) -> str:
    if f is None:
        return "none"
    payload = f.model_dump_json(exclude={"as_of"})
    return hashlib.sha1(payload.encode()).hexdigest()[:12]
