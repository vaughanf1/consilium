"""The daily spend cap for the public demo: measure real spend, and DEGRADE to
rules rather than breaking when the budget is gone."""

from __future__ import annotations

import json

import pytest

from consilium.core.spec import load_spec
from consilium.server.guard import SpendGuard, _today
from pathlib import Path

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"


def _guard(monkeypatch, usage, cap=3.0, state=None):
    g = SpendGuard(daily_usd=cap)
    monkeypatch.setattr(g, "_usage", lambda: usage)
    g._state = state if state is not None else {}
    saved = {}
    monkeypatch.setattr(g, "_save_state", lambda s: (saved.update(s), setattr(g, "_state", s)))
    return g, saved


def test_first_call_of_the_day_anchors_the_baseline(monkeypatch):
    g, saved = _guard(monkeypatch, usage=12.50)
    st = g.status()
    assert saved["baseline"] == 12.50 and st["spent_usd"] == 0.0 and st["allow_llm"] is True


def test_spend_is_measured_against_todays_baseline(monkeypatch):
    g, _ = _guard(monkeypatch, usage=14.00, cap=3.0, state={"day": _today(), "baseline": 12.50})
    st = g.status()
    assert st["spent_usd"] == pytest.approx(1.50)
    assert st["remaining_usd"] == pytest.approx(1.50) and st["allow_llm"] is True


def test_over_budget_degrades_the_mandate_instead_of_refusing(monkeypatch):
    g, _ = _guard(monkeypatch, usage=16.00, cap=3.0, state={"day": _today(), "baseline": 12.50})
    st = g.status()
    assert st["spent_usd"] == pytest.approx(3.50) and st["allow_llm"] is False

    spec = load_spec(MANDATES / "council-live.yaml")
    assert spec.committee.use_llm is True
    returned = g.apply(spec)
    assert returned["allow_llm"] is False
    assert spec.committee.use_llm is False          # every paid stage now falls back to rules
    assert spec.committee.board is True             # the council still sits
    assert len(spec.committee.board_members) == 5


def test_a_new_day_resets_the_budget(monkeypatch):
    g, saved = _guard(monkeypatch, usage=16.00, cap=3.0, state={"day": "2020-01-01", "baseline": 12.50})
    st = g.status()
    assert saved["baseline"] == 16.00 and st["spent_usd"] == 0.0 and st["allow_llm"] is True


def test_usage_below_baseline_reanchors(monkeypatch):
    """A rotated key reads lower than the baseline; that must not show as
    negative spend or silently disable the cap."""
    g, saved = _guard(monkeypatch, usage=2.00, cap=3.0, state={"day": _today(), "baseline": 12.50})
    st = g.status()
    assert saved["baseline"] == 2.00 and st["spent_usd"] == 0.0 and st["allow_llm"] is True


def test_unreadable_usage_fails_open(monkeypatch):
    """A flaky provider check must not take the demo down."""
    g, _ = _guard(monkeypatch, usage=None)
    st = g.status()
    assert st["allow_llm"] is True and st["measured"] is False
    spec = load_spec(MANDATES / "council-live.yaml")
    g.apply(spec)
    assert spec.committee.use_llm is True


def test_cap_of_zero_disables_the_guard(monkeypatch):
    g, _ = _guard(monkeypatch, usage=99.0, cap=0.0)
    assert g.status() == {"enabled": False, "allow_llm": True}
