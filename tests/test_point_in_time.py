"""Point-in-time is enforced, not assumed: a fundamentals row the meeting
date could not have had is withheld from every analyst, recorded on the
cycle, and reported on the backtest. On today's date a latest-only source is
point in time (today's figures on today's date) and nothing is withheld."""

from __future__ import annotations

from datetime import date as _date, timedelta
from pathlib import Path

import pytest

from consilium.analysts.base import AnalysisContext
from consilium.backtest import backtest_fund
from consilium.core.models import Fundamentals
from consilium.core.spec import Fund, load_spec
from consilium.data import CachedProvider, SyntheticProvider, available_providers, make_provider
from consilium.data.features import PriceBook
from consilium.pipeline.cycle import CycleContext, run_cycle
from consilium.committee import Committee
from consilium.execution.broker import SimBroker

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"
TODAY = "2025-06-30"


class LatestOnly(SyntheticProvider):
    """A yfinance-shaped source: the same (today's) figures whatever date is asked,
    stamped with the day they were downloaded."""
    name = "latest-only"
    point_in_time = False

    def __init__(self, today: str = TODAY) -> None:
        super().__init__()
        self.today = today
        self.calls = 0

    def fundamentals(self, ticker, as_of):
        self.calls += 1
        row = super().fundamentals(ticker, self.today)
        if row is None:
            return None
        return row.model_copy(update={"as_of": as_of, "known_at": self.today, "point_in_time": self.today <= as_of})


class Undated(SyntheticProvider):
    """A source that never says when its figures were published."""
    name = "undated"
    point_in_time = False

    def fundamentals(self, ticker, as_of):
        row = super().fundamentals(ticker, as_of)
        return row.model_copy(update={"known_at": None, "point_in_time": False}) if row else None


def _ctx(provider, date):
    return AnalysisContext(book=PriceBook(provider, "2025-01-01", TODAY), date=date, benchmark="SPY")


def test_knowable_on_is_strict():
    row = Fundamentals(ticker="AAPL", as_of="2024-06-03", known_at="2025-06-30")
    assert not row.knowable_on("2024-06-03")
    assert row.knowable_on("2025-06-30") and row.knowable_on("2025-07-01")
    assert not Fundamentals(ticker="AAPL", as_of="2024-06-03").knowable_on("2099-01-01")   # no provenance: never


def test_context_withholds_figures_the_date_could_not_have_had():
    ctx = _ctx(LatestOnly(), "2024-06-03")
    assert ctx.fundamentals("AAPL") is None
    assert "withheld" in ctx.withheld["AAPL"] and "2025-06-30" in ctx.withheld["AAPL"]
    assert ctx.no_fundamentals_reason("AAPL").startswith("fundamentals withheld")
    assert ctx.no_fundamentals_reason("MSFT") == "no fundamentals"          # never asked: plain absence


def test_context_serves_the_same_figures_on_the_day_they_were_downloaded():
    ctx = _ctx(LatestOnly(), TODAY)
    row = ctx.fundamentals("AAPL")
    assert row is not None and row.point_in_time and not ctx.withheld


def test_undated_figures_are_never_served():
    ctx = _ctx(Undated(), TODAY)
    assert ctx.fundamentals("AAPL") is None and "no publication date" in ctx.withheld["AAPL"]


def test_as_filed_sources_are_untouched():
    ctx = _ctx(SyntheticProvider(), "2024-06-03")
    row = ctx.fundamentals("AAPL")
    assert row is not None and row.known_at == "2024-06-03" and not ctx.withheld


def test_cache_keeps_the_download_date(tmp_path):
    src = LatestOnly()
    cached = CachedProvider(src, cache_dir=tmp_path / "cache")
    first = cached.fundamentals("AAPL", "2024-06-03")
    assert first.known_at == TODAY and src.calls == 1
    replay = CachedProvider(LatestOnly(today="2030-01-01"), cache_dir=tmp_path / "cache").fundamentals("AAPL", "2024-06-03")
    assert replay.known_at == TODAY            # the stored stamp, not the new instance's clock
    assert not replay.knowable_on("2024-06-03")


def test_cycle_records_withholding_and_the_value_analyst_abstains_with_the_reason():
    fund = Fund(load_spec(MANDATES / "committee-balanced.yaml"))
    provider = LatestOnly()
    book = PriceBook(provider, "2024-06-01", TODAY)
    ctx = CycleContext(fund=fund, book=book, committee=Committee(fund), high_water=fund.spec.capital)
    rec = run_cycle(ctx, "2024-06-03", SimBroker(cash=fund.spec.capital, costs=fund.spec.costs), ["AAPL", "MSFT"])
    assert {w.ticker for w in rec.withheld} == {"AAPL", "MSFT"}
    vq = [v for s in rec.strategies for v in s.views if v.analyst == "value_quality"]
    assert vq and all(v.abstained and v.metadata["abstain_reason"].startswith("fundamentals withheld") for v in vq)
    # price-based analysts carry on
    assert any(not v.abstained for s in rec.strategies for v in s.views if v.analyst == "trend")


def test_backtest_reports_what_enforcement_did():
    fund = Fund(load_spec(MANDATES / "committee-balanced.yaml"))
    with pytest.warns(UserWarning, match="withheld"):
        res = backtest_fund(fund, "2024-01-01", "2024-03-31", LatestOnly(), ["AAPL", "MSFT"], monte_carlo_paths=10)
    p = res.point_in_time
    assert p.prices == "point-in-time" and p.fundamentals == "withheld"
    assert p.meetings == len(res.records) and p.meetings_affected == p.meetings
    assert p.withheld_rows == 2 * p.meetings
    assert res.point_in_time_warning and "withheld" in res.point_in_time_warning

    clean = backtest_fund(fund, "2024-01-01", "2024-03-31", SyntheticProvider(), ["AAPL", "MSFT"], monte_carlo_paths=10)
    assert clean.point_in_time.fundamentals == "as-filed" and clean.point_in_time.withheld_rows == 0
    assert clean.point_in_time_warning is None

    price_only = backtest_fund(Fund(load_spec(MANDATES / "systematic-trend.yaml")), "2024-01-01", "2024-03-31",
                               LatestOnly(), ["AAPL", "MSFT"], monte_carlo_paths=10)
    assert price_only.point_in_time.fundamentals == "not-needed" and price_only.point_in_time_warning is None


def test_auto_provider_prefers_as_filed_fundamentals_when_the_sec_contact_is_set(monkeypatch):
    monkeypatch.delenv("CONSILIUM_SEC_USER_AGENT", raising=False)
    assert available_providers() == ["synthetic", "yfinance"]
    assert make_provider("auto").name == "yfinance"
    monkeypatch.setenv("CONSILIUM_SEC_USER_AGENT", "Test Runner test@example.com")
    assert available_providers() == ["synthetic", "yfinance", "edgar"]
    assert make_provider("auto").name == "yfinance+edgar" and make_provider("auto").point_in_time is True
    assert make_provider("yfinance").name == "yfinance"           # explicit stays explicit


def test_yfinance_rows_are_stamped_with_the_download_date():
    from consilium.data.yf import YFinanceProvider
    prov = YFinanceProvider(today=lambda: TODAY)
    prov._info["AAPL"] = {"quoteType": "EQUITY", "marketCap": 3e12, "trailingPE": 30.0}
    past = prov.fundamentals("AAPL", "2024-06-03")
    assert past.known_at == TODAY and past.point_in_time is False and not past.knowable_on("2024-06-03")
    now = prov.fundamentals("AAPL", TODAY)
    assert now.point_in_time is True and now.knowable_on(TODAY)
    later = prov.fundamentals("AAPL", (_date.fromisoformat(TODAY) + timedelta(days=1)).isoformat())
    assert later.point_in_time is True
