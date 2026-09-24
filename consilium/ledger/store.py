"""Ledger — the fund's persistent books (SQLite, stdlib only).

Every cycle and backtest is written here. `latest_book(fund)` seeds the next
live run's broker from the last known positions and cash, so NAV is a track
record that carries between runs instead of resetting to the mandate's capital.

Durable hosts (a BlobStore is configured): the SQLite file is a local index
only. Every cycle and backtest row is ALSO written as its own immutable blob —
`cycles/<fund>/<mode>/<as_of>-<id>.json`, `backtests/<id>.json` — and before
any read or write the ledger lists the store and imports what it has not seen.
Append-only objects are never overwritten, so many instances can write at once
without a stale one clobbering another's meeting (the failure mode of mirroring
one shared file).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from consilium.core.models import CycleRecord, Position
from consilium.core.paths import LEDGER_PATH
from consilium.storage import blob_store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
  id TEXT PRIMARY KEY, fund TEXT NOT NULL, as_of TEXT NOT NULL, mode TEXT NOT NULL,
  nav REAL NOT NULL, cash REAL NOT NULL, costs REAL NOT NULL, n_orders INTEGER NOT NULL,
  regime TEXT, record TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cycles_fund_asof ON cycles(fund, as_of);
CREATE TABLE IF NOT EXISTS backtests (
  id TEXT PRIMARY KEY, fund TEXT NOT NULL, start TEXT, end TEXT, universe TEXT, provider TEXT,
  total_return REAL, sharpe REAL, max_drawdown REAL, benchmark_return REAL, n_periods INTEGER,
  summary TEXT NOT NULL, result_path TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS synced (pathname TEXT PRIMARY KEY, tbl TEXT NOT NULL, row_id TEXT NOT NULL);
"""
_CYCLE_COLS = ("id", "fund", "as_of", "mode", "nav", "cash", "costs", "n_orders", "regime", "record", "created_at")
_BT_COLS = ("id", "fund", "start", "end", "universe", "provider", "total_return", "sharpe", "max_drawdown",
            "benchmark_return", "n_periods", "summary", "result_path", "created_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path | str = LEDGER_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._blob = blob_store()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._sync()

    # durable sync ---------------------------------------------------------
    def _sync(self) -> None:
        """Import every blob row this instance has not seen yet (one list call per prefix)."""
        if self._blob is None:
            return
        with self._lock:
            for prefix, table, cols in (("cycles/", "cycles", _CYCLE_COLS), ("backtests/", "backtests", _BT_COLS)):
                remote = set(self._blob.list(prefix))
                local = {r[0]: r[1] for r in self._db.execute("SELECT pathname, row_id FROM synced WHERE tbl=?", (table,))}
                for name in remote - set(local):
                    data = self._blob.get(name)
                    if data is None:
                        continue
                    row = json.loads(data)
                    self._db.execute(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(cols))})",
                                     tuple(row[c] for c in cols))
                    self._db.execute("INSERT OR REPLACE INTO synced VALUES (?,?,?)", (name, table, row["id"]))
                for name in set(local) - remote:          # deleted elsewhere (a reset on another instance)
                    self._db.execute(f"DELETE FROM {table} WHERE id=?", (local[name],))
                    self._db.execute("DELETE FROM synced WHERE pathname=?", (name,))
            self._db.commit()

    def _publish(self, pathname: str, table: str, row: dict) -> None:
        if self._blob is None:
            return
        if self._blob.put(pathname, json.dumps(row).encode(), "application/json"):
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO synced VALUES (?,?,?)", (pathname, table, row["id"]))
                self._db.commit()

    # cycles -----------------------------------------------------------
    def record_cycle(self, rec: CycleRecord, mode: str = "paper") -> None:
        self._sync()
        row = dict(zip(_CYCLE_COLS, (rec.id, rec.fund, rec.as_of, mode, rec.nav, rec.cash, rec.costs, len(rec.orders),
                                     rec.verdict.cio.regime, rec.model_dump_json(), _now())))
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(row[c] for c in _CYCLE_COLS))
            self._db.commit()
        self._publish(f"cycles/{rec.fund}/{mode}/{rec.as_of}-{rec.id}.json", "cycles", row)

    def cycles(self, fund: str, mode: str = "paper", limit: int = 500) -> list[dict]:
        self._sync()
        rows = self._db.execute(
            "SELECT id, as_of, nav, cash, costs, n_orders, regime, created_at FROM cycles "
            "WHERE fund=? AND mode=? ORDER BY as_of ASC, created_at ASC LIMIT ?", (fund, mode, limit)).fetchall()
        return [dict(zip(("id", "as_of", "nav", "cash", "costs", "n_orders", "regime", "created_at"), r)) for r in rows]

    def cycle(self, cycle_id: str) -> CycleRecord | None:
        row = self._db.execute("SELECT record FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            self._sync()
            row = self._db.execute("SELECT record FROM cycles WHERE id=?", (cycle_id,)).fetchone()
        return CycleRecord.model_validate_json(row[0]) if row else None

    def latest_book(self, fund: str, mode: str = "paper") -> tuple[float, dict[str, Position], str] | None:
        self._sync()
        row = self._db.execute(
            "SELECT record FROM cycles WHERE fund=? AND mode=? ORDER BY as_of DESC, created_at DESC LIMIT 1",
            (fund, mode)).fetchone()
        if not row:
            return None
        rec = CycleRecord.model_validate_json(row[0])
        positions = {t: Position(ticker=t, shares=s, avg_cost=rec.marks.get(t, 0.0)) for t, s in rec.positions.items()}
        return rec.cash, positions, rec.as_of

    def funds(self) -> list[dict]:
        self._sync()
        rows = self._db.execute(
            "SELECT fund, mode, COUNT(*), MIN(as_of), MAX(as_of) FROM cycles GROUP BY fund, mode").fetchall()
        return [dict(zip(("fund", "mode", "n_cycles", "first", "last"), r)) for r in rows]

    def reset(self, fund: str, mode: str = "paper") -> int:
        self._sync()
        with self._lock:
            cur = self._db.execute("DELETE FROM cycles WHERE fund=? AND mode=?", (fund, mode))
            self._db.commit()
        if self._blob is not None:
            for name in self._blob.list(f"cycles/{fund}/{mode}/"):
                self._blob.delete(name)
                self._db.execute("DELETE FROM synced WHERE pathname=?", (name,))
            self._db.commit()
        return cur.rowcount

    # backtests --------------------------------------------------------
    def record_backtest(self, result, result_path: Path | None) -> str:
        self._sync()
        bid = f"bt-{result.fund}-{result.end}-{abs(hash((result.start, tuple(result.universe)))) % 10**6:06d}"
        m = result.metrics
        summary = {"metrics": m.model_dump(), "dates": result.dates[::max(1, len(result.dates) // 200)],
                   "in_sample": result.in_sample.model_dump() if result.in_sample else None,
                   "out_of_sample": result.out_of_sample.model_dump() if result.out_of_sample else None,
                   "monte_carlo": {k: v for k, v in result.monte_carlo.items() if k != "bands"},
                   "llm_used": result.llm_used, "model": result.model}
        row = dict(zip(_BT_COLS, (bid, result.fund, result.start, result.end, ",".join(result.universe), result.provider,
                                  m.total_return, m.sharpe, m.max_drawdown, m.benchmark_return, m.n_periods,
                                  json.dumps(summary), str(result_path) if result_path else None, _now())))
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO backtests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row[c] for c in _BT_COLS))
            self._db.commit()
        if result_path is not None and self._blob is not None:
            self._blob.upload_file(Path(result_path), f"runs/{Path(result_path).name}", "application/json")
        self._publish(f"backtests/{bid}.json", "backtests", row)
        return bid

    def backtests(self, fund: str | None = None, limit: int = 50) -> list[dict]:
        self._sync()
        q = ("SELECT id, fund, start, end, universe, provider, total_return, sharpe, max_drawdown, benchmark_return, "
             "n_periods, result_path, created_at FROM backtests")
        args: tuple = ()
        if fund:
            q += " WHERE fund=?"
            args = (fund,)
        q += " ORDER BY created_at DESC LIMIT ?"
        rows = self._db.execute(q, args + (limit,)).fetchall()
        return [dict(zip(_BT_COLS[:11] + ("result_path", "created_at"), r)) for r in rows]
