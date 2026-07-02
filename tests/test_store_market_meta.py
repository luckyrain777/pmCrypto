"""market_snapshots 持久化 slug/category（跳转链接 + 事件类型列）+ 幂等升级。"""
import sqlite3

from data.store import Store
from strategy.models import Market, OutcomeBook


def _mk_market(slug="", category=""):
    return Market(
        market_id="0xcond1",
        question="Will X happen?",
        outcomes=(OutcomeBook(outcome="Yes", token_id="t1", best_ask=0.5),
                  OutcomeBook(outcome="No", token_id="t2", best_ask=0.5)),
        snapshot_ts=1000.0,
        slug=slug,
        category=category,
    )


def test_market_persists_slug_category(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_market_snapshot(_mk_market(slug="will-x-happen", category="Politics"))
    rows = store.latest_market_snapshots()
    assert len(rows) == 1
    assert rows[0]["slug"] == "will-x-happen"
    assert rows[0]["category"] == "Politics"


def test_market_empty_meta_defaults(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_market_snapshot(_mk_market())
    r = store.latest_market_snapshots()[0]
    assert r["slug"] == "" and r["category"] == ""


def test_idempotent_upgrade_adds_slug_category(tmp_path):
    """旧库（market_snapshots 无 slug/category 列）打开后应幂等补列，旧数据不丢。"""
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, question TEXT,
            snapshot_ts REAL NOT NULL, outcomes_json TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO market_snapshots (market_id, question, snapshot_ts, outcomes_json) "
        "VALUES ('0xold','old q',1.0,'[]')"
    )
    conn.commit()
    conn.close()

    store = Store(db)  # 触发幂等升级
    rows = store.latest_market_snapshots()
    assert len(rows) == 1
    assert rows[0]["market_id"] == "0xold"
    assert rows[0]["slug"] == "" and rows[0]["category"] == ""

    store.save_market_snapshot(_mk_market(slug="s", category="Crypto"))
    assert any(r["slug"] == "s" and r["category"] == "Crypto"
               for r in store.latest_market_snapshots())
