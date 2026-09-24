"""BlobStore against an in-memory fake of the Vercel Blob API, and the ledger /
prompt cache / mandate restore paths that sit on top of it."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from consilium.storage import blob
from consilium.storage.blob import BlobStore

TOKEN = "vercel_blob_rw_teststore123_secret"


class FakeBlobAPI:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def _etag(self, path):
        import hashlib
        return '"' + hashlib.md5(self.objects[path]).hexdigest() + '"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlparse(str(request.url))
        if u.netloc == "blob.vercel-storage.com":
            if request.method == "PUT":
                assert request.headers["x-vercel-blob-access"] == "private"
                path = parse_qs(u.query)["pathname"][0]
                self.objects[path] = request.content; self.puts += 1
                return httpx.Response(200, json={"url": f"https://teststore123.private.blob.vercel-storage.com/{path}", "pathname": path, "etag": self._etag(path)})
            if request.method == "GET":
                prefix = parse_qs(u.query).get("prefix", [""])[0]
                return httpx.Response(200, json={"blobs": [{"pathname": p} for p in self.objects if p.startswith(prefix)], "hasMore": False})
            if request.method == "POST" and u.path == "/delete":
                for url in json.loads(request.content)["urls"]:
                    self.objects.pop(url.split(".com/", 1)[1], None)
                return httpx.Response(200, json=None)
        if u.netloc == "teststore123.private.blob.vercel-storage.com":
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return httpx.Response(403)
            path = unquote(u.path.lstrip("/"))
            if path not in self.objects:
                return httpx.Response(404)
            if request.headers.get("If-None-Match") == self._etag(path):
                return httpx.Response(304)
            return httpx.Response(200, content=self.objects[path], headers={"etag": self._etag(path)})
        return httpx.Response(500)


@pytest.fixture
def fake(monkeypatch):
    api = FakeBlobAPI()
    store = BlobStore(TOKEN, transport=httpx.MockTransport(api.handler))
    monkeypatch.delenv("CONSILIUM_NO_BLOB", raising=False)
    monkeypatch.setattr(blob, "_store", store)
    monkeypatch.setattr(blob, "_checked", True)
    # the modules bound `blob_store` at import; they call it at runtime, so patching the globals is enough
    yield api, store
    blob.reset_for_tests()


def test_store_primitives(fake):
    api, store = fake
    assert store.store_id == "teststore123"
    assert store.get("nope.json") is None
    assert store.put("a/b.json", b'{"x":1}', "application/json")
    assert store.get("a/b.json") == b'{"x":1}'
    assert store.list("a/") == ["a/b.json"]
    assert store.delete("a/b.json") and store.get("a/b.json") is None


def test_ledger_mirrors_and_restores(fake, tmp_path):
    from consilium.backtest import backtest_fund
    from consilium.core.spec import Fund, load_spec
    from consilium.data import SyntheticProvider
    from consilium.ledger import Ledger
    api, store = fake
    mandates = Path(__file__).resolve().parent.parent / "consilium" / "mandates"
    res = backtest_fund(Fund(load_spec(mandates / "systematic-trend.yaml")), "2024-01-01", "2024-03-31",
                        SyntheticProvider(), ["AAPL", "MSFT"], monte_carlo_paths=5)
    first = Ledger(tmp_path / "one" / "ledger.sqlite")
    for r in res.records:
        first.record_cycle(r, mode="paper")
    receipt = tmp_path / "one" / "runs" / "bt.json"; receipt.parent.mkdir(parents=True); receipt.write_text(res.model_dump_json())
    first.record_backtest(res, receipt)
    assert "runs/bt.json" in api.objects
    assert sum(1 for k in api.objects if k.startswith("cycles/systematic-trend/paper/")) == len(res.records)
    assert any(k.startswith("backtests/") for k in api.objects)
    # a brand-new "instance" with an empty disk sees the same books
    second = Ledger(tmp_path / "two" / "ledger.sqlite")
    assert len(second.cycles("systematic-trend")) == len(res.records)
    assert second.latest_book("systematic-trend")[2] == res.records[-1].as_of
    assert second.backtests("systematic-trend")[0]["n_periods"] == res.metrics.n_periods
    # two warm instances: a write on one is visible on the other without a restart
    assert first.reset("systematic-trend") == len(res.records)
    assert second.cycles("systematic-trend") == []
    second.record_cycle(res.records[0], mode="paper")
    assert [c["as_of"] for c in first.cycles("systematic-trend")] == [res.records[0].as_of]
    # concurrent writers never lose each other's meetings (append-only objects)
    first.record_cycle(res.records[1], mode="paper")
    second.record_cycle(res.records[2], mode="paper")
    assert len(first.cycles("systematic-trend")) == len(second.cycles("systematic-trend")) == 3


def test_prompt_cache_read_through(fake, tmp_path):
    from consilium.llm import PromptCache
    api, store = fake
    a = PromptCache(tmp_path / "a")
    a.put("k1", {"parsed": {"stance": "bullish"}})
    import time
    for _ in range(50):
        if "prompts/k1.json" in api.objects:
            break
        time.sleep(0.02)
    assert "prompts/k1.json" in api.objects
    b = PromptCache(tmp_path / "b")      # empty local dir: must find it in the store
    assert b.get("k1") == {"parsed": {"stance": "bullish"}} and b.hits == 1
    assert b.get("missing") is None and b.misses == 1


def test_mandates_restore_on_serverless_boot(fake, tmp_path, monkeypatch):
    api, store = fake
    store.put("mandates/saved-by-user.yaml", b"name: saved-by-user\n", "application/yaml")
    monkeypatch.setenv("CONSILIUM_SERVERLESS", "1")
    monkeypatch.setenv("CONSILIUM_HOME", str(tmp_path / "home"))
    import importlib
    import consilium.core.paths as paths
    importlib.reload(paths)
    paths.ensure_home()
    assert (tmp_path / "home" / "mandates" / "saved-by-user.yaml").read_text() == "name: saved-by-user\n"
    assert (tmp_path / "home" / "mandates" / "committee-balanced.yaml").exists()
