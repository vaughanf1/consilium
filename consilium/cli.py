"""consilium — the command line.

    consilium demo                         offline backtest on synthetic data, no keys
    consilium backtest <mandate> --tickers AAPL,MSFT [--start --end --provider --model]
    consilium run <mandate> --tickers ...  one paper cycle as of today, book carried in the ledger
    consilium serve [--port 8765]          the web dashboard
    consilium funds | analysts | models    what is available
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table

from consilium import __version__
from consilium.core.paths import MANDATES_DIR, RUNS_DIR, ensure_home, load_env_file

console = Console(stderr=True)


def _resolve_mandate(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.exists():
        return p
    cand = MANDATES_DIR / (arg if arg.endswith(".yaml") else f"{arg}.yaml")
    if cand.exists():
        return cand
    raise SystemExit(f"mandate not found: {arg} (looked in {MANDATES_DIR})")


def _print_metrics(res) -> None:
    m = res.metrics
    t = Table(title=f"{res.fund}  {res.start} → {res.end}  ({res.rebalance}, {m.n_periods} periods, {res.provider})",
              show_header=True, header_style="bold")
    t.add_column("metric"); t.add_column("fund", justify="right"); t.add_column(res.benchmark, justify="right")
    t.add_row("total return", f"{m.total_return:+.1%}", f"{m.benchmark_return:+.1%}")
    t.add_row("CAGR", f"{m.cagr:+.1%}", f"{m.benchmark_cagr:+.1%}")
    t.add_row("volatility", f"{m.volatility:.1%}", "")
    t.add_row("Sharpe / Sortino / Calmar", f"{m.sharpe:.2f} / {m.sortino:.2f} / {m.calmar:.2f}", "")
    t.add_row("max drawdown", f"{m.max_drawdown:.1%}", f"{m.benchmark_max_drawdown:.1%}")
    t.add_row("beta / alpha", f"{m.beta:.2f} / {m.alpha:+.1%}", "")
    t.add_row("information ratio", f"{m.information_ratio:.2f}", "")
    t.add_row("hit rate", f"{m.hit_rate:.0%}", "")
    t.add_row("avg gross / turnover", f"{m.avg_gross_exposure:.0%} / {m.avg_turnover:.1%}", "")
    t.add_row("costs", f"${m.total_costs:,.0f} ({m.cost_drag:.2%})", "")
    if res.in_sample and res.out_of_sample:
        t.add_row("walk-forward Sharpe IS → OOS", f"{res.in_sample.sharpe:.2f} → {res.out_of_sample.sharpe:.2f}", "")
    mc = res.monte_carlo
    if mc.get("paths"):
        t.add_row(f"Monte Carlo ({mc['paths']} paths)",
                  f"P(loss) {mc['prob_loss']:.0%} · terminal p5 ${mc['terminal']['p5']:,.0f} / p95 ${mc['terminal']['p95']:,.0f}", "")
    console.print(t)
    if res.point_in_time_warning:
        console.print(f"[yellow]⚠ {res.point_in_time_warning}[/]")
    from consilium.analysts import ANALYST_REGISTRY
    staffed_llm = res.records and any(ANALYST_REGISTRY[v.analyst].kind == "llm"
                                      for s in res.records[0].strategies for v in s.views)
    if staffed_llm and not res.llm_used:
        console.print("[dim]LLM analysts abstained (no API key configured) — the committee ran on quant analysts and rules. "
                      "Set ANTHROPIC_API_KEY (or another provider) to staff the personas.[/]")


def cmd_backtest(args) -> None:
    from consilium.backtest import backtest_fund
    from consilium.core.spec import Fund, load_spec, normalize_universe
    from consilium.data import make_provider
    from consilium.ledger import Ledger

    spec = load_spec(_resolve_mandate(args.mandate))
    fund = Fund(spec, model=args.model)
    universe = normalize_universe(args.tickers)
    end = args.end or _date.today().isoformat()
    start = args.start or (_date.fromisoformat(end) - timedelta(weeks=args.weeks)).isoformat()
    provider = make_provider(args.provider)
    with console.status(f"[bold]{spec.name}[/]: backtesting {start} → {end} over {', '.join(universe)} ({provider.name})…"):
        res = backtest_fund(fund, start, end, provider, universe, model=args.model)
    out = Path(args.out) if args.out else RUNS_DIR / f"backtest-{spec.name}-{res.end}.json"
    out.write_text(res.model_dump_json())
    Ledger().record_backtest(res, out)
    _print_metrics(res)
    console.print(f"[dim]full result → {out}[/]")
    if args.json:
        print(res.model_dump_json())


def cmd_demo(args) -> None:
    args.mandate = args.mandate or "committee-balanced"
    args.tickers = args.tickers or "AAPL,MSFT,NVDA,GOOGL,AMZN,JPM,UNH,XOM,PG,CAT"
    args.provider = "synthetic"
    args.end = args.end or "2025-06-30"
    cmd_backtest(args)


def cmd_run(args) -> None:
    from consilium.committee import Committee
    from consilium.core.spec import Fund, load_spec, normalize_universe
    from consilium.data import make_provider
    from consilium.data.features import PriceBook
    from consilium.execution import SimBroker
    from consilium.ledger import Ledger
    from consilium.pipeline import CycleContext, run_cycle

    spec = load_spec(_resolve_mandate(args.mandate))
    fund = Fund(spec, model=args.model)
    universe = normalize_universe(args.tickers)
    as_of = args.date or _date.today().isoformat()
    provider = make_provider(args.provider)
    ledger = Ledger()
    prior = None if args.fresh else ledger.latest_book(spec.name)
    if prior:
        cash, positions, last = prior
        console.print(f"[dim]carrying the book from {last}: cash ${cash:,.0f}, {len(positions)} positions[/]")
        broker = SimBroker(cash=cash, costs=spec.costs, positions=positions)
    else:
        broker = SimBroker(cash=spec.capital, costs=spec.costs)
    start = (_date.fromisoformat(as_of) - timedelta(days=30)).isoformat()
    book = PriceBook(provider, start, as_of)
    ctx = CycleContext(fund=fund, book=book, committee=Committee(fund, model=args.model), high_water=spec.capital)
    hist = ledger.cycles(spec.name)
    if hist:
        ctx.high_water = max(c["nav"] for c in hist)
    with console.status(f"[bold]{spec.name}[/]: committee sitting as of {as_of} on {', '.join(universe)}…"):
        rec = run_cycle(ctx, as_of, broker, universe)
    ledger.record_cycle(rec, mode="paper")
    print(rec.model_dump_json(indent=2) if args.json else rec.model_dump_json())
    v = rec.verdict
    console.print(f"[bold]{spec.name}[/] @ {rec.as_of} · regime {v.cio.regime} ×{v.cio.exposure_multiplier:.2f} · "
                  f"{len(rec.orders)} orders · {len(rec.risk_events)} risk events · NAV ${rec.nav:,.2f}")
    console.print(f"[dim]{v.cio.memo}[/]")


def cmd_serve(args) -> None:
    import uvicorn
    from consilium.server.app import create_app
    console.print(f"Consilium dashboard → http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


def cmd_funds(args) -> None:
    from consilium.core.spec import load_spec
    t = Table(title=f"mandates in {MANDATES_DIR}")
    for c in ("name", "pods", "analysts", "cadence", "capital"):
        t.add_column(c)
    for p in sorted(MANDATES_DIR.glob("*.yaml")):
        try:
            s = load_spec(p)
            t.add_row(s.name, ", ".join(x.title for x in s.strategies), ", ".join(s.analyst_names), s.rebalance, f"${s.capital:,.0f}")
        except Exception as exc:
            t.add_row(p.stem, f"[red]invalid: {exc}[/]", "", "", "")
    console.print(t)


def cmd_analysts(args) -> None:
    from consilium.analysts import describe_analysts
    t = Table(title="analyst roster")
    for c in ("name", "display", "kind", "horizon", "summary"):
        t.add_column(c)
    for a in describe_analysts():
        t.add_row(a["name"], a["display"], a["kind"], f"{a['horizon_days']}d", a["summary"][:90])
    console.print(t)


def cmd_models(args) -> None:
    from consilium.llm import list_models, current_model
    t = Table(title=f"models (current: {current_model()})")
    for c in ("id", "display", "provider", "key", "configured"):
        t.add_column(c)
    for m in list_models():
        t.add_row(m["id"], m["display"], m["provider"], m["env"] or "none (local)", "✓" if m["configured"] else "—")
    console.print(t)


def main(argv=None) -> None:
    load_env_file()
    ensure_home()
    p = argparse.ArgumentParser(prog="consilium", description="An AI investment committee you can backtest and interrogate.")
    p.add_argument("--version", action="version", version=f"consilium {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, mandate_required=True):
        sp.add_argument("mandate", nargs=None if mandate_required else "?", help="mandate name or YAML path")
        sp.add_argument("--tickers", help="comma/space separated universe")
        sp.add_argument("--provider", default="auto", help="synthetic | yfinance (default auto → yfinance)")
        sp.add_argument("--model", help="LLM for the personas, e.g. claude-sonnet-5 or ollama:llama3.1")
        sp.add_argument("--json", action="store_true", help="print the full JSON to stdout")

    bt = sub.add_parser("backtest", help="backtest a mandate over history"); common(bt)
    bt.add_argument("--start"); bt.add_argument("--end"); bt.add_argument("--weeks", type=int, default=78); bt.add_argument("--out")
    bt.set_defaults(fn=cmd_backtest)

    dm = sub.add_parser("demo", help="offline demo backtest on synthetic data (no keys, no network)"); common(dm, False)
    dm.add_argument("--start"); dm.add_argument("--end"); dm.add_argument("--weeks", type=int, default=78); dm.add_argument("--out")
    dm.set_defaults(fn=cmd_demo)

    rn = sub.add_parser("run", help="run one paper cycle as of a date; the book carries in the ledger"); common(rn)
    rn.add_argument("--date"); rn.add_argument("--fresh", action="store_true", help="ignore the ledger and start from the mandate's capital")
    rn.set_defaults(fn=cmd_run)

    sv = sub.add_parser("serve", help="launch the web dashboard")
    sv.add_argument("--host", default="127.0.0.1"); sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(fn=cmd_serve)

    sub.add_parser("funds", help="list mandates").set_defaults(fn=cmd_funds)
    sub.add_parser("analysts", help="list the analyst roster").set_defaults(fn=cmd_analysts)
    sub.add_parser("models", help="list LLMs and which keys are configured").set_defaults(fn=cmd_models)

    args = p.parse_args(argv)
    if getattr(args, "model", None):
        os.environ["CONSILIUM_MODEL"] = args.model
    if args.cmd in ("backtest", "run") and not args.tickers:
        p.error("--tickers is required (a mandate carries no watchlist)")
    args.fn(args)


if __name__ == "__main__":
    main()
