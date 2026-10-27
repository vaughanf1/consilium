"""EdgarProvider — point-in-time fundamentals from SEC EDGAR, as reported.

yfinance knows a company's ratios today. EDGAR knows what the company filed and
WHEN: every XBRL fact in the companyfacts API carries the date its filing hit
the SEC. This provider indexes by that filing date, so on any date a backtest
sees exactly the numbers that had been published by then — a December year-end
filed in February is invisible in January, and a restatement only replaces the
original from the day it was filed.

    flows (revenue, earnings, cash flow)   latest FISCAL YEAR filed by as_of
    stocks (equity, debt, cash)            latest balance sheet filed by as_of
    ratios                                 combined with the close on as_of

Prices and profiles still come from the wrapped price provider (yfinance).
Annual flows are deliberately simple: a trailing-twelve-month stitch of 10-Qs
would be fresher but is easy to get subtly wrong, and wrong-but-confident is
worse than a quarter stale.

Units. yfinance closes are split-adjusted to today's share count; filed share
counts are as they stood. Market cap multiplies the filed count by every split
since it was reported, which puts both on one basis. That uses later split
dates only to convert units — the product is the market value on as_of.

Etiquette. The SEC requires a User-Agent naming who is asking
(CONSILIUM_SEC_USER_AGENT="Your Name you@example.com") and allows ten requests
a second; this client sends at most MAX_RPS. Responses are cached in SQLite.
A cached company is reused for any as_of up to the day it was fetched — it
already holds every filing made by then — and refetched only for later dates,
so a backtest over the past is replayable offline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from bisect import bisect_right
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Callable

from consilium.core.models import Bar, Fundamentals, Profile

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
USER_AGENT_ENV = "CONSILIUM_SEC_USER_AGENT"
MAX_RPS = 8                  # the SEC's published ceiling is 10
RETRIES = 3
ANNUAL_DAYS = (350, 380)     # a duration this long is a fiscal year
MARK_STALE_DAYS = 7

# Concept fallbacks, in priority order. Filers switch tags over the years
# (Revenues → RevenueFromContractWithCustomer… after ASC 606); for each field
# the tag with the most recent period wins.
REVENUE = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax")
NET_INCOME = ("NetIncomeLoss", "ProfitLoss")
OPERATING_INCOME = ("OperatingIncomeLoss",)
GROSS_PROFIT = ("GrossProfit",)
COST_OF_REVENUE = ("CostOfRevenue", "CostOfGoodsAndServicesSold")
DEPRECIATION = ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet")
OPERATING_CASH = ("NetCashProvidedByUsedInOperatingActivities",)
CAPEX = ("PaymentsToAcquirePropertyPlantAndEquipment",)
DIVIDENDS = ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends")
EQUITY = ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
DEBT_TOTAL = ("LongTermDebt",)
DEBT_PARTS = ("LongTermDebtNoncurrent", "LongTermDebtCurrent")
CASH = ("CashAndCashEquivalentsAtCarryingValue",)
CURRENT_ASSETS = ("AssetsCurrent",)
CURRENT_LIABILITIES = ("LiabilitiesCurrent",)
SHARES = ("dei:EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding")


class EdgarError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Facts: one company's filings, queried as of a date
# ---------------------------------------------------------------------------

class Facts:
    """companyfacts JSON reduced to {concept: [fact, ...]}; a fact is a dict
    with val, end, filed and (for durations) start."""

    def __init__(self, raw: dict) -> None:
        self.cik = raw.get("cik")
        self.name = raw.get("entityName")
        self._c: dict[str, list[dict]] = {}
        for ns, concepts in (raw.get("facts") or {}).items():
            for concept, body in concepts.items():
                key = concept if ns == "us-gaap" else f"{ns}:{concept}"
                for unit, rows in (body.get("units") or {}).items():
                    if unit not in ("USD", "shares"):
                        continue
                    self._c.setdefault(key, []).extend(
                        {"val": float(r["val"]), "end": r["end"], "filed": r["filed"], "start": r.get("start")}
                        for r in rows if r.get("val") is not None and r.get("end") and r.get("filed"))

    def _visible(self, concept: str, as_of: str) -> list[dict]:
        return [f for f in self._c.get(concept, ()) if f["filed"] <= as_of]

    @staticmethod
    def _as_known(facts: list[dict]) -> dict[tuple, dict]:
        """One value per period: the latest filing of it (a restatement replaces
        the original only from the day it was filed)."""
        out: dict[tuple, dict] = {}
        for f in facts:
            k = (f["start"], f["end"])
            if k not in out or f["filed"] >= out[k]["filed"]:
                out[k] = f
        return out

    def instant(self, concepts: tuple[str, ...], as_of: str) -> tuple[float | None, str | None]:
        """Latest balance-sheet value filed by as_of → (value, period end)."""
        best = None
        for c in concepts:
            facts = [f for f in self._visible(c, as_of) if f["start"] is None]
            if not facts:
                continue
            f = max(self._as_known(facts).values(), key=lambda f: (f["end"], f["filed"]))
            if best is None or f["end"] > best["end"]:
                best = f
        return (best["val"], best["end"]) if best else (None, None)

    def annual(self, concepts: tuple[str, ...], as_of: str, years_back: int = 0) -> float | None:
        """A fiscal-year flow filed by as_of: the latest year (years_back=0) or an earlier one."""
        best_series = None
        for c in concepts:
            facts = [f for f in self._visible(c, as_of) if f["start"] and _is_annual(f["start"], f["end"])]
            if not facts:
                continue
            series = sorted(self._as_known(facts).values(), key=lambda f: f["end"], reverse=True)
            if best_series is None or series[0]["end"] > best_series[0]["end"]:
                best_series = series
        if not best_series or len(best_series) <= years_back:
            return None
        return best_series[years_back]["val"]


def _is_annual(start: str, end: str) -> bool:
    days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
    return ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]


# ---------------------------------------------------------------------------
# The SEC client and its cache
# ---------------------------------------------------------------------------

def _http_fetcher() -> Callable[[str], dict]:
    ua = os.environ.get(USER_AGENT_ENV, "").strip()
    if not ua:
        raise EdgarError(f"SEC EDGAR requires a User-Agent naming who is asking. Set {USER_AGENT_ENV}="
                         "\"Your Name you@example.com\" and try again.")
    import httpx
    client = httpx.Client(headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=30.0)
    lock, last = threading.Lock(), [0.0]

    def fetch(url: str) -> dict:
        for attempt in range(RETRIES):
            with lock:
                wait = last[0] + 1 / MAX_RPS - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last[0] = time.monotonic()
                r = client.get(url)
            if r.status_code == 404:
                return {}
            if r.status_code in (429, 500, 502, 503) and attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 403:
                raise EdgarError(f"SEC EDGAR refused {url} (403). It rejects a User-Agent without a contact email: "
                                 f"set {USER_AGENT_ENV}=\"Your Name you@example.com\" (currently {ua!r}).")
            if r.status_code != 200:
                raise EdgarError(f"SEC EDGAR {r.status_code} for {url}: {r.text[:200]}")
            return r.json()
        raise EdgarError(f"SEC EDGAR gave up on {url} after {RETRIES} tries")

    return fetch


class EdgarCache:
    """SQLite: one compressed body per URL, with the date it was fetched."""

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS bodies (url TEXT PRIMARY KEY, fetched TEXT NOT NULL, body BLOB NOT NULL)")
        self._lock = threading.Lock()

    def get(self, url: str) -> tuple[dict, str] | None:
        with self._lock:
            row = self._db.execute("SELECT body, fetched FROM bodies WHERE url=?", (url,)).fetchone()
        return (json.loads(zlib.decompress(row[0])), row[1]) if row else None

    def put(self, url: str, body: dict, fetched: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO bodies VALUES (?,?,?)",
                             (url, fetched, zlib.compress(json.dumps(body, sort_keys=True).encode())))
            self._db.commit()


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

class EdgarProvider:
    """Prices and profiles from `prices`; fundamentals from EDGAR, point in time."""

    point_in_time = True

    def __init__(self, prices, *, fetch: Callable[[str], dict] | None = None, cache: EdgarCache | None = None,
                 today: Callable[[], str] | None = None) -> None:
        import consilium.core.paths as paths
        self._prices = prices
        self.name = f"{prices.name}+edgar"
        self.survivorship_free = False
        self._fetch = fetch or _http_fetcher()
        self._cache = cache or EdgarCache(paths.DATA_CACHE_DIR / "edgar.sqlite")
        self._today = today or (lambda: _date.today().isoformat())
        self._bars: dict[str, tuple[list[str], list[float], str]] = {}   # ticker -> (dates, closes, window end)
        self._facts: dict[str, tuple[Facts | None, str]] = {}    # ticker -> (facts, fetched)
        self._ciks: tuple[dict[str, int], str] | None = None
        self._splits: dict[str, list[tuple[str, float]]] = {}

    # --- the price side ---------------------------------------------------
    def prices(self, ticker: str, start: str, end: str) -> list[Bar]:
        bars = self._prices.prices(ticker, start, end)
        self._bars[ticker] = ([b.date for b in bars], [b.close for b in bars], end)
        return bars

    def profile(self, ticker: str) -> Profile | None:
        return self._prices.profile(ticker)

    def _close(self, ticker: str, as_of: str) -> float | None:
        if ticker not in self._bars:
            start = (_date.fromisoformat(as_of) - timedelta(days=MARK_STALE_DAYS)).isoformat()
            self.prices(ticker, start, as_of)
        dates, closes, _ = self._bars[ticker]
        i = bisect_right(dates, as_of)
        if i == 0 or (_date.fromisoformat(as_of) - _date.fromisoformat(dates[i - 1])).days > MARK_STALE_DAYS:
            return None
        return closes[i - 1]

    def _split_factor(self, ticker: str, since: str) -> float:
        """Product of every split after `since` (the filed share count's date)."""
        if ticker not in self._splits:
            get = getattr(self._prices, "splits", None)
            self._splits[ticker] = list(get(ticker, self._bars[ticker][2])) if get else []
        f = 1.0
        for d, ratio in self._splits[ticker]:
            if d > since:
                f *= ratio
        return f

    # --- the SEC side -----------------------------------------------------
    def _get(self, url: str, as_of: str) -> tuple[dict, str]:
        """(body, the day it was fetched). A body fetched on day F holds every
        filing made by F, so it answers any as_of <= F; later dates refetch."""
        cached = self._cache.get(url)
        if cached is not None and as_of <= cached[1]:
            return cached
        body, fetched = self._fetch(url), self._today()
        self._cache.put(url, body, fetched)
        return body, fetched

    def _cik(self, ticker: str, as_of: str) -> int | None:
        if self._ciks is None or as_of > self._ciks[1]:
            raw, fetched = self._get(TICKERS_URL, as_of)
            self._ciks = ({str(r["ticker"]).upper(): int(r["cik_str"]) for r in raw.values()}, fetched)
        ciks = self._ciks[0]
        return ciks.get(ticker.upper()) or ciks.get(ticker.upper().replace("-", "."))

    def facts(self, ticker: str, as_of: str) -> Facts | None:
        if ticker not in self._facts or as_of > self._facts[ticker][1]:
            cik = self._cik(ticker, as_of)
            raw, fetched = self._get(FACTS_URL.format(cik=cik), as_of) if cik else ({}, "9999-12-31")
            self._facts[ticker] = (Facts(raw) if raw.get("facts") else None, fetched)
        return self._facts[ticker][0]

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals | None:
        f = self.facts(ticker, as_of)
        if f is None:
            return None    # no SEC filer (an ETF, a foreign issuer): absent, not guessed
        rev, rev_prev = f.annual(REVENUE, as_of), f.annual(REVENUE, as_of, 1)
        ni, ni_prev = f.annual(NET_INCOME, as_of), f.annual(NET_INCOME, as_of, 1)
        op = f.annual(OPERATING_INCOME, as_of)
        gp = f.annual(GROSS_PROFIT, as_of)
        if gp is None and rev is not None and (cor := f.annual(COST_OF_REVENUE, as_of)) is not None:
            gp = rev - cor
        da = f.annual(DEPRECIATION, as_of)
        cfo, capex, divs = f.annual(OPERATING_CASH, as_of), f.annual(CAPEX, as_of), f.annual(DIVIDENDS, as_of)
        equity, _ = f.instant(EQUITY, as_of)
        debt, _ = f.instant(DEBT_TOTAL, as_of)
        if debt is None:
            parts = [f.instant((c,), as_of)[0] for c in DEBT_PARTS]
            debt = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
        cash, _ = f.instant(CASH, as_of)
        ca, _ = f.instant(CURRENT_ASSETS, as_of)
        cl, _ = f.instant(CURRENT_LIABILITIES, as_of)
        shares, shares_end = f.instant(SHARES, as_of)

        price = self._close(ticker, as_of)
        mcap = price * shares * self._split_factor(ticker, shares_end) if price and shares else None

        def ratio(a, b, positive_only=True):
            if a is None or b is None or b == 0 or (positive_only and b < 0):
                return None
            return a / b

        ebitda = op + da if op is not None and da is not None else None
        ev = mcap + (debt or 0.0) - (cash or 0.0) if mcap is not None else None
        return Fundamentals(
            ticker=ticker, as_of=as_of, known_at=as_of, point_in_time=True,
            market_cap=mcap, pe=ratio(mcap, ni), pb=ratio(mcap, equity), ps=ratio(mcap, rev),
            ev_ebitda=ratio(ev, ebitda), roe=ratio(ni, equity), roic=None,
            gross_margin=ratio(gp, rev), operating_margin=ratio(op, rev), net_margin=ratio(ni, rev),
            revenue_growth=(rev / rev_prev - 1) if rev is not None and rev_prev else None,
            earnings_growth=(ni / ni_prev - 1) if ni is not None and ni_prev and ni_prev > 0 else None,
            debt_to_equity=ratio(debt, equity), current_ratio=ratio(ca, cl),
            fcf_yield=ratio(cfo - capex, mcap) if cfo is not None and capex is not None else None,
            dividend_yield=ratio(divs, mcap) if divs is not None else None,
            beta=None,
        )
