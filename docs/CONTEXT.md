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
| Live demo | https://consilium-neon.vercel.app — no login, no key |
| Repo | https://github.com/vaughanf1/consilium — MIT |
| Stack | Python engine (FastAPI, pydantic, numpy) + hand-written dashboard (no framework, no build step, no chart library, no CDN) |
| Size | ~4,400 lines Python, ~1,200 lines dashboard, ~870 lines tests (measured before Oct 2026; see §17 for the current test count) |
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

Five tabs, each opening with a one-line explanation of what it is for.
Redesigned 23 October 2026 as a soft, simple layout:

- **Look:** warm paper in light mode, quiet charcoal in dark. It follows the
  system setting unless the toggle overrides it. One muted teal accent,
  sentence-case labels.
- **Detail is tucked away:** secondary options sit under "More options", and
  long tables sit behind "Show" disclosures.
- **Build stamp:** the footer shows the environment and commit.

| Tab | What is there | The moment |
|---|---|---|
| **Council** | Fund + stocks + Convene; a five-step progress line; 1 · the desk (analysts, Red Team, CIO), 2 · the vote (five seat cards and the decision), 3 · where the money went; then "Read the full transcript" | Press Convene: the steps light up, then five cards flip from "considering the book…" to their verdicts, one at a time, as each model finishes |
| **Backtest** | Fund + stocks + Run (dates, data and model under More options); seven stat tiles; fund vs index with regime shading; "Does it hold up?" (walk-forward); risk over time; alternative histories (Monte Carlo); the book at the end, with sector bars against the cap and every limit that fired | The curve draws itself meeting by meeting; then the alternative-histories fan |
| **Meetings** | Says whose transcript it is reading (a backtest, one Council meeting, or the paper fund); a slider across every meeting; CIO memo, Red Team, the vote, final weights and trades; analyst views and track records behind Show | Dragging the slider replays the fund's entire history of arguments |
| **Paper fund** | Hold the next meeting; value over time; current book; meetings held, each with a Read link into Meetings | The track record carries from run to run |
| **Funds** | Each fund's rules as editable YAML with validation; the analyst roster behind Show | The roster reads like a cast list |

Every chart has a table view. All charts are hand-drawn SVG — no chart library.

---|---|---|
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

---

## 13. Measured numbers — 23 October 2026: correlation-aware vol targeting (Workstream 1)

**What changed.** The portfolio vol target (risk stage 4) now estimates vol as
√(wᵀ Σ w) with Σ = D·R·D: the per-name 60-day vols on the diagonal, and R an
EWMA correlation of daily log returns (120-day lookback, 20-day halflife) shrunk
toward the identity by λ = 0.3, so no estimated correlation can exceed ±0.7.
With fewer than 40 overlapping return days it falls back to the old independent
estimate and logs a `portfolio_vol_fallback` risk event. When correlation alone
causes a scale-down, the event note says so. The clamp still only ever shrinks
the book. The new mandate field `risk.vol_correlation` (default `true`) can be
set to `false` to restore the old estimate exactly.

**How to reproduce.** From a clean clone: `consilium demo` (synthetic, seeded,
committee-balanced, keyless, 2024-01-05 → 2025-06-30, 79 meetings, default
10-name universe).

| Run | Return | Sharpe | Max DD | Vol | Costs | WF Sharpe IS → OOS | MC P(loss) | Vol-target clamps |
|---|---|---|---|---|---|---|---|---|
| BEFORE (`vol_correlation: false`) | +17.8% | 1.09 | 5.9% | 8.4% | $958 | 1.00 → 1.16 | 6% | 0 |
| AFTER (default) | +17.8% | 1.09 | 5.9% | 8.4% | $958 | 1.00 → 1.16 | 6% | 0 |
| systematic-trend, synthetic, both settings | +12.2% | 0.72 | 5.8% | 8.2% | $986 | 0.86 → 0.69 | 14% | 0 |

**The demo numbers do not move, and that is the honest result.** The two runs
match to the dollar (final NAV $117,847.80). The correlation-aware estimate is
higher, as it should be for a long-only book: 8.2% on average against 7.5%
assuming independence, and 13.7% at the peak against 9.6%. Neither ever reaches
the committee-balanced mandate's 16% target (or systematic-trend's 14%), so the
clamp never fires. The fallback never fired either: every meeting had the full
120 days of overlap. The change matters for books that are concentrated,
correlated or long/short, and for tighter targets. It does not change the
headline demo.

**Do not quote this as "correlation made the fund safer"** on the demo. It
changed no trades there.

**Baseline drift, noted rather than explained.** The BEFORE row above was
measured today, before any Workstream 1 code existed. It differs from the §9
figure (+16.1%, Sharpe 1.04, $921 costs). The §9 number is left as it was
measured. The cause of the difference was not investigated in this session.
Use the figures in this section for anything reproduced from the current code.

Tests: 64 (51 existing, all still passing, plus 13 in `tests/test_correlation.py`).

---

## 14. Measured numbers — 23 October 2026: historical universe membership (Workstream 2)

**What changed.** A backtest or paper run can take a membership file: a CSV of
`ticker,start_date,end_date` spells (blank end = still a member; a ticker may
have several spells). Pass it with `--membership file.csv`, or set
`membership:` in the mandate (a relative path resolves against the YAML).
On each meeting date only names that were members that day are tradeable.
Others are skipped with the reason `not an index member as of <date>`. A held
name that leaves the index stays in the committee and may be sold, but its
target can never grow or flip side. Each time that bites, an `index_exit_only`
risk event is logged, and what it withholds stays in cash. With
`--membership` and no `--tickers`, the universe is every name the file has
ever held.

**The survivorship warning is new, not preserved.** Before this change the
code printed no survivorship warning at all. Now every backtest without a
membership file carries one (`survivorship_warning` in the result, in the CLI
report, and in the dashboard's Research callout). The one exception is the
synthetic provider: it invents its names and never delists any, so there are
no survivors for it to be biased by.

**Shipped without a membership file.** The repo does not include real index
history; supplying one is up to the user.

| Run | Return | Sharpe | Max DD | Final NAV | Survivorship warning |
|---|---|---|---|---|---|
| `consilium demo` (no membership) | +17.8% | 1.09 | 5.9% | $117,847.80 | none (synthetic) |

The demo is unchanged to the dollar from §13: no membership file means no
change in behaviour.

Tests: 73 (64 before + 9 in `tests/test_membership.py`).

---

## 15. Measured numbers — 23 October 2026: analyst track records and reflection (Workstream 3)

**What changed.** Three bounded pieces, all off by default except the scorecard.

1. **Scorekeeper** (`consilium/committee/scorekeeper.py`, deterministic). Each
   directional view (|conviction| ≥ 0.1, not abstained) is a call. A call is
   scored once its horizon has run (horizon in trading days × 365/252, in
   calendar days). The entry is the mark at the meeting that heard it; the exit
   is the mark at the first meeting on or after the due date. A **hit** is a
   sign match; the **contribution** is conviction × realised return. Every
   CycleRecord now carries `track_record`: each analyst's score going into that
   meeting, using nothing that meeting could not have known. It is shown in the
   Committee tab as a scorecard that scrubs with the meeting slider.
2. **Reliability weighting** (`committee.track_record_weighting`, default
   off). In the blend, each analyst's mandate weight is multiplied by
   clip(p̂ / 0.5, 0.5, 1.5), where p̂ = (hits + 10) / (n + 20). In words: the
   prior is 20 coin-flip calls, so an analyst can lose half its voice or gain
   half, never more. The applied factors are logged in the record's
   `reliability` field (empty when off).
3. **Reflection** (`committee.reflection`, default off). Each LLM analyst's
   prompt gets a deterministic summary of its own last 10 scored calls and its
   worst recent miss (ticker, date, stance, thesis, outcome). It is part of the
   prompt, so it is part of the cache key. It grants no authority: the view
   that comes back is blended like any other.

Paper runs (`consilium run`, the dashboard's run and council) replay the
fund's ledger into the scorekeeper. They do this only when the mandate opts in,
because replaying every past meeting is not free.

| Run (`consilium demo`, synthetic, seeded, keyless) | Return | Sharpe | Max DD | Vol | Costs | WF Sharpe IS → OOS | MC P(loss) |
|---|---|---|---|---|---|---|---|
| Defaults (weighting off, reflection off) | +17.8% | 1.09 | 5.9% | 8.4% | $958 | 1.00 → 1.16 | 6% |
| `track_record_weighting: true` | +17.4% | 1.08 | 5.4% | 8.3% | $927 | 1.02 → 1.13 | 6% |

With the defaults the demo is identical to the dollar. Weighting by track
record did **not** improve it: return fell 0.4 points and Sharpe moved
1.09 → 1.08, while drawdown and costs fell slightly. Final factors: Trend
Follower ×1.15 (57.6% hit rate over 602 calls), Value & Quality ×1.10 (55.1%,
443 calls), Low-Vol Defensive ×0.76 (37.5%, 526 calls). The five LLM personas
stayed at ×1.00: keyless, they abstain, so they have no record. This is a
small effect on one seeded synthetic history. **It is not evidence that
learning helps**, and should not be presented as such.

**Reflection was not measured.** Keyless personas abstain, so it changes
nothing here, and measuring it with live models costs real money; no such run
was made. A track-record summary also changes an analyst's prompt whenever a
new call resolves, so with reflection on, expect fewer prompt-cache hits and
more spend on reruns over new dates.

Demo runtime with the scorekeeper on every meeting: 0.7 s.

Tests: 84 (73 before + 11 in `tests/test_track_record.py`, including an exact
regression against NAVs measured before this code existed).

---

## 16. Measured numbers — 23 October 2026: point-in-time fundamentals from SEC EDGAR (Workstream 4)

**What changed.** `--provider edgar` takes prices and profiles from yfinance
and fundamentals from the SEC's XBRL companyfacts API (`consilium/data/edgar.py`).
Every fact is indexed by its **filing date**. On any date a backtest sees only
what had been filed by then: a December year-end filed in February is
invisible in January, and a restatement replaces the original only from the
day it was filed.

- **Flows** (revenue, earnings, cash flow) are the latest fiscal year filed;
  **stocks** (equity, debt, cash) are the latest balance sheet filed. Ratios
  use the close on the date. Flows are annual on purpose: stitching 10-Qs into
  trailing-twelve-month figures would be fresher, but is easy to get subtly
  wrong.
- **Market cap:** filed share counts are converted to yfinance's
  split-adjusted basis, using the split history cached with the same price
  window.
- **Courtesy and caching:** the SEC requires a User-Agent with a contact
  email (`CONSILIUM_SEC_USER_AGENT="Name you@example.com"`). Requests are
  capped at 8 a second (the SEC allows 10). Responses are cached in SQLite
  (`~/.consilium/cache/data/edgar.sqlite`). A cached company answers any date
  up to the day it was fetched and is refetched only for later dates, so a
  backtest over the past replays offline and identically.
- **The point-in-time warning** is now a single rule
  (`backtest.engine.point_in_time_warning`): it is silent only when no
  analyst reads fundamentals, or the run's one provider guarantees them
  point in time. EDGAR does; yfinance does not, so keyless/yfinance runs warn
  exactly as before. The message now ends by pointing at `--provider edgar`.
- **Fixed in passing: a latent lookahead in the data cache.**
  `CachedProvider` cached point-in-time fundamentals per *month*. A run that
  asked for the 29th first would then hand the 29th's numbers to any later
  run asking for the 1st. It now keys them on the exact date. No current
  configuration could hit this, because only latest-only yfinance was ever
  cached.

**Measured:** `consilium demo` is unchanged to the dollar (+17.8%, Sharpe 1.09,
$117,847.80); it does not use EDGAR. **No live EDGAR backtest was run.**
The SEC rejected a User-Agent without a contact email (HTTP 403), and no
email was supplied for this session. All EDGAR behaviour above is verified
against a fixture in the tests, not against live SEC data.

Tests: 93 (84 before + 9 in `tests/test_edgar.py`; no network).

---

## 17. Measured numbers — 23 October 2026: after all four workstreams

**How these were produced.** Code at the merge of PR #4. Run on a fresh, empty
`CONSILIUM_HOME`, so no data or prompt cache was carried over. Keyless
(`CONSILIUM_NO_LLM=1`: the LLM personas abstain, and the Red Team, CIO and
Council run on rules). Universe: the demo's ten names, AAPL MSFT NVDA GOOGL
AMZN JPM UNH XOM PG CAT. 500 Monte Carlo paths. Each mandate was run three
ways:

- **baseline** — every new feature off (`risk.vol_correlation: false`; no
  membership file; weighting and reflection off). This is the code's
  behaviour before the workstreams.
- **defaults** — what a clean clone does today. Only WS1 is active, because
  correlation-aware vol is on by default.
- **+ weighting** — defaults plus `committee.track_record_weighting: true`,
  so WS1 and WS3a/b are active.

**Not active in any run:** WS2 (no membership file ships with the repo), WS3c
reflection (keyless personas abstain, so it can change nothing), and WS4 (no
live EDGAR run; see §16).

| Mandate · data · window | Variant | Return | Index | Sharpe | Max DD | Index DD | Costs | WF Sharpe IS → OOS | MC P(loss) | Final NAV |
|---|---|---|---|---|---|---|---|---|---|---|
| committee-balanced · synthetic · 2024-01-05 → 2025-06-30 (= `consilium demo`) | baseline | +17.8% | +24.4% | 1.09 | 5.9% | 11.3% | $958 | 1.00 → 1.16 | 6% | $117,847.80 |
| | defaults | +17.8% | +24.4% | 1.09 | 5.9% | 11.3% | $958 | 1.00 → 1.16 | 6% | $117,847.80 |
| | + weighting | +17.4% | +24.4% | 1.08 | 5.4% | 11.3% | $927 | 1.02 → 1.13 | 6% | $117,418.49 |
| systematic-trend · yfinance · 2024-06-03 → 2025-06-30 | baseline | −7.0% | +17.5% | −0.76 | 15.3% | 16.9% | $1,111 | 1.21 → −2.77 | 75% | $93,028.65 |
| | defaults | −6.5% | +17.5% | −0.73 | 14.9% | 16.9% | $1,113 | 1.21 → −2.72 | 73% | $93,506.92 |
| | + weighting | −6.5% | +17.5% | −0.73 | 14.9% | 16.9% | $1,113 | 1.21 → −2.72 | 73% | $93,506.92 |
| committee-balanced · yfinance · 2024-01-05 → 2025-06-30 | baseline | +8.8% | +34.5% | 0.39 | 13.8% | 16.9% | $1,157 | 1.90 → −1.31 | 28% | $108,803.75 |
| | defaults | +8.8% | +34.5% | 0.39 | 13.8% | 16.9% | $1,157 | 1.90 → −1.31 | 28% | $108,803.75 |
| | + weighting | +10.3% | +34.5% | 0.48 | 12.8% | 16.9% | $1,174 | 1.97 → −1.18 | 26% | $110,345.75 |

**What the numbers say, and what they don't.**

- **Correlation-aware vol (WS1)** fired once in all nine runs: one meeting of
  systematic-trend on real data, where it trimmed the book. That run finished
  0.5 points less badly (−6.5% vs −7.0%). Everywhere else the vol target was
  never reached, so nothing changed.
- **Track-record weighting (WS3)** helped the real-data committee
  (+10.3% vs +8.8%, Sharpe 0.48 vs 0.39, shallower drawdown). It slightly hurt
  the synthetic demo (+17.4% vs +17.8%). It did nothing at all on
  systematic-trend: every pod there has a single analyst, and the factor
  reweights analysts *within* a pod's blend, so with one analyst it cancels.
  It never moves capital between pods. Final factors, real-data committee:
  Value & Quality ×1.37, Trend ×1.13, Low-Vol ×0.96. Synthetic demo:
  Trend ×1.15, Value & Quality ×1.10, Low-Vol ×0.76.
- One mixed result across two histories is **not evidence that the learning
  loop works.** Say "it can reweight analysts by their record, within hard
  caps", not "it gets better".
- **Every fund still trails its index by a wide margin**, and both real-data
  runs collapse out of sample (walk-forward Sharpe turns negative). That is
  the honest story, unchanged.
- Both yfinance runs print the **survivorship warning** (WS2). The
  committee-balanced yfinance run also prints the **point-in-time warning**,
  because its value analyst reads latest-only fundamentals.

**Reproducing them.**

- **The synthetic rows reproduce exactly** from a clean clone:
  `consilium demo` gives $117,847.80. The demo itself never uses yfinance.
- **The yfinance rows will drift.** yfinance serves prices adjusted for
  dividends and splits as of the day you download them, so every new dividend
  slightly rescales past prices. A clean clone on a later date will get close
  to these numbers, not the same ones. To film something viewers can match to
  the cent, use the demo. Or say on screen that real-data figures depend on
  the download date.
- **The §9 real-data figures are not comparable.** They used an unrecorded
  universe and an earlier download (systematic-trend −2.7% there vs −7.0%
  baseline here; committee-balanced +6.8% vs +8.8%). They are left as
  measured.

Tests: 93 passing, ~3 s, no network, no keys.
