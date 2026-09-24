"""The Investment Committee: analysts -> pods -> Red Team -> CIO -> Verdict."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from consilium.analysts.base import AnalysisContext
from consilium.committee.blend import blend_views
from consilium.committee.board import Briefing, apply_resolution, convene_board
from consilium.committee.cio import llm_cio, rules_cio
from consilium.committee.redteam import llm_red_team, rules_red_team
from consilium.core.models import StrategyRecord, Verdict, View
from consilium.core.spec import Fund
from consilium.llm import LLMError, PromptCache, llm_available, make_llm

logger = logging.getLogger(__name__)


class Committee:
    """Runs the whole deliberation for one date. Holds the LLM client and prompt
    cache for the chair roles so they persist across cycles."""

    def __init__(self, fund: Fund, model: str | None = None, cache: PromptCache | None = None) -> None:
        self.fund = fund
        self.policy = fund.spec.committee
        self._model = model
        self._cache = cache or PromptCache()
        self._client = None
        self._llm_ok = self.policy.use_llm and llm_available(model)

    def _chair(self):
        if not self._llm_ok:
            return None
        if self._client is None:
            try:
                self._client = make_llm(self.policy.chair_model or self._model)
            except LLMError as exc:
                logger.warning("committee chair LLM unavailable: %s", exc)
                self._llm_ok = False
                return None
        return self._client

    def _collect_views(self, tickers: list[str], staff, ctx: AnalysisContext) -> list[View]:
        """Every analyst on every ticker. LLM analysts are I/O-bound, so a pod
        with any of them fans out over a thread pool; output order is fixed
        (ticker-major, then staff order) regardless of completion order."""
        jobs = [(t, a) for t in tickers for a in staff]
        if not any(a.kind == "llm" for a in staff) or len(jobs) < 2:
            return [a.view(t, ctx) for t, a in jobs]
        # Warm the shared point-in-time caches on this thread first so the
        # workers never race to build the same snapshot.
        for t in tickers:
            ctx.market(t)
            if any(a.needs_fundamentals for a in staff):
                ctx.fundamentals(t)
            ctx.profile(t)
        workers = int(os.environ.get("CONSILIUM_LLM_WORKERS", "8"))
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
            return list(pool.map(lambda job: job[1].view(job[0], ctx), jobs))

    def deliberate(self, tickers: list[str], ctx: AnalysisContext, drawdown: float = 0.0,
                   on_stage=None, on_vote=None) -> tuple[list[StrategyRecord], Verdict]:
        spec = self.fund.spec
        announce = on_stage or (lambda *a, **k: None)
        total = sum(s.weight for s, _ in self.fund.strategies)
        announce("analysts", "running", {"pods": len(self.fund.strategies), "tickers": len(tickers)})
        records: list[StrategyRecord] = []
        netted: dict[str, float] = {t: 0.0 for t in tickers}
        all_views: list[View] = []
        dispersion: dict[str, float] = {t: 0.0 for t in tickers}
        pod_summaries: list[str] = []

        for strategy, staff in self.fund.strategies:
            views = self._collect_views(tickers, staff, ctx)
            blend = blend_views(views, strategy.analyst_weights, strategy.blend, spec.rebalance_days)
            slice_ = strategy.weight / total
            for t, c in blend.convictions.items():
                netted[t] += slice_ * c
                dispersion[t] = max(dispersion[t], blend.dispersion.get(t, 0.0))
            records.append(StrategyRecord(name=strategy.name, slice=slice_, views=views, convictions=blend.convictions))
            all_views.extend(views)
            top = sorted(blend.convictions.items(), key=lambda kv: -abs(kv[1]))[:4]
            pod_summaries.append(f"  {strategy.title} ({slice_:.0%} of capital, {len(staff)} analysts): "
                                 + ", ".join(f"{t} {c:+.2f}" for t, c in top))

        announce("analysts", "done", {"views": len(all_views),
                                      "abstained": sum(1 for v in all_views if v.abstained)})
        notes = []
        if self.policy.red_team:
            announce("red_team", "running", {})
            notes = rules_red_team(netted, dispersion, all_views, self.policy.red_team_top_n, self.policy.max_haircut)
            chair = self._chair()
            if chair is not None:
                notes = llm_red_team(chair, self._cache, ctx.date, netted, all_views,
                                     self.policy.red_team_top_n, self.policy.max_haircut, notes)

        announce("red_team", "done", {"notes": [n.model_dump() for n in notes]})
        bench = ctx.benchmark_market()
        announce("cio", "running", {})
        if self.policy.cio:
            chair = self._chair()
            memo = (llm_cio(chair, self._cache, ctx.date, netted, notes, bench, pod_summaries)
                    if chair is not None else rules_cio(netted, notes, bench))
        else:
            memo = rules_cio(netted, notes, bench)
            memo.memo = "CIO disabled in mandate; haircuts applied, regime multiplier from rules."

        announce("cio", "done", {"regime": memo.regime, "exposure": memo.exposure_multiplier,
                                 "memo": memo.memo, "source": memo.source})
        convictions = memo.final_convictions
        exposure = memo.exposure_multiplier
        resolution = None
        if self.policy.board and self.policy.board_members:
            announce("board", "running", {"seats": [
                {"name": m.name, "display": __import__("consilium.committee.board", fromlist=["SEATS"]).SEATS[m.name].display,
                 "model": m.model or self._model} for m in self.policy.board_members]})
            briefing = Briefing(
                regime=memo.regime, cio_exposure=memo.exposure_multiplier, cio_memo=memo.memo,
                drawdown=drawdown, dispersion=(sum(dispersion.values()) / len(dispersion) if dispersion else 0.0),
                convictions=convictions, red_team=notes, market=bench,
                n_positions=sum(1 for c in convictions.values() if abs(c) >= 0.1),
            )
            resolution = convene_board(self.policy.board_members, briefing, self._cache, ctx.date,
                                       use_llm=self._chair() is not None, default_model=self._model,
                                       on_vote=on_vote)
            convictions = apply_resolution(convictions, resolution)
            exposure = resolution.exposure
            announce("board", "done", {"resolution": resolution.model_dump()})

        return records, Verdict(convictions=convictions, red_team=notes, cio=memo,
                                board=resolution, exposure_multiplier=exposure)
