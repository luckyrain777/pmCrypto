"""store 持久化 question 快照 + 幂等列升级测试。

信号/机会产生时把市场问题文本一起存下，即使市场快照被后续轮次挤出，
信号自身仍带名字（面板不再退化成 0x 地址）。
"""
import sqlite3

from data.store import Store
from strategy.models import Opportunity, OpportunityKind, Signal


def _mk_opp(question=""):
    return Opportunity(
        market_id="0xabc123",
        kind=OpportunityKind.YES_NO_COMPLEMENT,
        raw_edge=0.05,
        legs=(("tok1", 0.48),),
        min_leg_notional_usdc=100.0,
        snapshot_ts=1000.0,
        question=question,
    )


def _mk_sig(question=""):
    return Signal(
        market_id="0xabc123",
        kind=OpportunityKind.YES_NO_COMPLEMENT,
        raw_edge=0.05,
        net_edge=0.03,
        suggested_size_usdc=50.0,
        legs=(("tok1", 0.48),),
        reason="test",
        snapshot_ts=1000.0,
        question=question,
    )


def test_signal_persists_question(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_signal(_mk_sig("Will BTC be above $100k by Friday?"), created_ts=1000.0)
    rows = store.recent_signals()
    assert len(rows) == 1
    assert rows[0]["question"] == "Will BTC be above $100k by Friday?"


def test_opportunity_persists_question(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_opportunity(_mk_opp("Will ETH flip BTC in 2026?"))
    rows = store.recent_opportunities()
    assert len(rows) == 1
    assert rows[0]["question"] == "Will ETH flip BTC in 2026?"


def test_empty_question_defaults(tmp_path):
    """未带 question（旧信号/无文本）不报错，读回空串。"""
    store = Store(str(tmp_path / "t.db"))
    store.save_signal(_mk_sig(), created_ts=1000.0)
    assert store.recent_signals()[0]["question"] == ""


def test_idempotent_column_upgrade_on_legacy_db(tmp_path):
    """对没有 question 列的旧库执行 Store()，应幂等补列且旧数据不丢。"""
    db = str(tmp_path / "legacy.db")
    # 手动建一个缺 question 列的旧 signals 表 + 一条旧数据
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, kind TEXT NOT NULL,
            raw_edge REAL NOT NULL, net_edge REAL NOT NULL,
            suggested_size_usdc REAL, legs_json TEXT, reason TEXT,
            snapshot_ts REAL NOT NULL, created_ts REAL NOT NULL
        );
        CREATE TABLE opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, kind TEXT NOT NULL,
            raw_edge REAL NOT NULL, min_leg_notional_usdc REAL,
            legs_json TEXT, snapshot_ts REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO signals (market_id, kind, raw_edge, net_edge, "
        "suggested_size_usdc, legs_json, reason, snapshot_ts, created_ts) "
        "VALUES ('0xold','yes_no_complement',0.05,0.03,50,'[]','old',1.0,1.0)"
    )
    conn.commit()
    conn.close()

    # 打开 → 触发幂等升级
    store = Store(db)
    rows = store.recent_signals()
    assert len(rows) == 1
    assert rows[0]["market_id"] == "0xold"
    assert rows[0]["question"] == ""  # 补列默认空串

    # 升级后新写入带 question 正常
    store.save_signal(_mk_sig("new one"), created_ts=2.0)
    rows = store.recent_signals()
    assert any(r["question"] == "new one" for r in rows)
