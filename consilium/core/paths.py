"""Where user data lives: ~/.consilium/ (override with CONSILIUM_HOME)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("CONSILIUM_SERVERLESS"))
# On a serverless host only /tmp is writable and nothing persists between cold
# starts: mandates, caches and the ledger live for the life of the instance.
_DEFAULT_HOME = Path("/tmp/consilium") if SERVERLESS else Path.home() / ".consilium"
HOME = Path(os.environ.get("CONSILIUM_HOME", _DEFAULT_HOME))
MANDATES_DIR = HOME / "mandates"
CACHE_DIR = HOME / "cache"
DATA_CACHE_DIR = CACHE_DIR / "data"
PROMPT_CACHE_DIR = CACHE_DIR / "prompts"
RUNS_DIR = HOME / "runs"
LEDGER_PATH = HOME / "ledger.sqlite"
ENV_PATH = HOME / ".env"

BUNDLED_MANDATES = Path(__file__).resolve().parent.parent / "mandates"


def ensure_home() -> Path:
    """Create the user directory tree on first use and seed the example mandates."""
    for d in (MANDATES_DIR, DATA_CACHE_DIR, PROMPT_CACHE_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for src in BUNDLED_MANDATES.glob("*.yaml"):
        dst = MANDATES_DIR / src.name
        if not dst.exists():
            shutil.copy(src, dst)
    if SERVERLESS:
        from consilium.storage import blob_store  # local import: storage depends on paths
        store = blob_store()
        if store is not None:
            store.restore_dir("mandates/", MANDATES_DIR)
    return HOME


def load_env_file() -> None:
    """Load KEY=VALUE lines from ~/.consilium/.env without overriding the shell."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
