# Consilium — guide for Claude

An AI **investment committee** you can backtest, paper-trade and interrogate.
Analysts form views, a Red Team attacks the consensus, a CIO synthesises, and a
five-seat manager **Council — each seat on a different AI model** — votes on how
much of the book to deploy. Deterministic code then sizes positions, enforces
hard risk limits, charges trading costs and keeps a ledger. **The LLM never
touches a trade.**

**Read `docs/CONTEXT.md` first.** It is the complete product context: every
feature, the full cast, verified numbers that are safe to quote, what is on
screen, and what must not be claimed. It exists so this repo can be understood
without the conversation that produced it — including for writing marketing or
video scripts about it.

Live demo: https://consilium-khaki-three.vercel.app (no login, no API key)

## Working on this repo

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                 # 51 tests, ~2s, no network, no keys
consilium demo         # free offline backtest, ~0.6s
consilium serve        # dashboard on :8765
```

## Where things live

| Path | What |
|---|---|
| `consilium/core/` | pydantic models (the contract), the mandate spec, paths, build stamp |
| `consilium/data/` | `DataProvider` protocol, synthetic + yfinance providers, disk cache, features |
| `consilium/analysts/` | 4 quant analysts, 5 LLM personas, the registry |
| `consilium/committee/` | blend → red team → CIO → **board** (the Council), orchestrator |
| `consilium/portfolio/`, `risk/`, `execution/` | sizing, hard limits, broker with costs |
| `consilium/pipeline/cycle.py` | `run_cycle` — the heartbeat; one code path for backtest/paper/live |
| `consilium/backtest/` | engine, metrics, walk-forward, Monte Carlo |
| `consilium/llm/` | provider clients, prompt cache, model registry |
| `consilium/server/` | FastAPI API, streaming endpoints, spend guard |
| `consilium/dashboard/` | hand-written HTML/CSS/JS — no framework, no build step, no CDN |
| `consilium/mandates/` | the fund configs (YAML) |

## Rules this codebase holds itself to

- **The LLM forms views; code makes trades.** Every model stage has bounded
  authority and a deterministic fallback. Never let a model size a position.
- **Fail loud on data, degrade gracefully on models.** A bad price raises; a
  failed model call abstains and records why.
- **No silent neutrals.** An abstention is excluded from the blend, never
  counted as a neutral opinion.
- **Point-in-time or say so.** Never read data the date wouldn't have had. When
  a provider can't guarantee it, surface the warning (see `yf.py`).
- **Everything is reproducible.** The synthetic provider is seeded; the engine
  is deterministic; the prompt cache makes LLM runs replayable.
- **Money is guarded.** `CONSILIUM_NO_LLM` is enforced inside `make_llm()` so it
  is a real kill switch. `consilium demo` is free by design. Long CLI backtests
  estimate call volume and require `--yes`. The hosted demo has a daily cap.

Tests must keep passing and must not need network or keys.
