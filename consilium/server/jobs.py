"""Background jobs — backtests and cycles run in a thread; the dashboard polls."""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


class Job:
    def __init__(self, kind: str, params: dict) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.params = params
        self.status = "queued"
        self.progress = 0.0
        self.message = ""
        self.result: Any = None
        self.error: str | None = None
        self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.live: dict = {}          # streaming partial state (nav so far, etc.)

    def public(self, include_result: bool = False) -> dict:
        d = {"id": self.id, "kind": self.kind, "status": self.status, "progress": round(self.progress, 4),
             "message": self.message, "error": self.error, "created": self.created, "params": self.params, "live": self.live}
        if include_result and self.result is not None:
            d["result"] = self.result
        return d


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, params: dict, fn: Callable[[Job], Any]) -> Job:
        job = Job(kind, params)
        with self._lock:
            self._jobs[job.id] = job

        def _run():
            job.status = "running"
            try:
                job.result = fn(job)
                job.status = "done"
                job.progress = 1.0
            except Exception as exc:  # surfaced to the UI, never swallowed
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = traceback.format_exc()[-2000:]

        threading.Thread(target=_run, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [j.public() for j in sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)]
