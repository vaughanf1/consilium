"""Historical index membership: per-date filtering, exit-only departures, and an
honest survivorship warning that only a membership file in use can silence."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.backtest import backtest_fund
from consilium.core.spec import Fund, load_spec
from consilium.data import SyntheticProvider
from consilium.data.membership import SURVIVORSHIP_WARNING, Membership
from consilium.portfolio.construction import apply_exit_only

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"
UNI = ["AAPL", "MSFT", "JPM", "XOM", "PG"]


def _csv(tmp_path, body, name="members.csv"):
    p = tmp_path / name
    p.write_text("ticker,start_date,end_date\n" + body)
    return p


@pytest.fixture
def members(tmp_path):
    # XOM leaves at the end of April; JPM only joins in March; the rest are members throughout
    return Membership.from_csv(_csv(tmp_path, "AAPL,2000-01-01,\nMSFT,2000-01-01,\nPG,2000-01-01,\n"
                                              "XOM,2000-01-01,2024-04-30\nJPM,2024-03-01,\n"))


class _RealWorld(SyntheticProvider):
    """Synthetic prices, but a provider that does not promise a survivor-free universe."""
    name = "synthetic-as-real"
    survivorship_free = False


# --- the file -------------------------------------------------------------------

def test_membership_per_date_with_multiple_spells(tmp_path):
    m = Membership.from_csv(_csv(tmp_path, "ABC,2020-01-01,2020-12-31\nabc,2022-06-01,\nOLD,2010-01-01,2015-01-01\n"))
    assert m.is_member("ABC", "2020-01-01") and m.is_member("ABC", "2020-12-31")
    assert not m.is_member("ABC", "2021-06-01")              # between spells
    assert m.is_member("ABC", "2030-01-01")                  # blank end = still a member
    assert not m.is_member("OLD", "2016-01-01") and not m.is_member("NEVER", "2020-01-01")
    assert m.members(["OLD", "ABC", "NEVER"], "2012-01-01") == ["OLD"]
    assert m.tickers == ["ABC", "OLD"]


@pytest.mark.parametrize("body", ["ABC,2020-13-01,\n", "ABC,2021-01-01,2020-01-01\n", ",2020-01-01,\n"])
def test_bad_rows_fail_loud(tmp_path, body):
    with pytest.raises(ValueError):
        Membership.from_csv(_csv(tmp_path, body))


def test_mandate_membership_resolves_against_the_mandate_file(tmp_path):
    _csv(tmp_path, "AAPL,2000-01-01,\n", name="m.csv")
    y = tmp_path / "fund.yaml"
    y.write_text((MANDATES / "systematic-trend.yaml").read_text() + "\nmembership: m.csv\n")
    fund = Fund(load_spec(y))
    assert fund.membership is not None and fund.membership.tickers == ["AAPL"]


# --- exit-only ------------------------------------------------------------------

def test_exit_only_shrinks_never_grows_or_flips():
    current = {"A": -0.10, "B": 0.10, "C": 0.10, "D": 0.10}
    targets = {"A": -0.20, "B": 0.05, "C": -0.10, "D": 0.0, "E": 0.3}
    out, events = apply_exit_only(targets, current, {"A", "B", "C", "D"})
    assert out == {"A": -0.10, "B": 0.05, "C": 0.0, "D": 0.0, "E": 0.3}   # E is a member: untouched
    assert {e.ticker for e in events} == {"A", "C"}
    assert all(e.limit == "index_exit_only" for e in events)


# --- in the cycle ------------------------------------------------------------------

def test_backtest_filters_by_membership_and_treats_departures_as_exit_only(members):
    res = backtest_fund(Fund(load_spec(MANDATES / "systematic-trend.yaml"), membership=members),
                        "2024-01-01", "2024-09-30", SyntheticProvider(), UNI, monte_carlo_paths=10)
    before_jpm = [r for r in res.records if r.as_of < "2024-03-01"]
    assert before_jpm and all("JPM" not in r.positions and "JPM" not in r.target_weights for r in before_jpm)
    assert all(any(s.ticker == "JPM" and "not an index member" in s.reason for s in r.skipped) for r in before_jpm)
    assert any("JPM" in r.positions for r in res.records if r.as_of >= "2024-03-01")

    # XOM is held short when it leaves; afterwards it may only shrink toward zero
    after = [r for r in res.records if r.as_of > "2024-04-30"]
    held_at_exit = [r for r in res.records if r.as_of <= "2024-04-30"][-1].positions.get("XOM", 0)
    assert held_at_exit < 0
    prev = held_at_exit
    for r in after:
        now = r.positions.get("XOM", 0)
        assert now <= 0 and abs(now) <= abs(prev)
        prev = now
    assert res.membership == members.source and res.survivorship_warning is None


def test_survivorship_warning_present_without_membership_absent_with_it(members):
    spec = load_spec(MANDATES / "systematic-trend.yaml")
    with pytest.warns(UserWarning, match="survivorship"):
        plain = backtest_fund(Fund(spec), "2024-01-01", "2024-03-31", _RealWorld(), UNI, monte_carlo_paths=10)
    assert plain.survivorship_warning == SURVIVORSHIP_WARNING
    fixed = backtest_fund(Fund(spec, membership=members), "2024-01-01", "2024-03-31", _RealWorld(), UNI,
                          monte_carlo_paths=10)
    assert fixed.survivorship_warning is None
    # the synthetic market invents its names, so it has no survivors to be biased by
    synth = backtest_fund(Fund(spec), "2024-01-01", "2024-03-31", SyntheticProvider(), UNI, monte_carlo_paths=10)
    assert synth.survivorship_warning is None


def test_without_membership_behaviour_is_unchanged(members):
    spec = load_spec(MANDATES / "systematic-trend.yaml")
    everyone = Membership({t: members._spells["AAPL"] for t in UNI}, source="all")
    a = backtest_fund(Fund(spec), "2024-01-01", "2024-06-30", SyntheticProvider(), UNI, monte_carlo_paths=10)
    b = backtest_fund(Fund(spec, membership=everyone), "2024-01-01", "2024-06-30", SyntheticProvider(), UNI,
                      monte_carlo_paths=10)
    assert a.nav == b.nav    # a file that admits everyone changes nothing
