"""The LLM code path, exercised with a fake client: persona parsing, caching,
abstention on garbage, Red Team haircuts, and the CIO's bounded authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from consilium.analysts import OwnerAnalyst, BearAnalyst
from consilium.analysts.base import AnalysisContext
from consilium.backtest import backtest_fund
from consilium.committee import Committee
from consilium.committee.cio import llm_cio, rules_cio
from consilium.committee.redteam import llm_red_team, rules_red_team
from consilium.core.models import RedTeamNote, View
from consilium.core.spec import Fund, load_spec
from consilium.data import SyntheticProvider
from consilium.data.features import PriceBook
from consilium.llm import PromptCache, extract_json

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"


class FakeLLM:
    """Deterministic stand-in: answers by role, counts calls."""
    model = "fake-1"

    def __init__(self, analyst_reply=None, redteam_reply=None, cio_reply=None, fail=False):
        self.calls = 0
        self.fail = fail
        self.analyst_reply = analyst_reply or {"stance": "bullish", "conviction": 70, "confidence": 60, "horizon_days": 200,
                                               "thesis": "Durable returns at a fair price.", "risks": ["multiple compression"]}
        self.redteam_reply = redteam_reply
        self.cio_reply = cio_reply

    def complete(self, system, user):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        if "RED TEAM" in system:
            return json.dumps(self.redteam_reply or {"notes": []})
        if "CHIEF INVESTMENT OFFICER" in system:
            return json.dumps(self.cio_reply or {})
        return "Here you go:\n```json\n" + json.dumps(self.analyst_reply) + "\n```"


def _ctx(date="2024-12-31"):
    book = PriceBook(SyntheticProvider(), "2024-01-01", "2024-12-31")
    return AnalysisContext(book=book, date=date, benchmark="SPY")


def test_persona_parses_and_caches(tmp_path):
    llm = FakeLLM()
    a = OwnerAnalyst(llm=llm, cache=PromptCache(tmp_path / "pc"))
    v1 = a.view("AAPL", _ctx())
    v2 = a.view("AAPL", _ctx())
    assert not v1.abstained and v1.conviction == pytest.approx(0.7) and v1.confidence == pytest.approx(0.6)
    assert v1.horizon_days == 200 and v1.risks == ["multiple compression"]
    assert llm.calls == 1 and v2.metadata["cached"] is True


def test_persona_abstains_on_failure_and_garbage(tmp_path):
    down = BearAnalyst(llm=FakeLLM(fail=True), cache=PromptCache(tmp_path / "a"))
    assert down.view("AAPL", _ctx()).abstained
    bad = BearAnalyst(llm=FakeLLM(analyst_reply={"stance": "sideways", "conviction": 50}), cache=PromptCache(tmp_path / "b"))
    v = bad.view("AAPL", _ctx())
    assert v.abstained and "parse failed" in v.thesis


def _views():
    return [View(analyst="owner", ticker="AAPL", date="d", conviction=0.8, confidence=0.7, thesis="great"),
            View(analyst="bear", ticker="AAPL", date="d", conviction=-0.4, confidence=0.6, thesis="levered"),
            View(analyst="owner", ticker="XOM", date="d", conviction=-0.6, confidence=0.5, thesis="cyclical")]


def test_llm_red_team_is_bounded_and_falls_back(tmp_path):
    conv = {"AAPL": 0.5, "XOM": -0.6}
    fallback = rules_red_team(conv, {}, _views(), 2, 0.6)
    llm = FakeLLM(redteam_reply={"notes": [{"ticker": "AAPL", "haircut": 95, "strongest_objection": "one bull, one bear"},
                                            {"ticker": "ZZZ", "haircut": 10}]})
    notes = llm_red_team(llm, PromptCache(tmp_path), "d", conv, _views(), 2, 0.6, fallback)
    by = {n.ticker: n for n in notes}
    assert by["AAPL"].haircut == 0.6 and by["AAPL"].source == "llm"      # capped at max_haircut
    assert "ZZZ" not in by and by["XOM"].source == "rules"               # unknown ignored, missing filled from rules
    assert llm_red_team(FakeLLM(fail=True), PromptCache(tmp_path / "x"), "d", conv, _views(), 2, 0.6, fallback) == fallback


def test_llm_cio_bounded_authority(tmp_path):
    conv = {"AAPL": 0.5, "XOM": -0.6, "PG": 0.2}
    notes = [RedTeamNote(ticker="AAPL", consensus=0.5, haircut=0.2)]
    reply = {"regime": "risk-off", "exposure_multiplier": 0.4,
             "final_convictions": {"AAPL": 0.95, "XOM": 0.5, "PG": -0.3}, "overrides": {"AAPL": "love it"}, "memo": "Cut risk."}
    memo = llm_cio(FakeLLM(cio_reply=reply), PromptCache(tmp_path), "d", conv, notes, None, [])
    assert memo.source == "llm" and memo.regime == "risk-off" and memo.exposure_multiplier == 0.4
    assert memo.final_convictions["AAPL"] == pytest.approx(0.4 + 0.25)   # haircut-adjusted 0.4, +0.25 max move
    assert memo.final_convictions["XOM"] == pytest.approx(-0.35)         # can move toward zero but not flip sign
    assert memo.final_convictions["PG"] == 0.0                           # cannot flip a long into a short
    assert memo.overrides == {"AAPL": "love it"} and memo.memo == "Cut risk."
    rules = rules_cio(conv, notes, None)
    assert rules.source == "rules" and rules.final_convictions["AAPL"] == pytest.approx(0.4)


def test_full_committee_with_fake_llm(monkeypatch, tmp_path):
    """End to end: staff the flagship mandate with a fake LLM and make sure the
    personas vote, the chair uses the LLM, and the record says so."""
    import consilium.committee as committee_mod
    import consilium.analysts.llm as llm_mod
    fake = FakeLLM(redteam_reply={"notes": [{"ticker": "AAPL", "haircut": 30, "strongest_objection": "crowded"}]},
                   cio_reply={"regime": "neutral", "exposure_multiplier": 0.8, "final_convictions": {}, "memo": "Steady."})
    monkeypatch.delenv("CONSILIUM_NO_LLM", raising=False)
    monkeypatch.setattr(committee_mod, "llm_available", lambda model=None: True)
    monkeypatch.setattr(committee_mod, "make_llm", lambda model=None: fake)
    monkeypatch.setattr(llm_mod, "make_llm", lambda model=None, **kw: fake)
    monkeypatch.setattr(llm_mod, "PromptCache", lambda: PromptCache(tmp_path / "pc"))
    fund = Fund(load_spec(MANDATES / "committee-balanced.yaml"))
    res = backtest_fund(fund, "2024-01-01", "2024-03-31", SyntheticProvider(), ["AAPL", "MSFT", "JPM"], monte_carlo_paths=10)
    rec = res.records[-1]
    owner_views = [v for s in rec.strategies for v in s.views if v.analyst == "owner"]
    assert owner_views and all(not v.abstained and v.conviction > 0 for v in owner_views)
    assert rec.verdict.cio.source == "llm" and rec.verdict.cio.exposure_multiplier == 0.8
    assert any(n.source == "llm" and n.ticker == "AAPL" for n in rec.verdict.red_team)
    assert res.llm_used is True
    assert fake.calls > 0


# --- model selection ------------------------------------------------------

def test_model_autoselects_a_configured_provider(monkeypatch):
    """Setting any one provider key is enough: the default model is only used
    when its own provider is configured, and a local keyless provider is never
    chosen automatically."""
    from consilium.llm import client as c
    for var in ("CONSILIUM_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CONSILIUM_NO_LLM", raising=False)

    assert c.current_model() == c.DEFAULT_MODEL          # nothing configured: honest default
    assert c.llm_available() is False                     # ...and it says so

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert c.provider_for(c.current_model()) == "openai"  # only OpenAI configured -> an OpenAI model
    assert c.llm_available() is True

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert c.current_model() == c.DEFAULT_MODEL           # default's own provider wins once configured

    monkeypatch.setenv("CONSILIUM_MODEL", "grok-4.3")     # an explicit choice is never overridden
    assert c.current_model() == "grok-4.3"


# --- reasoning models -----------------------------------------------------

class _Choice:
    def __init__(self, content, finish_reason="stop", reasoning=None):
        self.finish_reason = finish_reason
        self.message = type("M", (), {"content": content, "model_extra": {"reasoning": reasoning} if reasoning else {}})()


class _Resp:
    def __init__(self, choice):
        self.choices = [choice]
        self.usage = type("U", (), {"completion_tokens": 999})()


def _client_returning(choice, monkeypatch):
    from consilium.llm.client import OpenAICompatibleClient
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: type("C", (), {
        "chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(lambda **kw: _Resp(choice))})()})()})())
    return OpenAICompatibleClient("reasoner-1", "k", None)


def test_reasoning_model_truncation_is_reported_not_swallowed(monkeypatch):
    """A reasoning model that burns its whole budget on the trace must raise a
    message that names the cause, not return an empty string that becomes a
    mystery abstention."""
    from consilium.llm import LLMError
    c = _client_returning(_Choice("", finish_reason="length", reasoning="thinking..."), monkeypatch)
    with pytest.raises(LLMError, match="before writing an answer"):
        c.complete("s", "u")


def test_answer_inside_the_reasoning_trace_is_recovered(monkeypatch):
    c = _client_returning(_Choice("", reasoning='so the answer is {"stance": "hold"}'), monkeypatch)
    assert extract_json(c.complete("s", "u")) == {"stance": "hold"}


def test_truly_empty_response_raises(monkeypatch):
    from consilium.llm import LLMError
    c = _client_returning(_Choice(""), monkeypatch)
    with pytest.raises(LLMError, match="empty response"):
        c.complete("s", "u")
