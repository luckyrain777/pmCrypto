"""活动流缓冲测试：记录系统每轮动作，供面板展示（不再逼用户看控制台）。"""
from core.activity import ActivityLog


def test_record_and_recent():
    log = ActivityLog(maxlen=50)
    log.record("scan", "扫描 300 个市场，产生 3 个信号", ts=100.0)
    log.record("order", "套利下单 $2.00", ts=101.0)
    rows = log.recent()
    # 倒序：最新在前
    assert rows[0]["message"] == "套利下单 $2.00"
    assert rows[0]["kind"] == "order"
    assert rows[0]["ts"] == 101.0
    assert rows[1]["message"] == "扫描 300 个市场，产生 3 个信号"


def test_ring_buffer_caps_size():
    log = ActivityLog(maxlen=5)
    for i in range(10):
        log.record("scan", f"轮次 {i}", ts=float(i))
    rows = log.recent()
    assert len(rows) == 5              # 只保留最近 5 条
    assert rows[0]["message"] == "轮次 9"   # 最新
    assert rows[-1]["message"] == "轮次 5"   # 最旧留存的


def test_recent_limit():
    log = ActivityLog(maxlen=100)
    for i in range(20):
        log.record("scan", f"m{i}", ts=float(i))
    assert len(log.recent(limit=5)) == 5


def test_empty_log():
    assert ActivityLog().recent() == []
