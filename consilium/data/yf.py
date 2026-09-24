"""YFinanceProvider — free market data, no key required.

Prices are historical and safe to backtest on. Fundamentals from yfinance are
LATEST-ONLY (no filing-date history), so `point_in_time=False`: an LLM analyst
backtested on them sees today's ratios on yesterday's dates. Price-only quant
models are unaffected; the backtester prints a warning when a mandate mixes
fundamental analysts with this provider. Plug a point-in-time provider into
the same protocol for research-grade fundamentals.
"""

from __future__ import annotations

import logging

import pandas as pd

from consilium.core.models import Bar, Fundamentals, Profile

logger = logging.getLogger(__name__)


def _f(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return None


class YFinanceProvider:
    name = "yfinance"
    point_in_time = False

    def __init__(self) -> None:
        import yfinance as yf  # imported lazily: the synthetic path never pays for it
        self._yf = yf
        self._info: dict[str, dict] = {}

    def prices(self, ticker: str, start: str, end: str) -> list[Bar]:
        end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        df = self._yf.download(ticker, start=start, end=end_excl, progress=False,
                               auto_adjust=True, threads=False)
        if df is None or df.empty:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        out = []
        for ts, row in df.iterrows():
            d = ts.strftime("%Y-%m-%d")
            if d > end:
                continue
            c = _f(row.get("Close"))
            if c is None:
                continue
            out.append(Bar(date=d, open=_f(row.get("Open")) or c, high=_f(row.get("High")) or c,
                           low=_f(row.get("Low")) or c, close=c, volume=_f(row.get("Volume")) or 0.0))
        return out

    def _get_info(self, ticker: str) -> dict:
        if ticker not in self._info:
            try:
                self._info[ticker] = self._yf.Ticker(ticker).info or {}
            except Exception as exc:  # yfinance raises a zoo of exceptions
                raise RuntimeError(f"yfinance info failed for {ticker}: {exc}") from exc
        return self._info[ticker]

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals | None:
        i = self._get_info(ticker)
        if not i or i.get("quoteType") == "ETF":
            return None
        return Fundamentals(
            ticker=ticker, as_of=as_of, point_in_time=False,
            market_cap=_f(i.get("marketCap")), pe=_f(i.get("trailingPE")), pb=_f(i.get("priceToBook")),
            ps=_f(i.get("priceToSalesTrailing12Months")), ev_ebitda=_f(i.get("enterpriseToEbitda")),
            roe=_f(i.get("returnOnEquity")), roic=None,
            gross_margin=_f(i.get("grossMargins")), operating_margin=_f(i.get("operatingMargins")),
            net_margin=_f(i.get("profitMargins")), revenue_growth=_f(i.get("revenueGrowth")),
            earnings_growth=_f(i.get("earningsGrowth")),
            debt_to_equity=(_f(i.get("debtToEquity")) or 0) / 100 if i.get("debtToEquity") is not None else None,
            current_ratio=_f(i.get("currentRatio")),
            fcf_yield=(_f(i.get("freeCashflow")) / _f(i.get("marketCap"))) if _f(i.get("freeCashflow")) and _f(i.get("marketCap")) else None,
            dividend_yield=_f(i.get("dividendYield")), beta=_f(i.get("beta")),
        )

    def profile(self, ticker: str) -> Profile | None:
        i = self._get_info(ticker)
        return Profile(ticker=ticker, name=i.get("shortName") or i.get("longName"),
                       sector=i.get("sector"), industry=i.get("industry"))
