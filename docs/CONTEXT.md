# Consilium — full product context

Written so this project can be understood from the repo alone, without the
conversation that produced it. It is the reference for anyone — human or model —
who needs to explain, document, or write a script about what this does.

**Everything here is verified against the running system.** Figures are dated
and reproducible; the engine is deterministic and the demo data is seeded.

---

## 1. The one-liner

An AI hedge fund that runs like a real investment committee: AI analysts pitch,
a Red Team attacks their ideas, five hedge-fund-manager archetypes — each one a
different AI model — vote on the risk, and cold arithmetic sizes every trade.
**The AI is never allowed to touch the money.**

Alternative framings that are all true:

- "Most AI trading bots poll a few chatbots and average the votes. This one holds a meeting."
- "Five different AI models sit on the board. They disagree, and you can read the argument."
- "It is allowed to tell you no — and to explain why."

---

## 2. What it is, and what it is not

It is a **research and paper-trading system**. It backtests strategies over
history, runs a simulated fund that keeps a persistent track record, and
explains every decision it makes.

It is **not** connected to a broker, does not trade real money, and is not
investment advice. The footer of the app says so. Any description of it should
say "paper trading" or "simulated" at least once.

| Fact | Detail |
|---|---|
| Name | Consilium (Latin: a council, a deliberation) |
| Tagline in the app | "an investment committee, in session" |
| Live demo | https://consilium-khaki-three.vercel.app — no login, no key |
| Repo | https://github.com/vaughanf1/consilium — MIT |
| Stack | Python engine (FastAPI, pydantic, numpy) + hand-written dashboard (no framework, no build step, no chart library, no CDN) |
| Size | ~4,400 lines Python, ~1,200 lines dashboard, ~870 lines tests |
| Tests | 51, run in ~2s, no network and no API keys required |
| Data | Free by default: yfinance for real prices, plus a seeded offline synthetic market |
| Models | Anthropic, OpenAI, DeepSeek, xAI, Groq, **OpenRouter** (one key → every vendor), or a free local model via Ollama |
| Runs with no key at all | Yes — quant analysts plus rule-based chair and council |

**It is an original system, not a fork.** It was inspired by the open-source
project `virattt/ai-hedge-fund` but shares no code, and differs in
architecture, naming, interface and almost every design decision (see §7).

---

## 3. How one meeting works

A "meeting" is one cycle: one trading week in a backtest, one scheduled run when
live. Every meeting is the same nine steps. **Only steps 2 and 4–6 involve AI;
everything after step 6 is deterministic code.**

```
prices as of today ─▶ analysts form views ─▶ pods blend them
   ─▶ Red Team attacks the consensus ─▶ CIO proposes exposure
   ─▶ THE COUNCIL votes ─▶ vol-scaled sizing ─▶ hard risk limits
   ─▶ broker fills with costs ─▶ ledger keeps the transcript
```

1. **Marks.** Last close on or before the meeting date. Nothing later is ever visible — no lookahead.
2. **Analysts form views.** Every analyst, on every stock, returns the same shape: a conviction from −1 to +1, a confidence, a time horizon, a written thesis, and the risks.
3. **Pods blend.** Each strategy averages its analysts, weighting by confidence and by whether the view's horizon outlives the rebalance gap.
4. **Red Team.** Attacks the strongest positions with a haircut of 0–60% and the single strongest objection. It can only shrink a position, never flip or add one.
5. **CIO.** Reads the haircut-adjusted book and the market regime, sets an exposure dial from 0 to 1 (risk-on / neutral / risk-off), and writes a memo. It may move a name by at most ±0.25 and can never turn a long into a short.
6. **The Council.** Five manager archetypes vote on how much of the book to deploy. Details in §5.
7. **Sizing.** Dollars scale by volatility: the same conviction in a stock three times as volatile gets a third of the money. A no-trade band stops churn.
8. **Risk limits.** Drawdown circuit breaker (halve past −10%, flat past −20%), per-name cap, sector cap, portfolio volatility target, gross and net limits. Each can only shrink the book, and every clamp is logged.
9. **Ledger.** The whole meeting — every view, thesis, haircut, memo, clamp, order, fill and NAV — is saved as one record, and that record is what the dashboard replays.

Backtest, paper and (future) live are **the same function** with a different
clock and broker, so what you test is what would trade.

---

## 4. The analysts

Nine analysts. Four are pure maths; five are LLM personas. They all return the
same `View` object, so they blend together and are individually backtestable.

**The personas are archetypes of investing *style*, not impersonations of real
people.** That is deliberate and worth stating in any description.

| Analyst | Kind | Horizon | Cares about |
|---|---|---|---|
| Trend Follower | quant | 63d | 12-month momentum, 50/200-day structure |
| Mean Reversion | quant | 10d | 5-day moves that are extreme vs recent volatility, RSI |
| Low-Vol Defensive | quant | 42d | Current volatility vs a stock's own history; shallow drawdowns |
| Value & Quality | quant | 126d | Earnings and FCF yield, EV/EBITDA, ROE, margins, leverage, growth |
| The Owner | LLM | 365d | Durable returns on capital, pricing power, clean balance sheets |
| The Skeptic | LLM | 180d | Margin of safety; distrusts growth narratives |
| The Catalyst Hunter | LLM | 90d | Acceleration and inflections before the multiple re-rates |
| The Short Seller | LLM | 90d | Margin compression, rising leverage, rich multiples on slowing growth |
| The Macro Tactician | LLM | 45d | Trend, volatility and drawdown regime over company detail |

One is bearish by design. A committee that only says "buy" is not a committee.

**Abstention is not neutrality.** If a model call fails or the data is
insufficient, the analyst abstains and is excluded from the blend entirely —
never counted as a neutral vote.

---

## 5. The Council (the headline feature)

Where the analysts argue about *companies*, the Council argues about
*portfolio risk*: how much of the fund to deploy, and which positions are too
big to be comfortable.

**Each seat is answered by a different AI model**, so the disagreement is
between models rather than one model talking to itself.

| Seat | Philosophy | Model on the live demo |
|---|---|---|
| The Macro Allocator | Sizes to the regime and nothing else; impatient with caution in an uptrend | Llama 4 Maverick |
| The Risk Budgeter | Risk budgets and drawdown limits; analyst disagreement means size down | Claude Sonnet 4.5 |
| The Concentrator | A few positions held through discomfort; resists trimming winners | GPT-6 Luna |
| The Systematizer | Defends the process against human override; votes the model's number | Mistral Medium 3.5 |
| The Capital Preserver | Asks what the worst case is first; cash is a position | Gemini 3.7 Flash |

**How the vote is settled — this is what makes it a mechanism rather than theatre:**

- The resolution is the **median** vote, so one extreme seat cannot run the fund.
- It is capped within **±0.35** of what the CIO proposed.
- A position is trimmed only if **half the council** flags it, and a trim halves it — it can never flip a bet or open a new one.
- Dissenters are recorded by name and outlined in amber on screen.
- Every seat has a **deterministic rule version**, so with no API key the council still sits, still disagrees, and still produces a real spread (measured: 50%–100% across the five seats, in 0.05 seconds).

**Votes arrive in the order the models finish thinking**, which is what makes it
watchable: the dashboard streams each vote as it lands.

A real vote, captured 24 Sep 2026 with the fund 9% below its high-water mark:
Grok voted to deploy 100% ("a 9% drawdown is noise"), Claude voted 75% ("this is
exactly when discipline matters"), GPT voted 95%, DeepSeek voted the process
number, and Gemini dissented at 35% ("deploying 95% is reckless").

---

## 6. Honest statistics

Standard metrics — return, CAGR, volatility, Sharpe, Sortino, Calmar, max
drawdown, beta, Jensen's alpha, information ratio, hit rate, turnover, cost drag
— plus three things most backtests skip, which exist specifically to make the
fund look worse when it deserves to:

- **Walk-forward.** Metrics for the first half (in-sample) and second half (out-of-sample, re-based). If they disagree, distrust the headline.
- **Monte Carlo.** 500 bootstrapped alternative histories from the realised returns → probability of loss, median max drawdown, terminal-NAV bands. Literally answers "how lucky was this?"
- **Point-in-time warnings.** yfinance fundamentals are latest-only, so a value or LLM analyst backtested on them would see today's ratios on past dates. The app says so, in the CLI and the dashboard, rather than quietly producing a flattering number.

---

## 7. Why it is different

| Area | Typical AI-trading demo | Consilium |
|---|---|---|
| Decision | Independent votes, averaged | Analysts → Red Team attack → CIO → a council of five models, each with bounded authority |
| Data | Paid API key required | Free by default; runs with zero keys |
| Position size | Same dollars for the same conviction | Scaled by volatility so positions carry comparable risk |
| Risk | Position cap, maybe gross cap | Drawdown breaker, sector cap, vol target, net and gross bands — all logged |
| Costs | None; fills at the close | Spread, slippage and commission on every fill |
| Statistics | Return and Sharpe | Full set, plus walk-forward and Monte Carlo |
| Track record | Resets every run | Persistent ledger; each paper run carries the previous book forward |
| Interface | Terminal | Web dashboard with a scrubbable transcript of every meeting |
| Failure | Silent or fatal | Degrades: models abstain with a recorded reason, rules take over, the page still fills |

---

## 8. What is on screen

Five tabs. Dark graphite theme with one electric-mint accent; both light and
dark palettes are validated for colour-blind separation and contrast. A build
stamp in the footer shows the environment and commit.

| Tab | What is there | The moment |
|---|---|---|
| **Council** | Convene button, a live pipeline strip, the desk's book and Red Team objections, five seat cards, the resolution | Press Convene: stages light up, then five cards flip from "considering the book…" to their verdicts, one at a time, as each model finishes |
| **Research** | Backtest filters, seven stat tiles, equity curve vs benchmark with regime shading, walk-forward, drawdown/exposure/rolling-Sharpe, Monte Carlo fan, full statistics | The curve draws itself meeting by meeting; then the Monte Carlo fan |
| **Committee** | A slider across every meeting; CIO memo, Red Team haircuts, every analyst's stance/conviction/thesis, conviction → weights → orders, risk events | Dragging the slider replays the fund's entire history of arguments |
| **Risk & Book** | NAV, gross/net, drawdown vs limits, positions, sector bars against the cap, every limit that fired | Sector bars hitting the cap line |
| **Mandates** | The fund configs as editable YAML with validation, and the analyst roster | The roster reads like a cast list |

Every chart has a table view. All charts are hand-drawn SVG — no chart library.

---

## 9. Verified numbers (safe to quote)

Measured on the running system, 22–24 September 2026. Reproducible: the engine
is deterministic and the demo data is seeded.

| Claim | Number |
|---|---|
| Demo backtest (committee-balanced, synthetic, Jan 2024–Jun 2025, keyless) | +16.1% return, Sharpe 1.04, max drawdown 6.1%, 79 meetings |
| …its Monte Carlo | 500 paths, 7% chance of ending below starting capital |
| …its walk-forward | Sharpe 1.01 in-sample → 1.12 out-of-sample |
| …its modelled costs | $921, 0.92% of capital |
| Real data (systematic-trend, yfinance, Jun 2024–Jun 2025) | Fund −2.7% vs S&P 500 +17.5%; fund max drawdown 11% vs index 16.9% |
| Real data (committee-balanced, Jan 2024–Jun 2025) | Fund +6.8% vs S&P +34.5%; CIO called risk-off through the spring 2025 sell-off |
| Council, five live models | ~12–22 seconds end to end, votes landing 2–5s apart |
| Council, keyless rules | 0.05 seconds, five seats, 50%–100% spread |
| Cost of one council | about 1.7 cents |
| Free offline demo | 0.6 seconds |
| Code | ~4,400 lines Python, ~1,200 dashboard, 51 tests |

**The real-data numbers are unflattering and should stay that way.** The keyless
committee trailed the index while cutting the crash drawdown roughly in half.
That is the honest story and it is more interesting than a fake win: *it lost to
the market, and it can show you exactly which analyst was wrong and why.*

---

## 10. What must not be claimed

- Do **not** say it makes money, beats the market, or predicts anything.
- Do **not** say it trades real money or connects to a broker. Paper only.
- Do **not** name real investors as the personas — they are style archetypes.
- Do **not** present synthetic data as real. The app labels it; keep the label.
- Do **not** say the LLM decides trades. It forms views; deterministic code sizes and executes.
- Backtests on free fundamentals are not point-in-time. Say so, as the app does.
- A UK-marketed system that generates trade ideas may be a regulated financial promotion. Anyone selling around this should take advice.

---

## 11. Engineering decisions worth knowing

These are the details that make the product credible, and several are good
material in their own right.

- **Reasoning models return nothing if you budget wrong.** They spend most of their output allowance on a hidden trace before writing a word. The client sizes for trace + answer, recovers an answer left inside a trace, and raises a message naming the cause instead of failing silently.
- **A gateway can answer with `choices: null`.** Indexing straight into that produced a cryptic `NoneType` error three frames away; it now names the model and the upstream reason, and the affected council seat records why it fell back.
- **Model choice was measured, not guessed.** Twelve candidate models were benchmarked on the real board prompt for latency and schema compliance. Picking the five fastest across five vendors took the council from 107 seconds to 14.
- **The prompt cache makes runs replayable and cheap.** Identical prompts are never paid for twice — a repeated council returns in ~2s at zero cost. Market context is rendered as coarse buckets so prompts only change when the regime actually changes.
- **`CONSILIUM_NO_LLM` is enforced inside `make_llm()`**, the single place every stage passes through, so it is a genuine kill switch rather than a suggestion.
- **Spend is guarded three ways**: `consilium demo` is free by design, long CLI backtests estimate call volume and require `--yes`, and the hosted demo has a daily cap measured against the provider's real billed usage. Past the cap it **degrades to rules rather than breaking** — visitors still see a working council.
- **State survives on a serverless host.** The ledger writes append-only per-cycle objects so several instances can write at once without clobbering each other; an earlier whole-file approach silently lost meetings under concurrency.

---

## 12. The story, if one is needed

Built in a few days as an original reimagining of an open-source AI hedge fund
project. The guiding question was not "can an LLM pick stocks" but **"what would
it take to trust one?"** — which led to adversarial review, bounded authority at
every model stage, deterministic execution, honest statistics designed to
embarrass the fund, and a transcript of every argument the machine ever had.

The most quotable property: **it is allowed to say no.** If the evidence does
not survive the Red Team, the CIO and the Council, the fund does not trade — and
you can read exactly who objected, in their own words.
