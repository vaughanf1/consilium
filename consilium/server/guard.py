"""SpendGuard — a daily budget for the public demo.

A hosted demo hands an API key to the internet. This caps what a day of
strangers can spend, and does it by asking the provider what has ACTUALLY been
spent rather than trusting an estimate of our own.

The important choice: when the budget is gone the demo does not break, it
DEGRADES. Every analyst, chair and board seat has a deterministic rule version,
so the council still sits and the page still fills — just without paid models.
A visitor sees a working product; the owner stops paying for it.

State (today's date + the usage reading at the start of today) lives in the
blob store so every serverless instance shares one budget.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx

from consilium.storage import blob_store

logger = logging.getLogger(__name__)

STATE_PATH = "guard/daily.json"
_USAGE_TTL = 60.0          # seconds to cache the provider's usage reading


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SpendGuard:
    """Soft daily cap on provider spend. Thread-safe; safe to construct per app."""

    def __init__(self, daily_usd: float | None = None) -> None:
        self.daily_usd = float(os.environ.get("CONSILIUM_DAILY_USD", daily_usd if daily_usd is not None else 3.0))
        self._lock = threading.Lock()
        self._usage_cache: tuple[float, float] | None = None    # (fetched_at, usage)
        self._state: dict | None = None

    # --- provider usage -------------------------------------------------
    def _usage(self) -> float | None:
        """Total spend on the key, from OpenRouter. None if it cannot be read."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return None
        now = time.time()
        if self._usage_cache and now - self._usage_cache[0] < _USAGE_TTL:
            return self._usage_cache[1]
        try:
            r = httpx.get("https://openrouter.ai/api/v1/key",
                          headers={"Authorization": f"Bearer {key}"}, timeout=6.0)
            r.raise_for_status()
            usage = float(r.json()["data"]["usage"])
        except Exception as exc:
            logger.warning("spend guard could not read usage: %s", exc)
            return None
        self._usage_cache = (now, usage)
        return usage

    # --- durable daily baseline ----------------------------------------
    def _load_state(self) -> dict:
        if self._state is not None:
            return self._state
        store = blob_store()
        state = {}
        if store is not None:
            raw = store.get(STATE_PATH)
            if raw:
                try:
                    import json
                    state = json.loads(raw)
                except ValueError:
                    state = {}
        self._state = state
        return state

    def _save_state(self, state: dict) -> None:
        import json
        self._state = state
        store = blob_store()
        if store is not None:
            store.put_async(STATE_PATH, json.dumps(state).encode(), "application/json")

    # --- the decision ---------------------------------------------------
    def status(self) -> dict:
        """What has been spent today, and whether paid models are still allowed."""
        if self.daily_usd <= 0:
            return {"enabled": False, "allow_llm": True}
        usage = self._usage()
        if usage is None:
            # Cannot measure: allow, rather than break a demo over a flaky check.
            return {"enabled": True, "allow_llm": True, "measured": False, "cap_usd": self.daily_usd}

        with self._lock:
            state = dict(self._load_state())
            if state.get("day") != _today() or "baseline" not in state:
                state = {"day": _today(), "baseline": usage}
                self._save_state(state)
            baseline = float(state["baseline"])

        # A key reset or a new key can read below the baseline; re-anchor rather
        # than report a negative spend.
        if usage < baseline:
            with self._lock:
                state = {"day": _today(), "baseline": usage}
                self._save_state(state)
            baseline = usage

        spent = max(0.0, usage - baseline)
        return {"enabled": True, "measured": True, "allow_llm": spent < self.daily_usd,
                "spent_usd": round(spent, 4), "cap_usd": self.daily_usd,
                "remaining_usd": round(max(0.0, self.daily_usd - spent), 4), "day": _today()}

    def apply(self, spec) -> dict:
        """Check the budget and, if it is gone, switch this request's mandate to
        its rule-based fallback. Returns the status for the caller to surface."""
        st = self.status()
        if st.get("enabled") and not st.get("allow_llm", True):
            # One switch covers every paid stage: analysts, chair and board all
            # consult this flag and fall back to their rule versions.
            spec.committee.use_llm = False
        return st
