"""edge_detector + guard.assess_edge + 连亏熔断 集成测试。"""
import pytest

from config import Config
from core.state import STATE
from strategy.models import Market, OutcomeBook, OpportunityKind
from strategy import edge_detector
from risk import guard as guard_module
from strategy import kelly as kelly_module


@pytest.fixture(autouse=True)
def reset_state():
    STATE.reset()
    yield
    STATE.reset()


def _history_uptrend():
    """构造一段 Yes 价格持续上涨、买盘占优的历史序列。"""
    hist = []
    for i, price in enumerate([0.30, 0.33, 0.36, 0.39, 0.42]):
        hist.append(Market(
            market_id="m", question="q",
            outcomes=(
                OutcomeBook("Yes", "ty", best_ask=price + 0.01, best_bid=price - 0.01,
                            ask_size=200.0, bid_size=1500.0),  # 买盘厚
                OutcomeBook("No", "tn", best_ask=1 - price + 0.01, best_bid=1 - price - 0.01,
                            ask_size=1500.0, bid_size=200.0),
            ),
            snapshot_ts=float(i),
        ))
    return hist


def test_edge_detector_finds_directional_on_uptrend(monkeypatch):
    cfg = Config(edge_min_threshold=0.005)  # 低门槛便于触发
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)
    opps = edge_detector.detect_edge(_history_uptrend())
    # 上涨+买盘应产生看涨 Yes 的方向性机会
    assert any(o.kind == OpportunityKind.EDGE_DIRECTIONAL for o in opps)
    o = opps[0]
    assert o.estimated_p > 0
    assert 0.0 <= o.confidence <= 1.0


def test_edge_detector_short_history_empty():
    hist = _history_uptrend()[:2]
    assert edge_detector.detect_edge(hist) == []


def test_assess_edge_produces_kelly_signal(monkeypatch):
    cfg = Config(edge_min_threshold=0.005, kelly_fraction_min=0.25,
                 kelly_fraction_max=0.50, kelly_max_single_pct=0.20,
                 max_total_exposure_pct=0.50)
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)
    monkeypatch.setattr(guard_module, "CONFIG", cfg)
    monkeypatch.setattr(kelly_module, "CONFIG", cfg) if hasattr(kelly_module, "CONFIG") else None

    opps = edge_detector.detect_edge(_history_uptrend())
    assert opps
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    sig = g.assess_edge(opps[0])
    assert sig is not None
    assert sig.suggested_size_usdc > 0
    assert sig.kind == OpportunityKind.EDGE_DIRECTIONAL


def test_assess_edge_blocked_when_halted(monkeypatch):
    cfg = Config(edge_min_threshold=0.005)
    monkeypatch.setattr(edge_detector, "CONFIG", cfg)
    monkeypatch.setattr(guard_module, "CONFIG", cfg)
    opps = edge_detector.detect_edge(_history_uptrend())
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    STATE.trip("测试急停")
    assert g.assess_edge(opps[0]) is None


def test_consecutive_losses_trips():
    STATE.reset()
    for _ in range(4):
        STATE.record_trade_result(-1.0, daily_max_loss_usdc=1000.0,
                                  max_consecutive_losses=5)
    assert STATE.is_running
    STATE.record_trade_result(-1.0, daily_max_loss_usdc=1000.0,
                              max_consecutive_losses=5)
    assert STATE.is_halted


def test_win_resets_consecutive_losses():
    STATE.reset()
    for _ in range(4):
        STATE.record_trade_result(-1.0, daily_max_loss_usdc=1000.0,
                                  max_consecutive_losses=5)
    STATE.record_trade_result(+2.0, daily_max_loss_usdc=1000.0,
                              max_consecutive_losses=5)  # 一次盈利清零
    for _ in range(4):
        STATE.record_trade_result(-1.0, daily_max_loss_usdc=1000.0,
                                  max_consecutive_losses=5)
    assert STATE.is_running  # 清零后又亏4笔，未达5
