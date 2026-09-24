"""The analyst roster. Add a class, register it here, and every mandate can staff it."""

from consilium.analysts.base import Analyst, AnalysisContext  # noqa: F401
from consilium.analysts.llm import (  # noqa: F401
    LLMAnalyst, OwnerAnalyst, SkepticAnalyst, CatalystAnalyst, BearAnalyst, MacroAnalyst,
)
from consilium.analysts.quant import (  # noqa: F401
    TrendAnalyst, MeanReversionAnalyst, LowVolQualityAnalyst, ValueQualityAnalyst,
)

ANALYST_REGISTRY: dict[str, type[Analyst]] = {
    cls.name: cls for cls in (
        TrendAnalyst, MeanReversionAnalyst, LowVolQualityAnalyst, ValueQualityAnalyst,
        OwnerAnalyst, SkepticAnalyst, CatalystAnalyst, BearAnalyst, MacroAnalyst,
    )
}


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    for end in (". ", "! ", "? "):
        if end in text:
            return text[: text.index(end) + 1]
    return text


def describe_analysts() -> list[dict]:
    return [{"name": c.name, "display": c.display, "kind": c.kind, "horizon_days": c.horizon_days,
             "needs_fundamentals": c.needs_fundamentals,
             "summary": _first_sentence(c.__doc__ or c.persona or "")}
            for c in ANALYST_REGISTRY.values()]
