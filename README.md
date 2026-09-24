# Consilium

**An AI investment committee you can backtest, paper-trade, and interrogate.**

Most "AI hedge fund" projects poll a handful of LLM personas and average their
votes. Consilium runs a *committee*: analysts form independent views, a **Red
Team** attacks the consensus, a **CIO** synthesizes the final book with regime
awareness — and then deterministic code sizes every position by volatility,
enforces hard risk limits, charges realistic costs, and keeps a persistent
ledger. The LLM never touches a trade.

It runs with **zero API keys** (quant analysts + rules-based chair on free
market data or an offline synthetic market) and gets *more* interesting, not
merely *possible*, when you add one.

> Educational research software. Not investment advice. Paper only.

---

## The Council

The headline feature. Five hedge-fund-manager archetypes vote on how much of the
book to deploy — **each one answered by a different AI model** — and you watch
the votes land one at a time as each model finishes thinking.

```bash
consilium serve            # → http://127.0.0.1:8765, opens on the Council
```

Press **Convene the Council**. The quant desk builds a book in about a second,
the Red Team attacks it, the CIO proposes an exposure, then five models argue
about it. The whole thing takes ~15 seconds with an OpenRouter key, and runs
instantly with no key at all (every seat falls back to a deterministic rule).

| Seat | Philosophy |
|---|---|
| The Macro Allocator | Sizes to the regime and nothing else |
| The Risk Budgeter | Risk budgets and drawdown limits; disagreement means size down |
| The Concentrator | A few positions held through discomfort; resists trimming |
| The Systematizer | Defends the process against human override |
| The Capital Preserver | Asks what the worst case is first; cash is a position |

The vote is settled on the **median**, capped within **±0.35** of the CIO's
proposal, and a position is trimmed only on a **majority** — so one loud seat
can never run the fund, and the models can only ever make the book smaller.

## Quick start

```bash
git clone <this repo> consilium && cd consilium
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

consilium demo            # offline backtest on the synthetic market — no keys, no network
consilium serve           # the dashboard → http://127.0.0.1:8765
```

Real data, still free, still keyless (prices via yfinance):

```bash
consilium backtest systematic-trend --tickers AAPL,MSFT,NVDA,JPM,XOM --start 2023-01-01
consilium run committee-balanced --tickers AAPL,MSFT,JPM,XOM,PG    # one paper cycle, book carried in the ledger
```

Staff the LLM personas by setting one key (or run a local model for free):

```bash
export ANTHROPIC_API_KEY=...                       # or OPENAI_API_KEY / OPENROUTER_API_KEY / DEEPSEEK_API_KEY / XAI_API_KEY / GROQ_API_KEY
consilium backtest committee-balanced --tickers AAPL,MSFT,JPM --model claude-sonnet-5
consilium backtest committee-balanced --tickers AAPL,MSFT,JPM --model ollama:llama3.1   # local, keyless
```

Keys can also live in `~/.consilium/.env` (see `.env.example`).

---

## How a meeting works

Every cycle — one trading week in a backtest, one scheduled run when live — is
the same code path (`consilium/pipeline/cycle.py`):

```
 point-in-time prices ─▶ ANALYSTS form Views        (LLM personas + quant models, per pod)
                       ─▶ PODS blend their views     (confidence- and horizon-weighted)
                       ─▶ RED TEAM attacks the top consensus positions → haircuts
                       ─▶ CIO sets regime + exposure, finalizes convictions (bounded authority)
                       ─▶ SIZING: vol-scaled weights, min-conviction floor, no-trade band
                       ─▶ RISK: drawdown breaker → name cap → sector cap → vol target → net/gross
                       ─▶ EXECUTION: delta orders, spread + slippage + commission
                       ─▶ LEDGER: the whole meeting, serialized, forever
```

### The committee

| Role | What it does | With an LLM | Without |
|---|---|---|---|
| **Analysts** | form a `View`: conviction ∈ [−1, 1], confidence, horizon, thesis, risks | 5 personas reason over fundamentals + a coarse price regime | 4 quant analysts (trend, mean-reversion, low-vol, value/quality) |
| **Pod blend** | one conviction per ticker per pod | — | — |
| **Red Team** | strongest objection + a haircut ∈ [0, max] per consensus name; can only shrink, never flip or add | one call for the whole slate | haircut from analyst disagreement & thin support |
| **CIO** | regime (risk-on/neutral/off), exposure multiplier, final convictions, a memo | may move a name ≤ 0.25 from the haircut-adjusted consensus, never through zero | regime from the benchmark's 200-day trend and vol |
| **The Board** | fund-manager archetypes vote on the CIO's proposal: how much of the book to deploy, and which names to trim | one call per seat — **each seat can run a different model** | every archetype has a deterministic rule version |

The personas are archetypes of investing *style* — The Owner, The Skeptic, The
Catalyst Hunter, The Short Seller, The Macro Tactician — not impersonations of
named people. One of them is bearish by design, because a committee that only
says "buy" is not a committee.

### The board

Where the analysts argue about companies, the **investment board** argues about
portfolio risk. Five manager archetypes — The Macro Allocator, The Risk
Budgeter, The Concentrator, The Systematizer, The Capital Preserver — each vote
an exposure in [0, 1] and may flag names to trim:

- the resolution is the **median** vote, so one extreme seat cannot run the fund;
- it is clamped to within **±0.35** of the CIO's proposal;
- a name is trimmed only on a **majority**, and a trim halves conviction — never flips a sign, never adds a position.

**Each seat can run a different model**, so the board is a genuine disagreement
between models rather than one model arguing with itself. See
`consilium/mandates/multi-model-board.yaml`:

```yaml
committee:
  board: true
  board_members:
    - {name: macro,         model: openrouter:anthropic/claude-sonnet-4.5}
    - {name: risk_budgeter, model: openrouter:openai/gpt-4o}
    - {name: concentrator,  model: openrouter:google/gemini-2.5-pro}
    - {name: systematizer,  model: openrouter:deepseek/deepseek-chat}
    - {name: preserver,     model: openrouter:meta-llama/llama-3.3-70b-instruct}
```

One `OPENROUTER_API_KEY` reaches every vendor; `consilium models` lists the live
catalogue (fetched from OpenRouter, never hardcoded). With no key the seats fall
back to their rules and the board still sits.

Reasoning models are handled explicitly: they spend most of their output budget
on a hidden trace before writing a word, so the client sizes for trace + answer,
recovers an answer left inside the trace, and raises a message that names the
cause when one is truncated — never a silent empty response.

Every LLM call is cached by exact prompt content. Market context is rendered as
*buckets* (trend / vol / drawdown / momentum), so an analyst only re-reasons
when the fundamentals or the regime actually change — not every day a price
ticks. If an LLM call fails, the analyst **abstains** (excluded from the blend)
and the chair falls back to rules. Nothing ever silently becomes "neutral".

### Sizing and risk

- **Vol-scaled sizing**: a +0.6 in a 60%-vol name gets a third of the dollars a
  +0.6 in a 20%-vol name gets. Every position contributes comparable risk.
- **No-trade band** and **minimum notional** keep turnover honest.
- **Risk limits** are hard and logged: drawdown circuit breaker (halve at
  soft, flatten at hard), per-name cap, sector concentration cap, portfolio vol
  target, net-exposure band, gross cap. Exposure removed by a clamp stays in cash.
- **Costs** are modelled on every fill: half-spread, slippage, commission.

### Honest statistics

Return, CAGR, vol, Sharpe (with a risk-free rate), Sortino, Calmar, max
drawdown and its length, beta, Jensen's alpha, information ratio, tracking
error, hit rate, turnover, cost drag — plus three things most backtests skip:

- **Walk-forward**: metrics for the first half (in-sample) and the second half
  (out-of-sample, re-based). If they disagree, distrust the headline.
- **Rolling Sharpe** over the window.
- **Monte Carlo bootstrap**: 500 alternative histories from the realized
  returns → P(loss), median max drawdown, terminal-NAV bands. "How lucky was this?"

### The ledger

`~/.consilium/ledger.sqlite` stores every cycle and backtest. `consilium run`
seeds the broker from the last recorded book, so paper NAV is a track record
that carries between runs — not a reset to the mandate's capital.

---

## The dashboard

`consilium serve` → a web dashboard in a dark graphite theme with one electric-mint
accent (and a clean light variant behind the toggle). Both palettes are validated
for colour-blind separation and contrast. No build step, no CDN: hand-drawn SVG
charts, works offline.

- **Research** — convene a backtest and watch the equity curve draw meeting by
  meeting with the CIO's regime calls shaded behind it; stat tiles, walk-forward,
  drawdown, exposure, rolling Sharpe, the Monte Carlo fan, full statistics.
  Every chart has a table twin.
- **Committee** — a time-scrubbable transcript of every meeting: the CIO memo,
  the Red Team's objections and haircuts, every analyst's stance, conviction,
  confidence, horizon, thesis and risks, and how conviction became weights,
  orders, and fills.
- **Risk & Book** — positions, sector exposure against the cap, every limit that fired.
- **Paper Fund** — run a cycle, see NAV carried across meetings, reset the book.
- **Mandates** — edit YAML with validation; the analyst roster.

---

## Mandates

A mandate is the desk — pods, staff, committee rules, sizing, risk, costs,
capital, cadence. It never names tickers; the universe is a run-time input.

```yaml
name: committee-balanced
strategies:
  - name: quality-owners          # a discretionary pod
    weight: 0.35
    analysts: [{name: owner, weight: 1.5}, {name: catalyst}, {name: value_quality}]
  - name: contrarians
    weight: 0.25
    analysts: [{name: skeptic}, {name: bear, weight: 1.5}]
    blend: {market_neutral: true}  # rank names against each other, dollar-neutral sleeve
  - name: systematic              # a quant pod
    weight: 0.40
    analysts: [{name: trend}, {name: lowvol, weight: 0.6}, {name: macro}]
committee: {red_team: true, cio: true, red_team_top_n: 4, max_haircut: 0.6}
portfolio: {sizing: vol_scaled, reference_vol: 0.20, min_conviction: 0.10, rebalance_band: 0.015, max_names: 12}
risk: {max_position_pct: 0.15, max_gross_exposure: 1.0, min_net_exposure: -0.3,
       max_sector_pct: 0.40, portfolio_vol_target: 0.16, drawdown_soft: 0.10, drawdown_hard: 0.20}
costs: {commission_bps: 1, slippage_bps: 5, half_spread_bps: 2}
capital: 100000
rebalance: weekly
benchmark: SPY
```

Three ship in `consilium/mandates/` and are copied to `~/.consilium/mandates/`
on first run: `committee-balanced` (the flagship), `systematic-trend` (keyless,
fully point-in-time), `long-short-value` (dollar-neutral discretionary).

---

## Data

| Provider | Prices | Fundamentals | Point-in-time | Key |
|---|---|---|---|---|
| `synthetic` | seeded GBM with sector factors and regime shocks | derived from a slowly-drifting "quality" state | yes | none |
| `yfinance` (default) | historical, adjusted | **latest only** | prices yes, fundamentals **no** | none |

yfinance fundamentals have no filing history, so an LLM or value analyst
backtested on them sees today's ratios on past dates. Consilium prints a
warning on such runs and the dashboard shows it. Price-based analysts are
unaffected. Implement `DataProvider` (`consilium/data/protocol.py`) —
three methods — to plug in a point-in-time source.

---

## Layout

```
consilium/
  core/        models (View, Verdict, CycleRecord…), the mandate spec, paths
  data/        DataProvider protocol · synthetic · yfinance · disk cache · features (PriceBook, MarketSnapshot)
  analysts/    Analyst base · quant analysts · LLM personas · registry
  committee/   blend · red team · CIO · the board · the Committee orchestrator
  portfolio/   vol-scaled sizing, no-trade band
  risk/        hard limits with an audit trail
  execution/   delta orders, SimBroker with costs
  pipeline/    run_cycle — the heartbeat
  backtest/    engine, metrics, walk-forward, Monte Carlo
  ledger/      SQLite books
  storage/     BlobStore — durable state for diskless hosts (Vercel Blob)
  llm/         Anthropic + OpenAI-compatible clients (OpenAI, DeepSeek, xAI, Groq, OpenRouter, Ollama), prompt cache, registry
  server/      FastAPI JSON API + background jobs
  dashboard/   index.html · app.css · app.js
  mandates/    bundled YAML mandates
  cli.py
tests/         42 tests: determinism, point-in-time, committee mechanics, risk, costs, ledger, API, LLM path (fake client)
```

## Extending

- **Add an analyst**: subclass `Analyst` (or `LLMAnalyst` with a `persona`),
  return a `View`, register it in `consilium/analysts/__init__.py`. Every
  mandate can staff it immediately.
- **Add a data source**: implement the three-method `DataProvider` protocol.
- **Add a broker**: implement `positions() / cash() / place_order()`; `run_cycle`
  never knows the difference between simulated, paper, and live.

## Deploying (Vercel)

The repo deploys as-is with the Vercel CLI — the FastAPI app becomes a Python
function (`api/index.py`), the dashboard is served statically, and backtests
stream progress over a single request so no background state is needed:

```bash
vercel deploy --prod                              # first deploy creates the project
vercel env add ANTHROPIC_API_KEY production       # optional: staff the LLM personas
vercel deploy --prod                              # redeploy so the function sees the key
```

Durable state on Vercel: create a private Blob store once and the ledger,
prompt cache, saved mandates and backtest receipts survive cold starts —
the paper fund becomes a real track record and LLM runs never re-spend:

```bash
vercel blob create-store consilium-state --access private -y   # sets BLOB_READ_WRITE_TOKEN on the project
vercel deploy --prod
```

Without a store the app still runs; state then lives only while an instance is
warm, and the dashboard says so. Each hosted backtest must finish inside the
5-minute function limit — with LLM personas, keep hosted runs to ~6 tickers
and ~1 year, or run locally for long studies.

## Tests

```bash
pytest
```

## License

MIT.
