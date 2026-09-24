import json
import time

from fastapi.testclient import TestClient


def test_api_end_to_end():
    from consilium.server.app import create_app
    c = TestClient(create_app())
    assert c.get("/api/health").json()["ok"]
    funds = c.get("/api/funds").json()
    assert {f["name"] for f in funds} >= {"systematic-trend", "committee-balanced"}
    assert c.get("/").status_code == 200 and "Consilium" in c.get("/").text

    job = c.post("/api/backtest", json={"fund": "systematic-trend", "tickers": "AAPL,MSFT,JPM", "start": "2024-01-01",
                                        "end": "2024-06-30", "provider": "synthetic"}).json()
    for _ in range(200):
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert j["status"] == "done", j.get("error")
    res = j["result"]
    assert res["metrics"]["n_periods"] > 10 and len(res["records"]) == res["metrics"]["n_periods"]

    # streaming backtest: NDJSON progress lines, result last
    lines = []
    with c.stream("POST", "/api/backtest/stream", json={"fund": "systematic-trend", "tickers": "AAPL,MSFT", "start": "2024-01-01",
                                                          "end": "2024-04-30", "provider": "synthetic"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.strip():
                lines.append(json.loads(line))
    kinds = [m["type"] for m in lines]
    assert kinds[0] == "start" and kinds[-1] == "result" and kinds.count("progress") == lines[-1]["result"]["metrics"]["n_periods"]
    assert lines[1]["nav"] > 0 and lines[1]["regime"] in ("risk-on", "neutral", "risk-off")

    # a paper cycle is synchronous: one request, the record comes back
    j = c.post("/api/run", json={"fund": "systematic-trend", "tickers": "AAPL,MSFT,JPM", "date": "2024-07-05", "provider": "synthetic"}).json()
    assert j["status"] == "done", j.get("error")
    assert j["result"]["as_of"] == "2024-07-05"
    led = c.get("/api/ledger/systematic-trend").json()
    assert len(led["cycles"]) == 1 and led["book"]["as_of"] == "2024-07-05"
    rec = c.get(f"/api/cycles/{led['cycles'][0]['id']}").json()
    assert rec["verdict"]["cio"]["regime"] in ("risk-on", "neutral", "risk-off")

    # mandate CRUD
    y = c.get("/api/funds/systematic-trend").json()["yaml"].replace("name: systematic-trend", "name: test-copy")
    assert c.put("/api/funds/test-copy", json={"yaml": y}).status_code == 200
    assert c.put("/api/funds/test-copy", json={"yaml": y.replace("max_position_pct", "max_positon_pct")}).status_code == 400
    assert c.delete("/api/funds/test-copy").json()["deleted"] == "test-copy"
