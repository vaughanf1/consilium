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
        self.survivorship_free = getattr(inner, "survivorship_free", False)
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
        # Latest-only providers return the same row for every as_of, so one file
        # serves them all. A point-in-time row is only valid for its own date:
        # bucketing it (by month, say) would let a run that asked for the 29th
        # hand the 29th's numbers to a later run asking for the 1st.
        bucket = as_of if self._inner.point_in_time else "latest"
        return self._through(self._key("fundamentals", ticker=ticker, as_of=bucket),
                             lambda: self._inner.fundamentals(ticker, as_of),
                             lambda d: Fundamentals(**d) if d else None)

    def splits(self, ticker: str, end: str) -> list[tuple[str, float]]:
        """Keyed on the price window's end so a split history is always the same
        snapshot as the split-adjusted closes it is paired with."""
        get = getattr(self._inner, "splits", None)
        if get is None:
            return []
        path = self._key("splits", ticker=ticker, end=end)
        if str(path) not in self._mem:
            if path.exists():
                rows = json.loads(path.read_text())
            else:
                rows = [[d, r] for d, r in get(ticker)]
                path.write_text(json.dumps(rows))
            self._mem[str(path)] = [(d, float(r)) for d, r in rows]
        return self._mem[str(path)]

    def profile(self, ticker: str) -> Profile | None:
        return self._through(self._key("profile", ticker=ticker),
                             lambda: self._inner.profile(ticker),
                             lambda d: Profile(**d) if d else None)
