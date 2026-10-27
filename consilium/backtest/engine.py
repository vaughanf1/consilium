"""backtest_fund — run_cycle looped over history against a persistent SimBroker.

The trading grid comes from the benchmark's actual bars at the mandate's
cadence. Every tick is the real run_cycle, so point-in-time discipline, the
committee, sizing, risk, and costs hold on every backtested date by
construction. Walk-forward: the window is split into in-sample / out-of-sample
halves and metrics are reported for each — if the second half looks nothing
like the first, you have been warned.
"""

from __future__ import annotations

import logging
import warnings
from datetime import date as _date
from typing import Callable

from pydantic import BaseModel

from consilium.backtest.metrics import Metrics, compute_metrics, monte_carlo, rolling_sharpe
from consilium.committee import Committee
from consilium.core.models import CycleRecord
from consilium.core.spec import Fund, normalize_universe
from consilium.data.features import PriceBook
from consilium.data.membership import SURVIVORSHIP_WARNING
from consilium.data.protocol import DataProvider
from consilium.execution.broker import SimBroker
from consilium.pipeline.cycle import CycleContext, run_cycle

logger = logging.getLogger(__name__)


class PointInTime(BaseModel):
    """What point-in-time enforcement did on this run. Prices are always
    point in time (the PriceBook never returns a bar after the date).
    Fundamentals are either as-filed (EDGAR, synthetic), withheld (a
    latest-only source on past dates: the analysts that read them abstained),
    or not needed (no analyst reads them)."""

    prices: str = "point-in-time"
    fundamentals: str                 # "as-filed" | "withheld" | "not-needed"
    provider: str
    withheld_rows: int = 0            # (ticker, date) rows refused
    meetings_affected: int = 0        # meetings on which at least one row was refused
    meetings: int = 0


class BacktestResult(BaseModel):
    fund: str
    start: str
    end: str
    rebalance: str
    benchmark: str
    universe: list[str]
    capital: float
    provider: str
    point_in_time: PointInTime | None = None
    point_in_time_warning: str | None = None
    survivorship_warning: str | None = None
    membership: str | None = None     # the membership file in force, if any
    dates: list[str]
    nav: list[float]
    benchmark_nav: list[float]
    gross_exposure: list[float]
    net_exposure: list[float]
    drawdown: list[float]
    regime: list[str]
    metrics: Metrics
    in_sample: Metrics | None = None
    out_of_sample: Metrics | None = None
    rolling_sharpe: list[float | None]
    monte_carlo: dict
    records: list[CycleRecord]
    model: str | None = None
    llm_used: bool = False


def reads_fundamentals(spec) -> bool:
    from consilium.analysts import ANALYST_REGISTRY
    return any(ANALYST_REGISTRY[n].needs_fundamentals for n in spec.analyst_names)


def point_in_time_warning(spec, provider) -> str | None:
    """Silent only when every analyst that reads fundamentals is fed by a
    provider that guarantees them point in time. One provider feeds a whole
    run, so that is: no such analyst, or a point-in-time provider (EDGAR, or
    the synthetic market, which has no filing lag). Otherwise the run still
    holds the line — the rows are withheld — and this says so up front."""
    if not reads_fundamentals(spec) or getattr(provider, "point_in_time", False):
        return None
    return (f"{provider.name} fundamentals are latest-only, so on past dates they are withheld: analysts that "
            "read them abstain rather than see today's ratios, and the committee runs on price-based signals. "
            "Set CONSILIUM_SEC_USER_AGENT (or use --provider edgar) for as-filed SEC fundamentals.")


def point_in_time_report(spec, provider, records: list[CycleRecord]) -> tuple[PointInTime, str | None]:
    """The structured account of what enforcement did, plus the message to
    show when any row was withheld (None when nothing was)."""
    withheld = sum(len(r.withheld) for r in records)
    affected = sum(1 for r in records if r.withheld)
    if not reads_fundamentals(spec):
        kind = "not-needed"
    elif withheld:
        kind = "withheld"
    else:
        kind = "as-filed"
    report = PointInTime(fundamentals=kind, provider=provider.name, withheld_rows=withheld,
                         meetings_affected=affected, meetings=len(records))
    message = None
    if withheld:
        message = (f"Point-in-time enforced: {withheld} fundamentals row{'s' if withheld != 1 else ''} on "
                   f"{affected} of {len(records)} meetings were withheld because {provider.name} publishes them "
                   "latest-only and they were not knowable on the meeting date. The analysts that read them "
                   "abstained; the committee ran on price-based signals. Set CONSILIUM_SEC_USER_AGENT (or use "
                   "--provider edgar) for as-filed SEC fundamentals.")
    return report, message


def rebalance_grid(days: list[str], cadence: str) -> list[str]:
    if cadence == "daily":
        return list(days)
    last: dict[tuple, str] = {}
    for d in days:
        dt = _date.fromisoformat(d)
        iso = dt.isocalendar()
        key = {"weekly": (iso[0], iso[1]), "biweekly": (iso[0], iso[1] // 2), "monthly": (dt.year, dt.month)}[cadence]
        last[key] = d
    return sorted(last.values())


def _years(a: str, b: str) -> float:
    return max((_date.fromisoformat(b) - _date.fromisoformat(a)).days / 365.25, 0.01)


def backtest_fund(fund: Fund, start: str, end: str, provider: DataProvider, universe: list[str], *,
                  model: str | None = None, on_cycle: Callable[[int, int, CycleRecord], None] | None = None,
                  monte_carlo_paths: int = 500) -> BacktestResult:
    spec = fund.spec
    universe = normalize_universe(universe)
    book = PriceBook(provider, start, end)
    days = book.trading_days(spec.benchmark)
    if not days:
        raise ValueError(f"{spec.name}: no {spec.benchmark} bars in [{start}, {end}] — cannot build the trading grid")
    grid = rebalance_grid(days, spec.rebalance)

    pit_warning = point_in_time_warning(spec, provider)
    if pit_warning:
        warnings.warn(pit_warning, stacklevel=2)

    # Silenced only by a membership file actually in use — or by a provider
    # whose universe cannot have survivors (the synthetic market invents its names).
    surv_warning = None
    if fund.membership is None and not getattr(provider, "survivorship_free", False):
        surv_warning = SURVIVORSHIP_WARNING
        warnings.warn(surv_warning, stacklevel=2)

    committee = Committee(fund, model=model)
    ctx = CycleContext(fund=fund, book=book, committee=committee, high_water=spec.capital)
    broker = SimBroker(cash=spec.capital, costs=spec.costs)

    records: list[CycleRecord] = []
    nav, bnav, gross, net, dd, regime = [], [], [], [], [], []
    base = book.mark(spec.benchmark, grid[0])
    for i, as_of in enumerate(grid):
        rec = run_cycle(ctx, as_of, broker, universe)
        records.append(rec)
        nav.append(rec.nav)
        bclose = rec.benchmark_close or base
        bnav.append(spec.capital * bclose / base)
        eq = rec.nav or 1.0
        gross.append(sum(abs(s * rec.marks[t]) for t, s in rec.positions.items()) / eq)
        net.append(sum(s * rec.marks[t] for t, s in rec.positions.items()) / eq)
        dd.append(rec.drawdown)
        regime.append(rec.verdict.cio.regime)
        if on_cycle:
            on_cycle(i, len(grid), rec)

    # The up-front warning said what WOULD happen; the report says what did.
    pit_report, pit_warning = point_in_time_report(spec, provider, records)

    yrs = _years(grid[0], grid[-1])
    turnovers = [r.turnover for r in records]
    costs = sum(r.costs for r in records)
    n_orders = sum(len(r.orders) for r in records)
    metrics = compute_metrics(spec.capital, nav, bnav, spec.rebalance, yrs, gross, turnovers, costs, n_orders)

    in_s = out_s = None
    half = len(nav) // 2
    if half >= 4:
        in_s = compute_metrics(spec.capital, nav[:half], bnav[:half], spec.rebalance, _years(grid[0], grid[half - 1]),
                               gross[:half], turnovers[:half], sum(r.costs for r in records[:half]),
                               sum(len(r.orders) for r in records[:half]))
        # out-of-sample is re-based to its own starting NAV so the two halves are comparable
        base_n, base_b = nav[half - 1], bnav[half - 1]
        out_s = compute_metrics(base_n, nav[half:], [b * base_n / base_b for b in bnav[half:]], spec.rebalance,
                                _years(grid[half - 1], grid[-1]), gross[half:], turnovers[half:],
                                sum(r.costs for r in records[half:]), sum(len(r.orders) for r in records[half:]))

    return BacktestResult(
        fund=spec.name, start=grid[0], end=grid[-1], rebalance=spec.rebalance, benchmark=spec.benchmark,
        universe=universe, capital=spec.capital, provider=provider.name,
        point_in_time=pit_report, point_in_time_warning=pit_warning,
        survivorship_warning=surv_warning, membership=fund.membership.source if fund.membership else None,
        dates=grid, nav=nav, benchmark_nav=bnav, gross_exposure=gross, net_exposure=net, drawdown=dd, regime=regime,
        metrics=metrics, in_sample=in_s, out_of_sample=out_s,
        rolling_sharpe=rolling_sharpe(spec.capital, nav, spec.rebalance),
        monte_carlo=monte_carlo(spec.capital, nav, n_paths=monte_carlo_paths), records=records,
        model=model, llm_used=committee._llm_ok or any(
            (not v.abstained and v.metadata.get("model")) for r in records for s in r.strategies for v in s.views),
    )
