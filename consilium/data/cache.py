"""CachedProvider — memoize any DataProvider's responses on disk as JSON.

Only successful responses are cached; exceptions propagate (fail-loud kept).
Price requests are cached per (ticker, start, end); the backtester asks for
the whole window once, so reruns are instant and offline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from consilium.core.models import Bar, Fundamentals, Profile
from consilium.core.paths import DATA_CACHE_DIR


class CachedProvider:
    def __init__(self, inner, cache_dir: Path | str = DATA_CACHE_DIR) -> None:
        self._inner = inner
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.name = inner.name
        self.point_in_time = inner.point_in_time
        self._mem: dict[str, object] = {}

    def _key(self, method: str, **params) -> Path:
        raw = json.dumps({"provider": self._inner.name, "method": method, **params}, sort_keys=True)
        return self._dir / f"{method}-{hashlib.sha1(raw.encode()).hexdigest()[:16]}.json"

    def _through(self, path: Path, fetch, load):
        k = str(path)
        if k in self._mem:
            return self._mem[k]
        if path.exists():
            data = json.loads(path.read_text())
            value = load(data)
        else:
            value = fetch()
            dump = ([v.model_dump() for v in value] if isinstance(value, list)
                    else (value.model_dump() if value is not None else None))
            path.write_text(json.dumps(dump))
        self._mem[k] = value
        return value

    def prices(self, ticker: str, start: str, end: str) -> list[Bar]:
        return self._through(self._key("prices", ticker=ticker, start=start, end=end),
                             lambda: self._inner.prices(ticker, start, end),
                             lambda d: [Bar(**b) for b in d])

    def fundamentals(self, ticker: str, as_of: str) -> Fundamentals | None:
        # Latest-only providers return the same row for every as_of; key on the
        # month so a long backtest doesn't write thousands of identical files.
        bucket = as_of[:7] if self._inner.point_in_time else "latest"
        return self._through(self._key("fundamentals", ticker=ticker, as_of=bucket),
                             lambda: self._inner.fundamentals(ticker, as_of),
                             lambda d: Fundamentals(**d) if d else None)

    def profile(self, ticker: str) -> Profile | None:
        return self._through(self._key("profile", ticker=ticker),
                             lambda: self._inner.profile(ticker),
                             lambda d: Profile(**d) if d else None)
