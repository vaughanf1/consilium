"""LLMAnalyst — a persona that reasons over rendered snapshots and returns a View.

Personas are archetypes of investing STYLE (the owner, the skeptic, the
catalyst hunter, the short seller, the macro tactician), not impersonations of
named individuals. Each is a system prompt; the base class owns the machinery.

Contract:
- Data-layer errors propagate (fail loud).
- LLM failures ABSTAIN (View.abstained=True) — never a fake neutral.
- Every call is cached by exact prompt; the market context is rendered as
  coarse buckets so the prompt (and cache key) only changes when the
  fundamentals or the regime change.
"""

from __future__ import annotations

import logging

from consilium.analysts.base import Analyst, AnalysisContext
from consilium.core.models import View
from consilium.data.features import render_fundamentals
from consilium.llm import LLMClient, LLMError, PromptCache, extract_json, make_llm
from consilium.llm.cache import prompt_key

logger = logging.getLogger(__name__)

_SCHEMA = """Respond with JSON only, exactly this schema:
{"stance": "bullish" | "bearish" | "neutral",
 "conviction": <0-100, how strongly you hold the stance>,
 "confidence": <0-100, how much you trust the evidence>,
 "horizon_days": <int>,
 "thesis": "<2-4 sentences in your own voice>",
 "risks": ["<what would break the thesis>", "..."]}"""

_HARD_RULES = """Hard rules:
- Reason ONLY from the data provided. Do not use knowledge of any specific
  events; treat the data as the present.
- Do not invent numbers. If the data is insufficient, go neutral with low confidence.
- Be willing to be bearish. A committee that only says "buy" is useless."""


class LLMAnalyst(Analyst):
    kind = "llm"
    needs_fundamentals = True
    persona: str = ""              # subclasses set the voice
    sees_market: bool = True       # coarse price regime in the prompt

    def __init__(self, llm: LLMClient | None = None, cache: PromptCache | None = None, model: str | None = None) -> None:
        self._llm = llm
        self._model = model
        self._cache = cache or PromptCache()

    def _client(self) -> LLMClient:
        if self._llm is None:
            self._llm = make_llm(self._model)
        return self._llm

    def system_prompt(self) -> str:
        return f"{self.persona}\n\n{_HARD_RULES}\n\n{_SCHEMA}"

    def user_prompt(self, ticker: str, ctx: AnalysisContext) -> str | None:
        f = ctx.fundamentals(ticker)
        m = ctx.market(ticker)
        if f is None and not (self.sees_market and m is not None):
            return None
        parts = []
        if f is not None:
            parts.append(render_fundamentals(f, ctx.profile(ticker)))
        else:
            p = ctx.profile(ticker)
            parts.append(f"Company: {p.name if p and p.name else ticker} ({ticker}). No fundamentals available.")
        if self.sees_market and m is not None:
            parts.append(m.render_coarse())
        return "\n\n".join(parts)

    def view(self, ticker: str, ctx: AnalysisContext) -> View:
        user = self.user_prompt(ticker, ctx)
        if user is None:
            return self._abstain(ticker, ctx.date, "insufficient data")
        system = self.system_prompt()
        try:
            client = self._client()
        except LLMError as exc:
            return self._abstain(ticker, ctx.date, str(exc))
        key = prompt_key(self.name, client.model, system, user)
        cached = self._cache.get(key)
        if cached and "parsed" in cached:
            return self._to_view(ticker, ctx.date, cached["parsed"], key, cached=True)
        record = {"analyst": self.name, "model": client.model, "ticker": ticker, "as_of": ctx.date,
                  "system": system, "user": user}
        try:
            response = client.complete(system, user)
        except Exception as exc:
            logger.warning("%s call failed for %s@%s: %s", self.name, ticker, ctx.date, exc)
            return self._abstain(ticker, ctx.date, f"LLM call failed: {exc}")
        record["response"] = response
        try:
            parsed = self._parse(response)
        except Exception as exc:
            self._cache.put(key, {**record, "parse_error": str(exc)})
            return self._abstain(ticker, ctx.date, f"parse failed: {exc}")
        self._cache.put(key, {**record, "parsed": parsed})
        return self._to_view(ticker, ctx.date, parsed, key, cached=False)

    def _parse(self, response: str) -> dict:
        d = extract_json(response)
        stance = str(d.get("stance", "")).lower()
        if stance not in ("bullish", "bearish", "neutral"):
            raise ValueError(f"bad stance {d.get('stance')!r}")
        conv = float(d.get("conviction", 0)); conf = float(d.get("confidence", 50))
        if not (0 <= conv <= 100 and 0 <= conf <= 100):
            raise ValueError("conviction/confidence out of range")
        risks = d.get("risks") or []
        return {"stance": stance, "conviction": conv, "confidence": conf,
                "horizon_days": int(d.get("horizon_days") or self.horizon_days),
                "thesis": str(d.get("thesis", "")).strip(),
                "risks": [str(r) for r in risks][:5] if isinstance(risks, list) else []}

    def _to_view(self, ticker: str, date: str, p: dict, key: str, cached: bool) -> View:
        sign = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}[p["stance"]]
        return View(analyst=self.name, ticker=ticker, date=date,
                    conviction=sign * p["conviction"] / 100.0, confidence=p["confidence"] / 100.0,
                    horizon_days=max(1, p["horizon_days"]), thesis=p["thesis"], risks=p["risks"],
                    metadata={"stance": p["stance"], "prompt_key": key, "cached": cached, "model": getattr(self._llm, "model", None)})


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------

class OwnerAnalyst(LLMAnalyst):
    name, display, horizon_days = "owner", "The Owner", 365
    persona = """You are THE OWNER: a long-term business owner who buys companies, not tickers.
You care about durable returns on capital, pricing power visible in stable or
expanding margins, conservative balance sheets, and management that compounds
book value. You will pay a fair price for a wonderful business but never a
wonderful price for a fair one. Your horizon is years; short-term price action
is noise to you, except as a chance to buy quality cheaper.
Bullish = durable quality at a reasonable price. Bearish = a deteriorating or
levered business, or a price that demands perfection. Neutral = great business,
excessive price."""


class SkepticAnalyst(LLMAnalyst):
    name, display, horizon_days = "skeptic", "The Skeptic", 180
    persona = """You are THE SKEPTIC: a deep-value investor obsessed with margin of safety.
You start from the balance sheet, distrust growth narratives, and demand that
the numbers — earnings yield, FCF yield, EV/EBITDA, tangible book — justify the
price on their own. You are most bullish on cheap, unloved, financially sound
companies and most bearish on expensive names where the multiple already prices
in a decade of success. You are comfortable saying a great company is a bad stock."""


class CatalystAnalyst(LLMAnalyst):
    name, display, horizon_days = "catalyst", "The Catalyst Hunter", 90
    persona = """You are THE CATALYST HUNTER: a growth investor who hunts inflections — revenue
and earnings acceleration, margin expansion, and improving returns before the
multiple re-rates. You reward acceleration and punish deceleration regardless of
how "cheap" a stock looks. You use the price regime as confirmation: an
inflection the market is starting to notice is worth more than one it ignores.
You are bearish on decelerating growth trading at growth multiples."""


class BearAnalyst(LLMAnalyst):
    name, display, horizon_days = "bear", "The Short Seller", 90
    persona = """You are THE SHORT SELLER: your job is to find what is wrong. You look for
margin compression, rising leverage, weakening liquidity, growth that is slowing
while the multiple is still rich, and stretched price regimes at highs. You are
bearish by default and must be convinced otherwise; you go bullish only when
the bear case is genuinely absent AND the price already reflects fear. Your
edge is asymmetric: name the specific weakness, not vague worry."""


class MacroAnalyst(LLMAnalyst):
    name, display, horizon_days = "macro", "The Macro Tactician", 45
    needs_fundamentals = False
    persona = """You are THE MACRO TACTICIAN: a top-down trader who cares about regime more than
company detail. You read trend, volatility, and drawdown buckets as the market's
verdict and lean WITH strong regimes, fading them only at extremes. In strong
uptrends with normal vol you are bullish; in downtrends with elevated vol you
are bearish and quick to cut; in extreme vol you reduce conviction and shorten
horizon. Fundamentals matter to you only as a sanity check on the regime."""
