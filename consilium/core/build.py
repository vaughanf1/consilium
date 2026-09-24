"""Which build is this? — so "is the deployed version the current one" is a
question you can answer by looking, not by hashing assets.

On Vercel the git metadata is injected as environment variables. Locally we ask
git. Neither is guaranteed, so every field is optional.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def build_info() -> dict:
    sha = os.environ.get("VERCEL_GIT_COMMIT_SHA")
    message = os.environ.get("VERCEL_GIT_COMMIT_MESSAGE")
    where = os.environ.get("VERCEL_ENV")
    if not sha:
        try:
            root = Path(__file__).resolve().parent.parent.parent
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                 text=True, timeout=5).stdout.strip() or None
            message = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=root, capture_output=True,
                                     text=True, timeout=5).stdout.strip() or None
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                                   text=True, timeout=5).stdout.strip()
            where = "local" + ("+dirty" if dirty else "")
        except Exception:
            pass
    return {"commit": (sha or "")[:7] or None, "message": (message or "").split("\n")[0][:80] or None,
            "env": where or "local", "deployment": os.environ.get("VERCEL_DEPLOYMENT_ID")}
