"""run_cycle — one tick of the fund. Same code path in backtest, paper, and live.

    prices -> committee (analysts -> pods -> red team -> CIO)
           -> sizing -> risk -> execution -> record

Only the clock and the broker change between modes. Deterministic given the
same spec, date, broker state, data, and prompt cache.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from consilium.analysts.base import AnalysisContext
from consilium.committee import Committee
from consilium.core.models import CycleRecord, Fill, TickerSkip
from consilium.core.spec import Fund, normalize_universe
from consilium.data.features import PriceBook
from consilium.execution.broker import Broker, build_orders
from consilium.portfolio.construction import apply_rebalance_band, size_book
from consilium.risk.limits import apply_limits


@dataclass
class CycleContext:
    """State that persists across cycles within one run: the committee (its
    LLM client + caches), the price book, and the high-water mark."""

    fund: Fund
    book: PriceBook
    committee: Committee
    high_water: float = 0.0
    sector_cache: dict[str, str] = field(default_factory=dict)

    def sector(self, t: str) -> str:
        if t not in self.sector_cache:
            self.sector_cache[t] = self.book.sector(t)
        return self.sector_cache[t]


def run_cycle(ctx: CycleContext, as_of: str, broker: Broker, universe: list[str],
              on_stage=None, on_vote=None) -> CycleRecord:
    t0 = time.perf_counter()
    spec = ctx.fund.spec
    universe = normalize_universe(universe)
    held = broker.positions()

    # --- marks ---------------------------------------------------------
    marks: dict[str, float] = {}
    skipped: list[TickerSkip] = []
    for t in sorted(set(universe) | set(held)):
        m = ctx.book.mark(t, as_of)
        if m is not None:
            marks[t] = m
        elif t in held:
            raise ValueError(f"held position {t} has no recent price as of {as_of}; cannot value the book")
        else:
            skipped.append(TickerSkip(ticker=t, reason="no recent price"))
    tradeable = [t for t in universe if t in marks]

    cash_before = broker.cash()
    equity_before = cash_before + sum(p.shares * marks[t] for t, p in held.items())
    if equity_before <= 0:
        raise ValueError(f"{spec.name}: equity {equity_before:.2f} as of {as_of} — cannot size against a non-positive book")
    ctx.high_water = max(ctx.high_water, equity_before)
    drawdown = equity_before / ctx.high_water - 1 if ctx.high_water > 0 else 0.0

    # --- committee -----------------------------------------------------
    actx = AnalysisContext(book=ctx.book, date=as_of, benchmark=spec.benchmark)
    strategy_records, verdict = ctx.committee.deliberate(
        tradeable, actx, drawdown=drawdown, on_stage=on_stage, on_vote=on_vote)

    # --- sizing --------------------------------------------------------
    vols = {t: (actx.market(t).vol_60 if actx.market(t) else None) for t in tradeable}
    gross_target = sum(s.weight * s.blend.gross_target for s, _ in ctx.fund.strategies) / sum(s.weight for s, _ in ctx.fund.strategies)
    sizing = size_book(verdict.convictions, vols, spec.portfolio, gross_target, verdict.exposure_multiplier)
    current_w = {t: p.shares * marks[t] / equity_before for t, p in held.items()}
    targets = apply_rebalance_band(sizing.weights, current_w, spec.portfolio.rebalance_band)

    # --- risk ----------------------------------------------------------
    sectors = {t: ctx.sector(t) for t in set(targets) | set(held)}
    risk = apply_limits(targets, spec.risk, sectors=sectors, vols=vols, drawdown=drawdown)

    # --- execution -----------------------------------------------------
    orders = build_orders(risk.weights, held, marks, equity_before, spec.portfolio.min_trade_pct)
    fills: list[Fill] = [broker.place_order(o) for o in orders]
    positions_after = {t: p.shares for t, p in broker.positions().items()}
    cash_after = broker.cash()
    nav = cash_after + sum(s * marks[t] for t, s in positions_after.items())
    costs = sum(f.commission + f.slippage_cost for f in fills)
    traded = sum(f.price * f.quantity for f in fills)

    bench_mark = ctx.book.mark(spec.benchmark, as_of)
    return CycleRecord(
        id=uuid.uuid4().hex[:12], fund=spec.name, as_of=as_of, universe=universe, marks=marks,
        sectors={t: sectors.get(t, "Unknown") for t in tradeable}, skipped=skipped,
        strategies=strategy_records, verdict=verdict, target_weights=targets, risk_events=risk.events,
        final_weights=risk.weights, equity_before=equity_before, cash_before=cash_before,
        orders=orders, fills=fills, positions=positions_after, cash=cash_after, nav=nav,
        costs=costs, turnover=traded / equity_before if equity_before else 0.0, drawdown=drawdown,
        benchmark_close=bench_mark, elapsed_s=round(time.perf_counter() - t0, 3),
    )
