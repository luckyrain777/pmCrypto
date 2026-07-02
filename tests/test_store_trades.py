"""真实持仓台账（trades 表）测试：下单留痕 + 结算平仓回填的存储层。"""
import sqlite3

from data.store import Store


def test_record_and_list_open_trade(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    tid = store.record_trade(market_id="0xm1", token_id="tokYES",
                             cost_price=0.45, shares=100.0, cost_usdc=45.0,
                             created_ts=1000.0)
    assert isinstance(tid, int)
    opens = store.open_trades()
    assert len(opens) == 1
    r = opens[0]
    assert r["market_id"] == "0xm1" and r["token_id"] == "tokYES"
    assert r["cost_usdc"] == 45.0 and r["status"] == "open"


def test_close_trade_marks_closed_and_records_pnl(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    tid = store.record_trade("0xm1", "tokYES", 0.45, 100.0, 45.0, 1000.0)
    store.close_trade(tid, realized_pnl_usdc=55.0, resolved_ts=2000.0)
    # 已平仓 → 不再出现在 open_trades
    assert store.open_trades() == []
    # 台账仍在，状态 closed，pnl 记录
    all_rows = store.all_trades()
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "closed"
    assert all_rows[0]["realized_pnl_usdc"] == 55.0


def test_open_trades_only_returns_open(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    a = store.record_trade("0xa", "t1", 0.5, 10.0, 5.0, 1.0)
    store.record_trade("0xb", "t2", 0.3, 10.0, 3.0, 1.0)
    store.close_trade(a, realized_pnl_usdc=5.0, resolved_ts=2.0)
    opens = store.open_trades()
    assert len(opens) == 1 and opens[0]["market_id"] == "0xb"


def test_trades_schema_created_on_fresh_db(tmp_path):
    """全新库应含 trades 表（供 open_trades 查询不报错）。"""
    store = Store(str(tmp_path / "fresh.db"))
    assert store.open_trades() == []  # 空但不报错


def test_trades_table_added_to_legacy_db(tmp_path):
    """旧库（无 trades 表）打开后应幂等建表，不影响既有数据。"""
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, market_id TEXT, "
        "question TEXT, snapshot_ts REAL, outcomes_json TEXT);"
    )
    conn.commit()
    conn.close()
    store = Store(db)  # 触发建表
    assert store.open_trades() == []
    tid = store.record_trade("0xm", "t", 0.5, 2.0, 1.0, 1.0)
    assert len(store.open_trades()) == 1
