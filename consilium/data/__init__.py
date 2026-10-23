from consilium.data.protocol import DataProvider  # noqa: F401
from consilium.data.synthetic import SyntheticProvider  # noqa: F401
from consilium.data.cache import CachedProvider  # noqa: F401


def make_provider(name: str = "auto"):
    """Build a data provider by name: 'synthetic' (offline), 'yfinance' (free, live), 'auto',
    or 'edgar' (yfinance prices + point-in-time SEC fundamentals; needs CONSILIUM_SEC_USER_AGENT)."""
    name = (name or "auto").lower()
    if name == "synthetic":
        return SyntheticProvider()
    if name in ("edgar", "yfinance+edgar"):
        from consilium.data.edgar import EdgarProvider
        from consilium.data.yf import YFinanceProvider
        return EdgarProvider(CachedProvider(YFinanceProvider()))
    if name in ("yfinance", "yf", "auto"):
        from consilium.data.yf import YFinanceProvider
        return CachedProvider(YFinanceProvider())
    raise ValueError(f"unknown data provider {name!r}; use synthetic, yfinance or edgar")
