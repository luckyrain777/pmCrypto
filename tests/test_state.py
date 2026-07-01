"""core.state 急停逻辑测试。"""
import pytest

from core.state import GlobalState, RunState


def test_starts_running():
    s = GlobalState()
    assert s.is_running
    assert not s.is_halted


def test_trip_halts():
    s = GlobalState()
    s.trip("手动急停")
    assert s.is_halted
    assert s.snapshot()["halt_reason"] == "手动急停"


def test_api_failures_trip_at_threshold():
    s = GlobalState()
    for _ in range(4):
        s.record_api_failure(threshold=5)
    assert s.is_running  # 还没到阈值
    s.record_api_failure(threshold=5)
    assert s.is_halted    # 第 5 次触发


def test_api_success_resets_counter():
    s = GlobalState()
    s.record_api_failure(threshold=5)
    s.record_api_failure(threshold=5)
    s.record_api_success()
    assert s.snapshot()["api_failure_count"] == 0


def test_daily_loss_trips():
    s = GlobalState()
    # 当日累计亏损达 20 触发（上限设 20）
    s.record_trade_result(pnl_usdc=-15.0, daily_max_loss_usdc=20.0)
    assert s.is_running
    s.record_trade_result(pnl_usdc=-6.0, daily_max_loss_usdc=20.0)
    assert s.is_halted


def test_reset_clears_halt():
    s = GlobalState()
    s.trip("x")
    s.reset()
    assert s.is_running
