"""The investment board: rule versions, aggregation, bounded authority, and the
multi-model path through a fake gateway."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from consilium.committee.board import SEATS, Briefing, apply_resolution, convene_board, resolve
from consilium.core.models import BoardVote, RedTeamNote
from consilium.core.spec import BoardMemberSpec, FundSpec, load_spec
from consilium.llm import PromptCache

MANDATES = Path(__file__).resolve().parent.parent / "consilium" / "mandates"


def _briefing(regime="neutral", cio=0.8, drawdown=0.0, dispersion=0.2, convictions=None):
    return Briefing(regime=regime, cio_exposure=cio, cio_memo="proposal", drawdown=drawdown,
                    dispersion=dispersion, convictions=convictions or {"AAPL": 0.6, "MSFT": 0.3, "XOM": -0.4},
                    red_team=[RedTeamNote(ticker="AAPL", consensus=0.6, haircut=0.2)], market=None, n_positions=3)


def _seats(*names):
    return [BoardMemberSpec(name=n) for n in names]


def test_rule_board_actually_disagrees():
    """The point of a board is dissent. In a risk-off drawdown the archetypes
    must not all land on the same number."""
    b = _briefing(regime="risk-off", cio=0.5, drawdown=-0.12, dispersion=0.5)
    res = convene_board(_seats(*SEATS), b, PromptCache(), "2024-01-01", use_llm=False, default_model=None)
    votes = {v.member: v.exposure_vote for v in res.votes}
    assert len(votes) == 5
    assert max(votes.values()) - min(votes.values()) > 0.25      # a real spread
    assert votes["concentrator"] > votes["preserver"]            # and it runs the right way round
    assert res.summary and not res.unanimous


def test_median_stops_one_seat_hijacking_the_fund():
    votes = [BoardVote(member=m, display=m, stance="hold", exposure_vote=e)
             for m, e in [("a", 0.8), ("b", 0.75), ("c", 0.8), ("d", 0.05), ("e", 0.85)]]
    res = resolve(votes, cio_exposure=0.8)
    assert res.exposure == pytest.approx(0.8)                    # the 0.05 outlier is outvoted
    assert [v.dissent for v in res.votes if v.member == "d"] == [True]


def test_board_authority_is_bounded_to_the_cio_proposal():
    votes = [BoardVote(member=str(i), display=str(i), stance="add risk", exposure_vote=1.0) for i in range(5)]
    assert resolve(votes, cio_exposure=0.3).exposure == pytest.approx(0.65)   # +0.35 cap
    votes = [BoardVote(member=str(i), display=str(i), stance="cut risk", exposure_vote=0.0) for i in range(5)]
    assert resolve(votes, cio_exposure=0.9).exposure == pytest.approx(0.55)   # -0.35 cap


def test_trim_needs_a_majority_and_never_flips_a_sign():
    mk = lambda m, trim: BoardVote(member=m, display=m, stance="hold", exposure_vote=0.8, trim=trim)
    res = resolve([mk("a", ["AAPL"]), mk("b", ["AAPL"]), mk("c", ["AAPL"]), mk("d", ["XOM"]), mk("e", [])], 0.8)
    assert res.trimmed == {"AAPL": 3}                            # XOM had one vote, not a majority
    out = apply_resolution({"AAPL": 0.6, "XOM": -0.4}, res)
    assert out["AAPL"] == pytest.approx(0.3) and out["XOM"] == pytest.approx(-0.4)
    assert all((out[t] >= 0) == (v >= 0) for t, v in {"AAPL": 0.6, "XOM": -0.4}.items())


class FakeGateway:
    """Stands in for OpenRouter: answers per model id, records who was asked."""

    def __init__(self, replies):
        self.replies = replies
        self.asked = []

    def client_for(self, model):
        gateway = self

        class C:
            def __init__(self):
                self.model = model

            def complete(self, system, user):
                gateway.asked.append(model)
                return json.dumps(gateway.replies[model])
        return C()


def test_each_seat_can_run_a_different_model(monkeypatch, tmp_path):
    import consilium.committee.board as board
    replies = {
        "m-bull": {"stance": "add risk", "exposure_vote": 95, "trim": [], "concern": "press the winners"},
        "m-bear": {"stance": "cut risk", "exposure_vote": 30, "trim": ["AAPL"], "concern": "too much in one name"},
        "m-mid": {"stance": "hold", "exposure_vote": 70, "trim": ["AAPL"], "concern": "steady"},
    }
    gw = FakeGateway(replies)
    monkeypatch.setattr(board, "make_llm", lambda model=None, **kw: gw.client_for(model))
    members = [BoardMemberSpec(name="macro", model="m-bull"),
               BoardMemberSpec(name="preserver", model="m-bear"),
               BoardMemberSpec(name="systematizer", model="m-mid")]
    res = convene_board(members, _briefing(), PromptCache(tmp_path), "2024-01-01", use_llm=True, default_model=None)
    assert sorted(gw.asked) == ["m-bear", "m-bull", "m-mid"]           # three different models voted
    assert {v.model for v in res.votes} == {"m-bull", "m-bear", "m-mid"}
    assert all(v.source == "llm" for v in res.votes)
    assert res.exposure == pytest.approx(0.7)                          # median of 0.95 / 0.30 / 0.70
    assert res.trimmed == {"AAPL": 2}                                  # 2 of 3 is a majority


def test_a_broken_seat_falls_back_to_its_rule(monkeypatch, tmp_path):
    import consilium.committee.board as board

    def boom(model=None, **kw):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(board, "make_llm", boom)
    res = convene_board(_seats("macro", "preserver"), _briefing(), PromptCache(tmp_path), "2024-01-01",
                        use_llm=True, default_model=None)
    assert [v.source for v in res.votes] == ["rules", "rules"] and res.exposure > 0
    # and the transcript says WHY it fell back, rather than looking like "no key"
    assert all("gateway down" in (v.fallback_reason or "") for v in res.votes)


def test_board_changes_the_book_end_to_end():
    """A board that votes to cut risk must actually shrink the book."""
    from consilium.backtest import backtest_fund
    from consilium.core.spec import Fund
    from consilium.data import SyntheticProvider
    spec = load_spec(MANDATES / "committee-balanced.yaml")
    assert spec.committee.board and len(spec.committee.board_members) == 5
    with_board = backtest_fund(Fund(spec), "2024-01-01", "2024-06-30", SyntheticProvider(),
                               ["AAPL", "MSFT", "JPM", "XOM"], monte_carlo_paths=5)
    spec.committee.board = False
    without = backtest_fund(Fund(spec), "2024-01-01", "2024-06-30", SyntheticProvider(),
                            ["AAPL", "MSFT", "JPM", "XOM"], monte_carlo_paths=5)
    rec = with_board.records[-1]
    assert rec.verdict.board is not None and len(rec.verdict.board.votes) == 5
    assert rec.verdict.exposure_multiplier == rec.verdict.board.exposure
    assert without.records[-1].verdict.board is None
    # the board is not decoration: it moved the exposure the fund actually ran
    assert any(r.verdict.board.exposure != r.verdict.cio.exposure_multiplier for r in with_board.records)
    assert with_board.metrics.avg_gross_exposure != without.metrics.avg_gross_exposure


def test_mandate_rejects_an_unknown_seat():
    with pytest.raises(Exception):
        FundSpec(name="x", strategies=[{"name": "a", "analysts": [{"name": "trend"}]}],
                 committee={"board": True, "board_members": [{"name": "nope"}]})
