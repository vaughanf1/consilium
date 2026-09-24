"""FundSpec — a fund's mandate as data.

    FUND      = capital slices over STRATEGIES + a committee + master risk
    STRATEGY  = a blend policy over ANALYSTS (a pod)
    ANALYST   = an alpha source -> View

`extra="forbid"` everywhere: a typo in YAML fails at load time, not trade time.
A mandate never names tickers: the universe is a run-time input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from consilium.analysts import ANALYST_REGISTRY, Analyst


class AnalystSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    weight: float = Field(default=1.0, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)


class BlendPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gross_target: float = Field(default=1.0, gt=0)
    market_neutral: bool = False
    confidence_weighted: bool = Field(default=True, description="scale each view by its confidence")
    horizon_aware: bool = Field(default=True, description="down-weight views whose horizon is shorter than the rebalance gap")


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    display_name: str | None = None
    weight: float = Field(default=1.0, gt=0)
    analysts: list[AnalystSpec] = Field(min_length=1)
    blend: BlendPolicy = Field(default_factory=BlendPolicy)

    @property
    def title(self) -> str:
        return self.display_name or self.name.replace("-", " ").title()

    @property
    def analyst_weights(self) -> dict[str, float]:
        return {a.name: a.weight for a in self.analysts}


class BoardMemberSpec(BaseModel):
    """One seat on the investment board. `model` lets each seat run on a
    different LLM — the board is then a disagreement between models, not one
    model arguing with itself."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="key into consilium.committee.board.SEATS")
    model: str | None = Field(default=None, description="e.g. openrouter:openai/gpt-4o; falls back to the run's model")


class CommitteePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    red_team: bool = True
    cio: bool = True
    red_team_top_n: int = Field(default=4, ge=1)
    max_haircut: float = Field(default=0.6, ge=0, le=1)
    board: bool = Field(default=False, description="convene an investment board to vote on the CIO's proposal")
    chair_model: str | None = Field(default=None, description="model for the Red Team and CIO; a fast one keeps the wait before the board short")
    board_members: list[BoardMemberSpec] = Field(default_factory=list)
    use_llm: bool = Field(default=True, description="use the LLM for red team / CIO / board when a key is configured; otherwise rules")

    @field_validator("board_members")
    @classmethod
    def _known_seats(cls, members: list[BoardMemberSpec]) -> list[BoardMemberSpec]:
        from consilium.committee.board import SEATS
        for m in members:
            if m.name not in SEATS:
                raise ValueError(f"unknown board seat {m.name!r}; available: {sorted(SEATS)}")
        names = [m.name for m in members]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate board seats: {sorted(dupes)}")
        return members


class PortfolioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sizing: Literal["vol_scaled", "conviction"] = "vol_scaled"
    reference_vol: float = Field(default=0.20, gt=0, description="a name at this annual vol gets weight = conviction; higher vol -> smaller")
    min_conviction: float = Field(default=0.10, ge=0, le=1)
    rebalance_band: float = Field(default=0.015, ge=0, description="no-trade zone: skip orders whose |Δweight| is below this")
    max_names: int | None = Field(default=None, description="keep only the N highest-|conviction| names")
    min_trade_pct: float = Field(default=0.002, ge=0, description="suppress non-exit orders smaller than this fraction of equity")


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_position_pct: float = Field(default=0.15, gt=0, le=1)
    max_gross_exposure: float = Field(default=1.0, gt=0)
    max_net_exposure: float = Field(default=1.0)
    min_net_exposure: float = Field(default=-1.0)
    max_sector_pct: float | None = Field(default=0.40, gt=0)
    portfolio_vol_target: float | None = Field(default=None, gt=0, description="scale the book down when estimated vol exceeds this")
    drawdown_soft: float | None = Field(default=0.10, gt=0, description="halve exposure beyond this drawdown")
    drawdown_hard: float | None = Field(default=0.20, gt=0, description="go flat beyond this drawdown")


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commission_bps: float = Field(default=1.0, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)
    half_spread_bps: float = Field(default=2.0, ge=0)


class FundSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    strategies: list[StrategySpec] = Field(min_length=1)
    committee: CommitteePolicy = Field(default_factory=CommitteePolicy)
    portfolio: PortfolioPolicy = Field(default_factory=PortfolioPolicy)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    costs: CostModel = Field(default_factory=CostModel)
    capital: float = Field(default=100_000.0, gt=0)
    rebalance: Literal["daily", "weekly", "biweekly", "monthly"] = "weekly"
    benchmark: str = "SPY"

    @field_validator("benchmark")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("strategies")
    @classmethod
    def _unique(cls, s: list[StrategySpec]) -> list[StrategySpec]:
        names = [x.name for x in s]
        dups = {n for n in names if names.count(n) > 1}
        if dups:
            raise ValueError(f"duplicate strategy names: {sorted(dups)}")
        for st in s:
            for a in st.analysts:
                if a.name not in ANALYST_REGISTRY:
                    raise ValueError(f"unknown analyst {a.name!r} in {st.name!r}; available: {sorted(ANALYST_REGISTRY)}")
        return s

    @property
    def rebalance_days(self) -> int:
        return {"daily": 1, "weekly": 5, "biweekly": 10, "monthly": 21}[self.rebalance]

    @property
    def analyst_names(self) -> list[str]:
        out: list[str] = []
        for s in self.strategies:
            for a in s.analysts:
                if a.name not in out:
                    out.append(a.name)
        return out

    @property
    def uses_llm(self) -> bool:
        """True if ANY stage would call a model: analysts, or a staffed board."""
        if any(ANALYST_REGISTRY[n].kind == "llm" for n in self.analyst_names):
            return True
        return bool(self.committee.board and self.committee.board_members)


def normalize_universe(tickers) -> list[str]:
    if isinstance(tickers, str):
        tickers = tickers.replace(",", " ").split()
    out: list[str] = []
    for t in tickers:
        u = t.strip().upper()
        if u and u not in out:
            out.append(u)
    if not out:
        raise ValueError("universe is empty — a run needs at least one ticker")
    return out


def load_spec(path: str | Path) -> FundSpec:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return FundSpec(**data)


def dump_spec(spec: FundSpec) -> str:
    return yaml.safe_dump(spec.model_dump(exclude_none=True), sort_keys=False)


class Fund:
    """The living fund: its spec plus instantiated analysts, built once per run
    so their caches survive across cycles."""

    def __init__(self, spec: FundSpec, analysts: dict[str, list[Analyst]] | None = None, model: str | None = None) -> None:
        self.spec = spec
        self.strategies: list[tuple[StrategySpec, list[Analyst]]] = []
        shared: dict[str, Analyst] = {}
        for st in spec.strategies:
            if analysts is not None:
                self.strategies.append((st, analysts[st.name]))
                continue
            staff = []
            for a in st.analysts:
                key = f"{a.name}:{sorted(a.params.items())}"
                if key not in shared:
                    cls = ANALYST_REGISTRY[a.name]
                    kw = dict(a.params)
                    if cls.kind == "llm" and model:
                        kw.setdefault("model", model)
                    shared[key] = cls(**kw)
                staff.append(shared[key])
            self.strategies.append((st, staff))
