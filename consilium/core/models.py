"""Pydantic models — the single source of truth for everything in the pipeline.

Every stage consumes and emits these. A CycleRecord round-trips through JSON,
so the dashboard, the ledger, and the CLI all read one thing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

class Bar(BaseModel):
    date: str            # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Fundamentals(BaseModel):
    """A point-in-time-ish fundamentals row. Nullable everywhere: providers differ."""

    ticker: str
    as_of: str
    market_cap: float | None = None
    pe: float | None = None
    pb: float | None = None
    ps: float | None = None
    ev_ebitda: float | None = None
    roe: float | None = None
    roic: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    fcf_yield: float | None = None
    dividend_yield: float | None = None
    beta: float | None = None
    point_in_time: bool = Field(default=False, description="True only if the provider guarantees the row was public by as_of")


class Profile(BaseModel):
    ticker: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None


# ---------------------------------------------------------------------------
# Committee
# ---------------------------------------------------------------------------

Stance = Literal["bullish", "bearish", "neutral"]


class View(BaseModel):
    """One analyst's view on one ticker at one date. The atom of the committee."""

    analyst: str
    ticker: str
    date: str
    conviction: float = Field(ge=-1.0, le=1.0, description="-1 bearish … +1 bullish")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="how sure the analyst is, independent of direction")
    horizon_days: int = Field(default=60, gt=0, description="how long the thesis needs to play out")
    thesis: str = ""
    risks: list[str] = Field(default_factory=list)
    components: dict[str, float] = Field(default_factory=dict)
    abstained: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def stance(self) -> Stance:
        if self.abstained or abs(self.conviction) < 0.1:
            return "neutral"
        return "bullish" if self.conviction > 0 else "bearish"


class RedTeamNote(BaseModel):
    """The adversary's challenge to one consensus position."""

    ticker: str
    consensus: float                 # blended conviction the desk arrived at
    haircut: float = Field(ge=0.0, le=1.0, description="0 = thesis survives intact, 1 = thesis destroyed")
    strongest_objection: str = ""
    what_would_change_my_mind: str = ""
    source: Literal["llm", "rules"] = "rules"


class CIOMemo(BaseModel):
    """The CIO's synthesis: final convictions, regime stance, and the memo."""

    regime: Literal["risk-on", "neutral", "risk-off"] = "neutral"
    exposure_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)
    final_convictions: dict[str, float] = Field(default_factory=dict)
    memo: str = ""
    overrides: dict[str, str] = Field(default_factory=dict, description="ticker -> why the CIO moved it")
    source: Literal["llm", "rules"] = "rules"


class BoardVote(BaseModel):
    """One board member's vote on the CIO's proposed book.

    The board argues about PORTFOLIO risk, not stock picking — that is the
    analysts' job. A member may push exposure up or down and flag names to
    trim; it can never add a name or flip a sign.
    """

    member: str
    display: str
    stance: Literal["add risk", "hold", "cut risk"]
    exposure_vote: float = Field(ge=0.0, le=1.0)
    trim: list[str] = Field(default_factory=list)
    concern: str = ""
    model: str | None = None          # which LLM cast this vote, when the board is staffed
    source: Literal["llm", "rules"] = "rules"
    fallback_reason: str | None = None   # why a staffed seat voted by rule instead
    dissent: bool = False             # set during aggregation: far from the resolution


class BoardResolution(BaseModel):
    """What the board decided, and how split it was."""

    exposure: float = Field(ge=0.0, le=1.0)
    stance: Literal["add risk", "hold", "cut risk"]
    votes: list[BoardVote] = Field(default_factory=list)
    trimmed: dict[str, int] = Field(default_factory=dict)   # ticker -> how many members flagged it
    trim_factor: float = 1.0
    cio_proposal: float = 1.0
    unanimous: bool = True
    summary: str = ""


class Verdict(BaseModel):
    """The whole committee's output for one cycle, ready for portfolio construction."""

    convictions: dict[str, float]     # ticker -> final conviction after the committee
    red_team: list[RedTeamNote]
    cio: CIOMemo
    board: BoardResolution | None = None
    exposure_multiplier: float = Field(default=1.0, ge=0.0, le=1.0,
                                       description="the one dial sizing reads: the board's resolution when a "
                                                   "board sits, else the CIO's number")


# ---------------------------------------------------------------------------
# Portfolio, risk, execution
# ---------------------------------------------------------------------------

class RiskEvent(BaseModel):
    limit: str
    ticker: str | None = None
    before: float
    after: float
    note: str = ""


class Position(BaseModel):
    ticker: str
    shares: int              # signed; negative = short
    avg_cost: float = 0.0


class Order(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    reference_price: float


class Fill(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float             # actual fill incl. slippage
    commission: float = 0.0
    slippage_cost: float = 0.0


# ---------------------------------------------------------------------------
# The record of one cycle
# ---------------------------------------------------------------------------

class TickerSkip(BaseModel):
    ticker: str
    reason: str


class StrategyRecord(BaseModel):
    name: str
    slice: float
    views: list[View]
    convictions: dict[str, float]     # blended per ticker, pre-committee


class CycleRecord(BaseModel):
    """One tick of the fund end to end. Nothing about a decision lives anywhere else."""

    id: str
    fund: str
    as_of: str
    universe: list[str]
    marks: dict[str, float]
    sectors: dict[str, str] = Field(default_factory=dict)
    skipped: list[TickerSkip] = Field(default_factory=list)
    strategies: list[StrategyRecord]
    verdict: Verdict
    target_weights: dict[str, float]  # post-committee, post-sizing, pre-risk
    risk_events: list[RiskEvent]
    final_weights: dict[str, float]
    equity_before: float
    cash_before: float
    orders: list[Order]
    fills: list[Fill]
    positions: dict[str, int]
    cash: float
    nav: float
    costs: float = 0.0                # commissions + slippage this cycle
    turnover: float = 0.0             # traded notional / equity
    drawdown: float = 0.0             # from high-water mark, as fraction
    benchmark_close: float | None = None
    elapsed_s: float = 0.0
