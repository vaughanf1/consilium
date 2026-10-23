"""Analyst track records: the scorekeeper's arithmetic, the shrinkage and its cap,
the factors on the record, reflection prompts, and an exact regression with
everything switched off."""

from __future__ import annotations

from pathlib import Path

import pytest

from consilium.analysts import OwnerAnalyst
from consilium.analysts.base import AnalysisContext
from consilium.backtest import backtest_fund
from consilium.committee.blend import blend_views
from consilium.committee.scorekeeper import FACTOR_MAX, FACTOR_MIN, PRIOR_OBS, Scorekeeper, reliability_factor
from consilium.core.models import CIOMemo, CycleRecord, StrategyRecord, Verdict, View
from consilium.core.spec import BlendPolicy, Fund, load_spec
from consilium.data import SyntheticProvider
from consilium.data.features import PriceBook
from consilium.llm import PromptCache

from test_llm_path import FakeLLM

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"
UNI6 = ["AAPL", "MSFT", "NVDA", "JPM", "XOM", "PG"]


def _view(analyst, ticker, c, h=5, thesis="", abst=False):
    return View(analyst=analyst, ticker=ticker, date="x", conviction=c, horizon_days=h, thesis=thesis, abstained=abst)


def _rec(as_of, marks, views=()):
    return CycleRecord(id=as_of, fund="f", as_of=as_of, universe=sorted(marks), marks=marks,
                       strategies=[StrategyRecord(name="pod", slice=1.0, views=list(views), convictions={})],
                       verdict=Verdict(convictions={}, red_team=[], cio=CIOMemo()), target_weights={},
                       risk_events=[], final_weights={}, equity_before=1.0, cash_before=1.0, orders=[], fills=[],
                       positions={}, cash=1.0, nav=1.0)


# A five-trading-day call is due 8 calendar days later (ceil(5 × 365/252)).
LEDGER = [
    _rec("2024-01-05", {"A": 100.0, "B": 50.0}, [
        _view("bull", "A", +0.8, thesis="A rallies"),            # A goes to 110: right, +0.08
        _view("bull", "B", +0.4, thesis="B rallies too"),        # B goes to 45: wrong, −0.04
        _view("bear", "A", -0.5, thesis="A breaks"),             # wrong, −0.05
        _view("bear", "B", -0.05),                               # too faint to have a direction: not scored
        _view("quiet", "A", +0.9, abst=True),                    # abstained: not scored
        _view("slow", "A", +0.9, h=63),                          # not due for months
    ]),
    _rec("2024-01-12", {"A": 105.0, "B": 48.0}),                 # 7 days on: nothing is due yet
    _rec("2024-01-19", {"A": 110.0, "B": 45.0}),                 # 14 days on: the 5-day calls resolve here
]


# --- arithmetic ----------------------------------------------------------------

def test_scorekeeper_arithmetic_on_a_synthetic_ledger():
    k = Scorekeeper.from_records(LEDGER)
    s = k.scores(["bull", "bear", "quiet", "slow"])
    assert (s["bull"].n, s["bull"].hits) == (2, 1) and s["bull"].hit_rate == pytest.approx(0.5)
    assert s["bull"].avg_contribution == pytest.approx((0.8 * 0.10 + 0.4 * -0.10) / 2)
    assert (s["bear"].n, s["bear"].hits) == (1, 0) and s["bear"].avg_contribution == pytest.approx(-0.05)
    assert s["quiet"].n == 0 and s["quiet"].hit_rate is None and s["quiet"].reliability == 1.0
    assert s["slow"].n == 0 and s["slow"].pending == 1


def test_nothing_is_scored_before_its_horizon_has_run():
    k = Scorekeeper.from_records(LEDGER[:2])      # the 12th: A is already up, but the call is not due
    assert k.scores(["bull"])["bull"].n == 0 and k.scores(["bull"])["bull"].pending == 2


def test_a_name_without_a_mark_waits_rather_than_guessing():
    k = Scorekeeper()
    k.resolve(LEDGER[0].as_of, LEDGER[0].marks); k.add(LEDGER[0])
    k.resolve("2024-01-19", {"A": 110.0})         # B has left: no mark
    s = k.scores(["bull"])["bull"]
    assert s.n == 1 and s.pending == 1
    k.resolve("2024-01-26", {"A": 111.0, "B": 45.0})
    assert k.scores(["bull"])["bull"].n == 2


def test_an_analyst_in_two_pods_makes_one_call():
    rec = _rec("2024-01-05", {"A": 100.0}, [_view("bull", "A", 0.8)])
    rec.strategies.append(StrategyRecord(name="pod2", slice=0.5, views=[_view("bull", "A", 0.8)], convictions={}))
    k = Scorekeeper(); k.add(rec)
    assert k.scores(["bull"])["bull"].pending == 1


# --- shrinkage and the cap ---------------------------------------------------

def test_shrinkage_toward_one_and_the_hard_cap():
    assert reliability_factor(0, 0) == 1.0
    assert reliability_factor(14, 20) == pytest.approx(1.2)       # 70% over 20 calls is not 1.4
    assert reliability_factor(6, 20) == pytest.approx(0.8)
    assert reliability_factor(10, 10) == pytest.approx((10 + PRIOR_OBS / 2) / (10 + PRIOR_OBS) * 2)
    assert reliability_factor(10_000, 10_000) == FACTOR_MAX == 1.5
    assert reliability_factor(0, 10_000) == FACTOR_MIN == 0.5
    for h in range(0, 201, 7):
        assert FACTOR_MIN <= reliability_factor(h, 200) <= FACTOR_MAX


def test_blend_applies_the_factor_to_the_mandate_weight():
    views = [_view("a", "X", 1.0), _view("b", "X", -1.0)]
    even = blend_views(views, {"a": 1, "b": 1}, BlendPolicy(confidence_weighted=False, horizon_aware=False), 5)
    tilted = blend_views(views, {"a": 1, "b": 1}, BlendPolicy(confidence_weighted=False, horizon_aware=False), 5,
                         reliability={"a": 1.5, "b": 0.5})
    assert even.convictions["X"] == pytest.approx(0.0)
    assert tilted.convictions["X"] == pytest.approx((1.5 - 0.5) / 2.0)


# --- in the cycle ----------------------------------------------------------------

def _spec(name, **committee):
    spec = load_spec(MANDATES / f"{name}.yaml")
    return spec.model_copy(update={"committee": spec.committee.model_copy(update=committee)})


def test_reflection_and_weighting_off_reproduce_todays_behaviour_exactly():
    """Golden numbers measured on the code before track records existed."""
    for name, nav in (("committee-balanced", 110208.14277086951), ("systematic-trend", 103910.2519316859)):
        res = backtest_fund(Fund(load_spec(MANDATES / f"{name}.yaml")), "2024-01-01", "2024-12-31",
                            SyntheticProvider(), UNI6, monte_carlo_paths=10)
        assert res.nav[-1] == pytest.approx(nav, rel=1e-12)
        assert all(r.reliability == {} for r in res.records)
        assert res.records[-1].track_record["trend"].n > 0     # the scorecard is still kept


def test_factors_are_logged_on_the_record_and_bounded():
    res = backtest_fund(Fund(_spec("systematic-trend", track_record_weighting=True)), "2024-01-01", "2024-12-31",
                        SyntheticProvider(), UNI6, monte_carlo_paths=10)
    assert res.records[0].reliability == {a: 1.0 for a in ("trend", "lowvol", "meanrev")}   # no history yet
    last = res.records[-1]
    assert set(last.reliability) == {"trend", "lowvol", "meanrev"}
    assert all(0.5 <= f <= 1.5 for r in res.records for f in r.reliability.values())
    assert any(f != 1.0 for f in last.reliability.values())
    assert all(last.reliability[a] == last.track_record[a].reliability for a in last.reliability)


# --- reflection ---------------------------------------------------------------------

def test_reflection_summary_is_deterministic_and_names_the_worst_miss():
    a = Scorekeeper.from_records(LEDGER).summary("bull")
    b = Scorekeeper.from_records(LEDGER).summary("bull")
    assert a == b
    assert "of your last 2 calls, 1 was right" in a
    assert "worst recent miss: B on 2024-01-05, bullish +0.40" in a and '"B rallies too"' in a and "-10.0%" in a
    assert Scorekeeper.from_records(LEDGER).summary("slow") is None     # nothing resolved: nothing said


def test_reflection_reaches_the_prompt_and_its_cache_key(tmp_path):
    class Recording(FakeLLM):
        def complete(self, system, user):
            self.last_user = user
            return super().complete(system, user)

    book = PriceBook(SyntheticProvider(), "2024-01-01", "2024-12-31")
    summary = Scorekeeper.from_records(LEDGER).summary("bull")
    llm = Recording()
    owner = OwnerAnalyst(llm=llm, cache=PromptCache(tmp_path / "pc"))
    plain = owner.view("AAPL", AnalysisContext(book=book, date="2024-12-31", benchmark="SPY"))
    told = owner.view("AAPL", AnalysisContext(book=book, date="2024-12-31", benchmark="SPY",
                                              reflection={"owner": summary}))
    assert summary in llm.last_user
    assert plain.metadata["prompt_key"] != told.metadata["prompt_key"] and llm.calls == 2
    again = owner.view("AAPL", AnalysisContext(book=book, date="2024-12-31", benchmark="SPY",
                                               reflection={"owner": summary}))
    assert again.metadata["cached"] is True and llm.calls == 2     # same record, same prompt, no new spend
    assert not told.abstained and -1 <= told.conviction <= 1       # context, not authority


def test_paper_run_seeds_the_track_record_from_the_ledger(tmp_path):
    from consilium.committee import Committee
    from consilium.ledger import Ledger
    from consilium.pipeline import CycleContext, seed_track
    spec = _spec("systematic-trend", track_record_weighting=True)
    res = backtest_fund(Fund(spec), "2024-01-01", "2024-06-30", SyntheticProvider(), UNI6, monte_carlo_paths=10)
    led = Ledger(tmp_path / "l.sqlite")
    for r in res.records:
        led.record_cycle(r, mode="paper")
    fund = Fund(spec)
    ctx = CycleContext(fund=fund, book=PriceBook(SyntheticProvider(), "2024-06-01", "2024-07-05"),
                       committee=Committee(fund))
    seed_track(ctx, led)
    assert ctx.track.scores(["trend"]) == Scorekeeper.from_records(res.records).scores(["trend"])
