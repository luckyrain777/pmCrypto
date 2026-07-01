"""SQLite 存储：市场快照、机会、信号历史。

零外部依赖（标准库 sqlite3）。所有写入留痕，供网页面板展示与回测回放。
回测引擎从 market_snapshots 表按 snapshot_ts 顺序回放，喂给同一套策略逻辑。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Iterator, Optional

from strategy.models import (
    Market,
    OutcomeBook,
    Opportunity,
    OpportunityKind,
    Signal,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    question    TEXT,
    snapshot_ts REAL NOT NULL,
    outcomes_json TEXT NOT NULL          -- 序列化的 OutcomeBook 列表
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON market_snapshots(snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_snap_market ON market_snapshots(market_id);

CREATE TABLE IF NOT EXISTS opportunities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    question    TEXT DEFAULT '',          -- 市场问题文本快照（面板显示名字）
    kind        TEXT NOT NULL,
    raw_edge    REAL NOT NULL,
    min_leg_notional_usdc REAL,
    legs_json   TEXT,
    snapshot_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    question    TEXT DEFAULT '',          -- 市场问题文本快照（面板显示名字）
    kind        TEXT NOT NULL,
    raw_edge    REAL NOT NULL,
    net_edge    REAL NOT NULL,
    suggested_size_usdc REAL,
    legs_json   TEXT,
    reason      TEXT,
    snapshot_ts REAL NOT NULL,
    created_ts  REAL NOT NULL
);

-- 市场结算结果：edge 验证的原料（预测 vs 真实）。
CREATE TABLE IF NOT EXISTS resolutions (
    market_id       TEXT PRIMARY KEY,
    winning_token_id TEXT,               -- 最终获胜结果的 token_id
    resolved_ts     REAL NOT NULL
);
"""


class Store:
    """SQLite 封装。线程安全（每次操作单独连接，避免跨线程共享）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 幂等升级：给旧库补 end_ts 列（加密定价需要到期时间）。
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(market_snapshots)").fetchall()}
            if "end_ts" not in cols:
                conn.execute(
                    "ALTER TABLE market_snapshots ADD COLUMN end_ts REAL DEFAULT 0")
            # 幂等升级：给旧库的 signals/opportunities 补 question 列（问题文本快照）。
            for tbl in ("signals", "opportunities"):
                tcols = {r[1] for r in conn.execute(
                    f"PRAGMA table_info({tbl})").fetchall()}
                if "question" not in tcols:
                    conn.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN question TEXT DEFAULT ''")

    # ── 写入 ──────────────────────────────────────────────
    def save_market_snapshot(self, market: Market) -> None:
        outcomes = [
            {
                "outcome": o.outcome,
                "token_id": o.token_id,
                "best_ask": o.best_ask,
                "best_bid": o.best_bid,
                "ask_size": o.ask_size,
                "bid_size": o.bid_size,
            }
            for o in market.outcomes
        ]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO market_snapshots "
                "(market_id, question, snapshot_ts, outcomes_json, end_ts) "
                "VALUES (?,?,?,?,?)",
                (market.market_id, market.question, market.snapshot_ts,
                 json.dumps(outcomes), market.end_ts),
            )

    def save_opportunity(self, opp: Opportunity) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO opportunities "
                "(market_id, question, kind, raw_edge, min_leg_notional_usdc, "
                " legs_json, snapshot_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (opp.market_id, opp.question, opp.kind.value, opp.raw_edge,
                 opp.min_leg_notional_usdc, json.dumps(list(opp.legs)),
                 opp.snapshot_ts),
            )

    def save_signal(self, sig: Signal, created_ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO signals "
                "(market_id, question, kind, raw_edge, net_edge, suggested_size_usdc, "
                " legs_json, reason, snapshot_ts, created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sig.market_id, sig.question, sig.kind.value, sig.raw_edge,
                 sig.net_edge, sig.suggested_size_usdc, json.dumps(list(sig.legs)),
                 sig.reason, sig.snapshot_ts, created_ts),
            )

    # ── 读取（供网页面板）─────────────────────────────────
    def recent_signals(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_opportunities(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM opportunities ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_market_snapshots(self, limit: int = 100) -> list[dict]:
        """每个 market_id 取最新一条快照。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.* FROM market_snapshots m
                JOIN (
                    SELECT market_id, MAX(snapshot_ts) AS mts
                    FROM market_snapshots GROUP BY market_id
                ) latest
                ON m.market_id = latest.market_id AND m.snapshot_ts = latest.mts
                ORDER BY m.snapshot_ts DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 回测回放：按时间顺序逐条吐出历史 Market ─────────────
    def replay_markets(self) -> Iterator[Market]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_snapshots ORDER BY snapshot_ts ASC"
            ).fetchall()
        for r in rows:
            outcomes = tuple(
                OutcomeBook(
                    outcome=o["outcome"],
                    token_id=o["token_id"],
                    best_ask=o["best_ask"],
                    best_bid=o["best_bid"],
                    ask_size=o.get("ask_size", 0.0),
                    bid_size=o.get("bid_size", 0.0),
                )
                for o in json.loads(r["outcomes_json"])
            )
            yield Market(
                market_id=r["market_id"],
                question=r["question"] or "",
                outcomes=outcomes,
                snapshot_ts=r["snapshot_ts"],
                end_ts=(r["end_ts"] if "end_ts" in r.keys() else 0.0) or 0.0,
            )

    def market_history(self, market_id: str, limit: int = 50) -> list[Market]:
        """取某市场最近 limit 条快照，时间升序（最新在末尾）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_snapshots WHERE market_id=? "
                "ORDER BY snapshot_ts DESC LIMIT ?",
                (market_id, limit),
            ).fetchall()
        out: list[Market] = []
        for r in reversed(rows):  # 反转为时间升序
            outcomes = tuple(
                OutcomeBook(
                    outcome=o["outcome"],
                    token_id=o["token_id"],
                    best_ask=o["best_ask"],
                    best_bid=o["best_bid"],
                    ask_size=o.get("ask_size", 0.0),
                    bid_size=o.get("bid_size", 0.0),
                )
                for o in json.loads(r["outcomes_json"])
            )
            out.append(Market(
                market_id=r["market_id"],
                question=r["question"] or "",
                outcomes=outcomes,
                snapshot_ts=r["snapshot_ts"],
                end_ts=(r["end_ts"] if "end_ts" in r.keys() else 0.0) or 0.0,
            ))
        return out

    def distinct_market_ids(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT market_id FROM market_snapshots"
            ).fetchall()
        return [r["market_id"] for r in rows]

    # ── 结算结果：edge 验证的原料 ─────────────────────────
    def save_resolution(self, market_id: str, winning_token_id: str | None,
                        resolved_ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO resolutions "
                "(market_id, winning_token_id, resolved_ts) VALUES (?,?,?)",
                (market_id, winning_token_id, resolved_ts),
            )

    def get_resolution(self, market_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM resolutions WHERE market_id=?", (market_id,)
            ).fetchone()
        return dict(row) if row else None

    def all_resolutions(self) -> dict[str, str | None]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM resolutions").fetchall()
        return {r["market_id"]: r["winning_token_id"] for r in rows}
