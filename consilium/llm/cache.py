"""PromptCache — every LLM decision persisted, keyed by exact prompt content.

An unchanged prompt never pays for a second call, and every response (parsed
or not) is on disk for audit: the committee is replayable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from consilium.core.paths import PROMPT_CACHE_DIR
from consilium.storage import blob_store


def prompt_key(role: str, model: str, system: str, user: str) -> str:
    return hashlib.sha256(f"{role}\x00{model}\x00{system}\x00{user}".encode()).hexdigest()[:32]


class PromptCache:
    def __init__(self, cache_dir: Path | str = PROMPT_CACHE_DIR) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self._blob = blob_store()

    def get(self, key: str) -> dict | None:
        p = self._dir / f"{key}.json"
        if not p.exists() and self._blob is not None:
            self._blob.download_to(f"prompts/{key}.json", p)   # read-through from durable storage
        if p.exists():
            try:
                self.hits += 1
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        self.misses += 1
        return None

    def put(self, key: str, record: dict) -> None:
        payload = json.dumps(record, indent=1, default=str)
        (self._dir / f"{key}.json").write_text(payload)
        if self._blob is not None:
            self._blob.put_async(f"prompts/{key}.json", payload.encode(), "application/json")
