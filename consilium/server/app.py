"""The Consilium web app: a JSON API over the engine plus the dashboard.

Everything the dashboard shows comes through /api. Backtests and cycles run as
background jobs; the UI polls /api/jobs/{id} and draws the equity curve as it
grows.
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from consilium import __version__
from consilium.analysts import describe_analysts
from consilium.core.paths import MANDATES_DIR, RUNS_DIR, SERVERLESS, ensure_home, load_env_file
from consilium.core.spec import FundSpec, dump_spec, load_spec
from consilium.data import make_provider
from consilium.ledger import Ledger
from consilium.llm import current_model, list_models, llm_available
from consilium.server.guard import SpendGuard
from consilium.server.jobs import JobRunner
from consilium.storage import blob_store

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"


class BacktestRequest(BaseModel):
    fund: str
    tickers: str = "AAPL,MSFT,NVDA,GOOGL,AMZN,JPM,UNH,XOM,PG,CAT"
    start: str | None = None
    end: str | None = None
    provider: str = "synthetic"
    model: str | None = None
    weeks: int = Field(default=78, ge=4, le=1040)


class RunRequest(BaseModel):
    fund: str
    tickers: str
    date: str | None = None
    provider: str = "synthetic"
    model: str | None = None
    fresh: bool = False
    sync: bool = True


class MandateBody(BaseModel):
    yaml: str


def _mandate_path(name: str) -> Path:
    p = MANDATES_DIR / f"{name}.yaml"
    if not p.exists():
        raise HTTPException(404, f"mandate {name!r} not found")
    return p


def _spec_summary(spec: FundSpec) -> dict:
    return {"name": spec.name, "description": spec.description, "rebalance": spec.rebalance, "benchmark": spec.benchmark,
            "capital": spec.capital, "uses_llm": spec.uses_llm, "analysts": spec.analyst_names,
            "strategies": [{"name": s.name, "title": s.title, "weight": s.weight,
                            "analysts": [a.name for a in s.analysts], "market_neutral": s.blend.market_neutral}
                           for s in spec.strategies],
            "committee": spec.committee.model_dump(), "risk": spec.risk.model_dump(), "portfolio": spec.portfolio.model_dump(),
            "costs": spec.costs.model_dump()}


def create_app() -> FastAPI:
    load_env_file()
    ensure_home()
    app = FastAPI(title="Consilium", version=__version__)
    jobs = JobRunner()
    ledger = Ledger()
    # A hosted demo hands a key to the internet. Cap the day's spend; past it the
    # committee degrades to its rule versions instead of the page breaking.
    guard = SpendGuard()

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "model": current_model(), "llm_available": llm_available(),
                "home": str(MANDATES_DIR.parent), "serverless": SERVERLESS, "durable": blob_store() is not None,
                "budget": guard.status()}

    @app.get("/api/meta")
    def meta():
        return {"analysts": describe_analysts(), "models": list_models(), "current_model": current_model(),
                "llm_available": llm_available(), "providers": ["synthetic", "yfinance"], "serverless": SERVERLESS,
                "durable": blob_store() is not None, "budget": guard.status()}

    # mandates -----------------------------------------------------------
    @app.get("/api/funds")
    def funds():
        out = []
        for p in sorted(MANDATES_DIR.glob("*.yaml")):
            try:
                out.append(_spec_summary(load_spec(p)))
            except Exception as exc:
                out.append({"name": p.stem, "error": str(exc)})
        return out

    @app.get("/api/funds/{name}")
    def fund(name: str):
        p = _mandate_path(name)
        spec = load_spec(p)
        return {**_spec_summary(spec), "yaml": p.read_text()}

    @app.put("/api/funds/{name}")
    def save_fund(name: str, body: MandateBody):
        try:
            data = yaml.safe_load(body.yaml) or {}
            spec = FundSpec(**data)
        except Exception as exc:
            raise HTTPException(400, f"invalid mandate: {exc}")
        if spec.name != name:
            raise HTTPException(400, f"mandate name {spec.name!r} must match {name!r}")
        (MANDATES_DIR / f"{name}.yaml").write_text(body.yaml)
        if blob_store() is not None:
            blob_store().put(f"mandates/{name}.yaml", body.yaml.encode(), "application/yaml")
        return _spec_summary(spec)

    @app.delete("/api/funds/{name}")
    def delete_fund(name: str):
        _mandate_path(name).unlink()
        if blob_store() is not None:
            blob_store().delete(f"mandates/{name}.yaml")
        return {"deleted": name}

    # streaming backtest: one request, NDJSON progress lines, the full result last.
    # Works the same locally and on a serverless host (no background state needed).
    @app.post("/api/backtest/stream")
    def stream_backtest(req: BacktestRequest):
        from consilium.backtest import backtest_fund
        from consilium.core.spec import Fund, normalize_universe
        spec = load_spec(_mandate_path(req.fund))
        budget = guard.apply(spec)
        universe = normalize_universe(req.tickers)
        end = req.end or (_date.today().isoformat() if req.provider != "synthetic" else "2025-06-30")
        start = req.start or (_date.fromisoformat(end) - timedelta(weeks=req.weeks)).isoformat()

        def gen():
            q: queue.Queue = queue.Queue()
            base = {}

            def on_cycle(i, n, rec):
                if "b" not in base:
                    base["b"] = rec.benchmark_close or 1.0
                q.put({"type": "progress", "i": i + 1, "n": n, "date": rec.as_of, "nav": round(rec.nav, 2),
                       "benchmark_nav": round(spec.capital * (rec.benchmark_close or base["b"]) / base["b"], 2),
                       "regime": rec.verdict.cio.regime})

            def work():
                try:
                    provider = make_provider(req.provider)
                    res = backtest_fund(Fund(spec, model=req.model), start, end, provider, universe,
                                        model=req.model, on_cycle=on_cycle)
                    try:
                        out = RUNS_DIR / f"backtest-{spec.name}-{res.end}.json"
                        out.write_text(res.model_dump_json())
                        ledger.record_backtest(res, out)
                    except OSError:
                        pass
                    q.put({"type": "result", "result": json.loads(res.model_dump_json())})
                except Exception as exc:
                    q.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                q.put(None)

            threading.Thread(target=work, daemon=True).start()
            yield json.dumps({"type": "start", "fund": spec.name, "start": start, "end": end,
                              "universe": universe, "budget": budget}) + "\n"
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield json.dumps({"type": "keepalive"}) + "\n"
                    continue
                if item is None:
                    break
                yield json.dumps(item) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # the council, live: one request, every stage narrated as it happens
    @app.post("/api/council/stream")
    def council_stream(req: RunRequest):
        from consilium.committee import Committee
        from consilium.core.spec import Fund, normalize_universe
        from consilium.data.features import PriceBook
        from consilium.execution import SimBroker
        from consilium.pipeline import CycleContext, run_cycle
        spec = load_spec(_mandate_path(req.fund))
        budget = guard.apply(spec)
        universe = normalize_universe(req.tickers)
        as_of = req.date or (_date.today().isoformat() if req.provider != "synthetic" else "2025-06-30")

        def gen():
            q: queue.Queue = queue.Queue()

            def work():
                try:
                    provider = make_provider(req.provider)
                    fund = Fund(spec, model=req.model)
                    prior = None if req.fresh else ledger.latest_book(spec.name)
                    broker = (SimBroker(cash=prior[0], costs=spec.costs, positions=prior[1]) if prior
                              else SimBroker(cash=spec.capital, costs=spec.costs))
                    book = PriceBook(provider, (_date.fromisoformat(as_of) - timedelta(days=30)).isoformat(), as_of)
                    ctx = CycleContext(fund=fund, book=book, committee=Committee(fund, model=req.model),
                                       high_water=spec.capital)
                    q.put({"type": "stage", "name": "data", "state": "done",
                           "detail": {"tickers": universe, "as_of": as_of, "provider": provider.name}})
                    rec = run_cycle(ctx, as_of, broker, universe,
                                    on_stage=lambda name, state, detail: q.put(
                                        {"type": "stage", "name": name, "state": state, "detail": detail}),
                                    on_vote=lambda v: q.put({"type": "vote", "vote": v.model_dump()}))
                    ledger.record_cycle(rec, mode="paper")
                    q.put({"type": "record", "record": json.loads(rec.model_dump_json())})
                except Exception as exc:
                    q.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                q.put(None)

            threading.Thread(target=work, daemon=True).start()
            yield json.dumps({"type": "convened", "fund": spec.name, "as_of": as_of,
                              "universe": universe, "budget": budget}) + "\n"
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield json.dumps({"type": "keepalive"}) + "\n"
                    continue
                if item is None:
                    break
                yield json.dumps(item) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # jobs -----------------------------------------------------------------
    @app.post("/api/backtest")
    def start_backtest(req: BacktestRequest):
        from consilium.backtest import backtest_fund
        from consilium.core.spec import Fund, normalize_universe
        spec = load_spec(_mandate_path(req.fund))
        guard.apply(spec)
        universe = normalize_universe(req.tickers)
        end = req.end or (_date.today().isoformat() if req.provider != "synthetic" else "2025-06-30")
        start = req.start or (_date.fromisoformat(end) - timedelta(weeks=req.weeks)).isoformat()

        def work(job):
            provider = make_provider(req.provider)
            fund = Fund(spec, model=req.model)
            job.live = {"dates": [], "nav": [], "benchmark_nav": [], "regime": []}
            base = {}

            def on_cycle(i, n, rec):
                job.progress = (i + 1) / n
                job.message = f"{rec.as_of} · NAV ${rec.nav:,.0f} · {rec.verdict.cio.regime}"
                if "b" not in base:
                    base["b"] = rec.benchmark_close or 1.0
                job.live["dates"].append(rec.as_of)
                job.live["nav"].append(round(rec.nav, 2))
                job.live["benchmark_nav"].append(round(spec.capital * (rec.benchmark_close or base["b"]) / base["b"], 2))
                job.live["regime"].append(rec.verdict.cio.regime)

            res = backtest_fund(fund, start, end, provider, universe, model=req.model, on_cycle=on_cycle)
            out = RUNS_DIR / f"backtest-{spec.name}-{res.end}-{job.id}.json"
            out.write_text(res.model_dump_json())
            ledger.record_backtest(res, out)
            return json.loads(res.model_dump_json())

        job = jobs.submit("backtest", req.model_dump(), work)
        return job.public()

    @app.post("/api/run")
    def start_run(req: RunRequest):
        from consilium.committee import Committee
        from consilium.core.spec import Fund, normalize_universe
        from consilium.data.features import PriceBook
        from consilium.execution import SimBroker
        from consilium.pipeline import CycleContext, run_cycle
        spec = load_spec(_mandate_path(req.fund))
        guard.apply(spec)
        universe = normalize_universe(req.tickers)
        as_of = req.date or (_date.today().isoformat() if req.provider != "synthetic" else "2025-06-30")

        def work(job):
            provider = make_provider(req.provider)
            fund = Fund(spec, model=req.model)
            prior = None if req.fresh else ledger.latest_book(spec.name)
            broker = (SimBroker(cash=prior[0], costs=spec.costs, positions=prior[1]) if prior
                      else SimBroker(cash=spec.capital, costs=spec.costs))
            book = PriceBook(provider, (_date.fromisoformat(as_of) - timedelta(days=30)).isoformat(), as_of)
            ctx = CycleContext(fund=fund, book=book, committee=Committee(fund, model=req.model), high_water=spec.capital)
            hist = ledger.cycles(spec.name)
            if hist:
                ctx.high_water = max(c["nav"] for c in hist)
            job.message = "committee sitting…"
            rec = run_cycle(ctx, as_of, broker, universe)
            ledger.record_cycle(rec, mode="paper")
            return json.loads(rec.model_dump_json())

        if SERVERLESS or req.sync:
            # one request, one meeting: no background state to lose between invocations
            class _J:
                message = ""
            result = work(_J())
            return {"id": None, "kind": "run", "status": "done", "progress": 1.0, "message": "", "error": None,
                    "params": req.model_dump(), "result": result}
        job = jobs.submit("run", req.model_dump(), work)
        return job.public()

    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, result: bool = True):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return JSONResponse(job.public(include_result=result))

    # ledger -----------------------------------------------------------------
    @app.get("/api/ledger")
    def ledger_funds():
        return {"funds": ledger.funds(), "backtests": ledger.backtests()}

    @app.get("/api/ledger/{fund}")
    def ledger_fund(fund: str):
        cycles = ledger.cycles(fund)
        latest = ledger.latest_book(fund)
        return {"fund": fund, "cycles": cycles,
                "book": ({"cash": latest[0], "positions": {t: p.model_dump() for t, p in latest[1].items()}, "as_of": latest[2]}
                         if latest else None),
                "backtests": ledger.backtests(fund)}

    @app.delete("/api/ledger/{fund}")
    def ledger_reset(fund: str):
        return {"deleted": ledger.reset(fund)}

    @app.get("/api/cycles/{cycle_id}")
    def cycle(cycle_id: str):
        rec = ledger.cycle(cycle_id)
        if not rec:
            raise HTTPException(404, "no such cycle")
        return json.loads(rec.model_dump_json())

    @app.get("/api/backtests/{bt_id}")
    def backtest_result(bt_id: str):
        for b in ledger.backtests():
            if b["id"] != bt_id or not b["result_path"]:
                continue
            p = Path(b["result_path"])
            if not p.exists() and blob_store() is not None:
                blob_store().download_to(f"runs/{p.name}", p)   # another instance ran it
            if p.exists():
                return JSONResponse(json.loads(p.read_text()))
        raise HTTPException(404, "backtest result not found")

    # dashboard --------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(DASHBOARD)), name="static")

    @app.get("/")
    def index():
        return FileResponse(DASHBOARD / "index.html")

    return app
