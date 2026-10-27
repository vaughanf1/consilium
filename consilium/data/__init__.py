import os

from consilium.data.protocol import DataProvider  # noqa: F401
from consilium.data.synthetic import SyntheticProvider  # noqa: F401
from consilium.data.cache import CachedProvider  # noqa: F401

SEC_USER_AGENT_ENV = "CONSILIUM_SEC_USER_AGENT"


def sec_configured() -> bool:
    """True when the SEC contact string is set, so EDGAR can be used."""
    return bool(os.environ.get(SEC_USER_AGENT_ENV, "").strip())


def available_providers() -> list[str]:
    return ["synthetic", "yfinance"] + (["edgar"] if sec_configured() else [])


def make_provider(name: str = "auto"):
    """Build a data provider by name: 'synthetic' (offline), 'yfinance' (free, live),
    'edgar' (yfinance prices + as-filed SEC fundamentals; needs CONSILIUM_SEC_USER_AGENT),
    or 'auto': edgar when the SEC contact is configured, otherwise yfinance. Either way the
    run is point in time — on yfinance, fundamentals are withheld on past dates rather than
    read from the future."""
    name = (name or "auto").lower()
    if name == "synthetic":
        return SyntheticProvider()
    if name == "auto" and sec_configured():
        name = "edgar"
    if name in ("edgar", "yfinance+edgar"):
        from consilium.data.edgar import EdgarProvider
        from consilium.data.yf import YFinanceProvider
        return EdgarProvider(CachedProvider(YFinanceProvider()))
    if name in ("yfinance", "yf", "auto"):
        from consilium.data.yf import YFinanceProvider
        return CachedProvider(YFinanceProvider())
    raise ValueError(f"unknown data provider {name!r}; use synthetic, yfinance or edgar")
