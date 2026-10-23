"""Point-in-time fundamentals from EDGAR: filing-date visibility, restatements,
units, the warning rule, and a cache that replays without the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.backtest import backtest_fund
from consilium.backtest.engine import point_in_time_warning
from consilium.core.spec import Fund, load_spec
from consilium.data import SyntheticProvider
from consilium.data.edgar import FACTS_URL, TICKERS_URL, EdgarCache, EdgarError, EdgarProvider

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"


def _fy(year, val, filed):
    return {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": val, "filed": filed, "form": "10-K"}


def _at(end, val, filed):
    return {"end": end, "val": val, "filed": filed}


FACTS = {"cik": 320193, "entityName": "Example Corp", "facts": {
    "us-gaap": {
        "Revenues": {"units": {"USD": [
            _fy(2022, 1000.0, "2023-02-10"),
            _fy(2023, 1200.0, "2024-02-09"),
            _fy(2022, 1050.0, "2024-02-09"),                     # FY2022 restated in the FY2023 10-K
            {"start": "2023-07-01", "end": "2023-09-30", "val": 300.0, "filed": "2023-11-01"},   # a quarter: not a year
        ]}},
        "NetIncomeLoss": {"units": {"USD": [_fy(2022, 100.0, "2023-02-10"), _fy(2023, 150.0, "2024-02-09")]}},
        "StockholdersEquity": {"units": {"USD": [
            _at("2022-12-31", 500.0, "2023-02-10"), _at("2023-09-30", 550.0, "2023-11-01"),
            _at("2023-12-31", 600.0, "2024-02-09")]}},
    },
    "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        _at("2023-01-31", 10.0, "2023-02-10"), _at("2024-01-31", 10.0, "2024-02-09")]}}},
}}


class FakeSEC:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        if url == TICKERS_URL:
            return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Example Corp"}}
        if url == FACTS_URL.format(cik=320193):
            return FACTS
        return {}


class SplitPrices(SyntheticProvider):
    """Synthetic closes, plus a 4-for-1 split in mid 2023."""

    def splits(self, ticker, end):
        return [("2023-06-01", 4.0)] if ticker == "AAPL" else []


def _edgar(tmp_path, today="2024-06-01", prices=None, sec=None):
    return EdgarProvider(prices or SyntheticProvider(), fetch=sec or FakeSEC(),
                         cache=EdgarCache(tmp_path / "edgar.sqlite"), today=lambda: today)


# --- visibility ---------------------------------------------------------------

def test_a_year_end_filed_in_february_is_invisible_in_january(tmp_path):
    p = _edgar(tmp_path)
    jan = p.fundamentals("AAPL", "2024-01-15")
    feb = p.fundamentals("AAPL", "2024-02-09")
    assert jan.point_in_time is True
    assert jan.net_margin == pytest.approx(100 / 1000)            # FY2022: all that had been filed
    assert jan.roe == pytest.approx(100 / 550)                    # the Q3 balance sheet, filed in November
    assert jan.revenue_growth is None                             # one year visible: no growth to compute
    assert feb.net_margin == pytest.approx(150 / 1200)            # the 10-K lands on its filing date
    assert feb.roe == pytest.approx(150 / 600)


def test_a_restatement_replaces_the_original_only_from_its_filing_date(tmp_path):
    p = _edgar(tmp_path)
    assert p.facts("AAPL", "2024-01-15").annual(("Revenues",), "2024-01-15") == 1000.0
    assert p.facts("AAPL", "2024-03-01").annual(("Revenues",), "2024-03-01", years_back=1) == 1050.0
    assert p.fundamentals("AAPL", "2024-03-01").revenue_growth == pytest.approx(1200 / 1050 - 1)


def test_before_the_first_filing_there_is_nothing_to_see(tmp_path):
    f = _edgar(tmp_path).fundamentals("AAPL", "2023-02-09")
    assert f.market_cap is None and f.net_margin is None and f.roe is None


def test_market_cap_puts_filed_shares_on_the_split_adjusted_price_basis(tmp_path):
    p = _edgar(tmp_path, prices=SplitPrices())
    p.prices("AAPL", "2023-01-01", "2023-03-31")
    close = p._close("AAPL", "2023-03-01")
    f = p.fundamentals("AAPL", "2023-03-01")
    assert f.market_cap == pytest.approx(close * 10.0 * 4.0)      # shares filed before the 4:1 split
    assert f.pe == pytest.approx(f.market_cap / 100.0)


def test_a_name_with_no_sec_filings_is_absent_not_guessed(tmp_path):
    assert _edgar(tmp_path).fundamentals("SPY", "2024-03-01") is None


def test_the_sec_needs_to_know_who_is_asking(monkeypatch, tmp_path):
    monkeypatch.delenv("CONSILIUM_SEC_USER_AGENT", raising=False)
    with pytest.raises(EdgarError, match="User-Agent"):
        EdgarProvider(SyntheticProvider(), cache=EdgarCache(tmp_path / "e.sqlite"))


# --- the cache --------------------------------------------------------------------

def test_cache_replays_identically_offline_and_refetches_only_for_later_dates(tmp_path):
    first_sec = FakeSEC()
    first = _edgar(tmp_path, sec=first_sec).fundamentals("AAPL", "2024-03-01")
    assert len(first_sec.calls) == 2                              # the ticker map, then the company

    replay_sec = FakeSEC()
    replay = _edgar(tmp_path, sec=replay_sec).fundamentals("AAPL", "2024-03-01")
    assert replay == first and replay_sec.calls == []             # fetched 2024-06-01: still answers March

    later_sec = FakeSEC()
    _edgar(tmp_path, today="2024-08-01", sec=later_sec).fundamentals("AAPL", "2024-07-01")
    assert FACTS_URL.format(cik=320193) in later_sec.calls        # July may hold filings the cache cannot


# --- the warning ---------------------------------------------------------------------

class _LatestOnly(SyntheticProvider):
    name = "latest-only"
    point_in_time = False


def test_pit_warning_is_silenced_only_when_fundamentals_are_point_in_time(tmp_path):
    fundamental = load_spec(MANDATES / "committee-balanced.yaml")     # value_quality + LLM personas read fundamentals
    price_only = load_spec(MANDATES / "systematic-trend.yaml")
    assert point_in_time_warning(fundamental, _LatestOnly()) is not None
    assert point_in_time_warning(fundamental, _edgar(tmp_path)) is None
    assert point_in_time_warning(price_only, _LatestOnly()) is None


def test_backtest_on_edgar_carries_no_pit_warning(tmp_path):
    with pytest.warns(UserWarning, match="survivorship"):
        res = backtest_fund(Fund(load_spec(MANDATES / "committee-balanced.yaml")), "2024-01-01", "2024-03-31",
                            _edgar(tmp_path), ["AAPL", "MSFT"], monte_carlo_paths=10)
    assert res.point_in_time_warning is None and res.provider == "synthetic+edgar"
    vq = [v for s in res.records[-1].strategies for v in s.views if v.analyst == "value_quality"]
    assert {v.ticker: v.abstained for v in vq}["MSFT"] is True    # no filings for MSFT in the fake: it abstains
