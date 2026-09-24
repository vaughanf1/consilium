"""BlobStore — durable state for hosts with no disk (Vercel Blob, private access).

Enabled automatically when BLOB_READ_WRITE_TOKEN is set. Everything else in
Consilium keeps writing to the local home directory; this layer mirrors the
few things worth keeping across cold starts:

    ledger.sqlite         the paper fund's books and backtest index
    prompts/<key>.json    the LLM prompt cache (so hosted runs don't re-spend)
    runs/<name>.json      backtest receipts the dashboard reloads on open
    mandates/<name>.yaml  mandates saved from the dashboard

Failures here are logged and swallowed: the app must keep working when the
store is unreachable — it just forgets on the next cold start.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

API = "https://blob.vercel-storage.com"
API_VERSION = "12"


class BlobStore:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, timeout: float = 20.0) -> None:
        self.token = token
        parts = token.split("_")
        self.store_id = parts[3] if len(parts) > 3 else ""
        self.base = f"https://{self.store_id}.private.blob.vercel-storage.com/"
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._auth = {"authorization": f"Bearer {token}"}

    # primitives ----------------------------------------------------------
    def get(self, path: str) -> bytes | None:
        try:
            r = self._client.get(self.base + quote(path) + "?cache=0", headers=self._auth)
        except httpx.HTTPError as exc:
            logger.warning("blob get %s failed: %s", path, exc)
            return None
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            logger.warning("blob get %s -> %s", path, r.status_code)
            return None
        return r.content

    def get_if_changed(self, path: str, etag: str | None) -> tuple[bytes | None, str | None, bool]:
        """Conditional read: (data, etag, changed). Unchanged -> (None, etag, False);
        missing or unreachable -> (None, None, False)."""
        headers = dict(self._auth)
        if etag:
            headers["If-None-Match"] = etag
        try:
            r = self._client.get(self.base + quote(path) + "?cache=0", headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("blob get %s failed: %s", path, exc)
            return None, etag, False
        if r.status_code == 304:
            return None, etag, False
        if r.status_code == 200:
            return r.content, r.headers.get("etag"), True
        return None, None, False

    def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        headers = {**self._auth, "x-api-version": API_VERSION, "x-vercel-blob-access": "private",
                   "x-add-random-suffix": "0", "x-allow-overwrite": "1", "x-content-type": content_type,
                   "x-cache-control-max-age": "0"}
        try:
            r = self._client.put(f"{API}/?pathname={quote(path, safe='')}", content=data, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("blob put %s failed: %s", path, exc)
            return False
        if r.status_code >= 300:
            logger.warning("blob put %s -> %s %s", path, r.status_code, r.text[:200])
            return False
        self.last_etag = r.json().get("etag") if r.headers.get("content-type", "").startswith("application/json") else None
        return True

    def put_async(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        threading.Thread(target=self.put, args=(path, data, content_type), daemon=True).start()

    def list(self, prefix: str) -> list[str]:
        try:
            r = self._client.get(f"{API}/?prefix={quote(prefix, safe='')}&limit=1000",
                                 headers={**self._auth, "x-api-version": API_VERSION})
            r.raise_for_status()
            return [b["pathname"] for b in r.json().get("blobs", [])]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("blob list %s failed: %s", prefix, exc)
            return []

    def delete(self, path: str) -> bool:
        try:
            r = self._client.post(f"{API}/delete", json={"urls": [self.base + path]},
                                  headers={**self._auth, "x-api-version": API_VERSION})
            return r.status_code < 300
        except httpx.HTTPError as exc:
            logger.warning("blob delete %s failed: %s", path, exc)
            return False

    # file helpers ----------------------------------------------------------
    def download_to(self, path: str, local: Path) -> bool:
        data = self.get(path)
        if data is None:
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return True

    def upload_file(self, local: Path, path: str, content_type: str = "application/octet-stream", wait: bool = True) -> None:
        if not local.exists():
            return
        data = local.read_bytes()
        (self.put if wait else self.put_async)(path, data, content_type)

    def restore_dir(self, prefix: str, local_dir: Path) -> int:
        """Pull every blob under prefix into local_dir if it is not already there."""
        n = 0
        for p in self.list(prefix):
            target = local_dir / p[len(prefix):]
            if not target.exists() and self.download_to(p, target):
                n += 1
        return n


_store: BlobStore | None = None
_checked = False


def blob_store() -> BlobStore | None:
    """The process-wide store, or None when no token is configured."""
    global _store, _checked
    if not _checked:
        _checked = True
        token = os.environ.get("BLOB_READ_WRITE_TOKEN")
        if token and not os.environ.get("CONSILIUM_NO_BLOB"):
            _store = BlobStore(token)
    return _store


def reset_for_tests() -> None:
    global _store, _checked
    _store, _checked = None, False
