"""guard 单元测试：仓位约束 + 急停管辖。"""
import pytest

import config as config_module
from config import Config
from core.state import STATE, RunState
from strategy.models import Opportunity, OpportunityKind
from risk import guard as guard_module
from strategy import pricing as pricing_module


@pytest.fixture(autouse=True)
def reset_state():
    """每个测试前后确保全局状态干净（RUNNING）。"""
    STATE.reset()
    yield
    STATE.reset()


@pytest.fixture
def fixed_config(monkeypatch):
    """单笔上限 10%、总仓位 50%、阈值 1.5%、费率0、安全系数1、流动性门槛20。"""
    cfg = Config(
        account_balance_usdc=100.0,
        max_position_pct=0.10,
        max_total_exposure_pct=0.50,
        fee_rate=0.0,
        slippage_safety_factor=1.0,
        min_profit_threshold=0.015,
        min_liquidity_usdc=20.0,
    )
    monkeypatch.setattr(guard_module, "CONFIG", cfg)
    monkeypatch.setattr(pricing_module, "CONFIG", cfg)
    return cfg


def _opp(raw_edge=0.10, min_notional=1000.0):
    return Opportunity(
        market_id="m",
        kind=OpportunityKind.YES_NO_COMPLEMENT,
        raw_edge=raw_edge,
        legs=(("ty", 0.45), ("tn", 0.45)),
        min_leg_notional_usdc=min_notional,
        snapshot_ts=1.0,
    )


def test_suggest_size_capped_by_single_limit(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    # 单笔上限 = 100*0.10 = 10；深度 1000 → 取 10
    assert abs(g.suggest_size_usdc(_opp(min_notional=1000.0)) - 10.0) < 1e-9


def test_suggest_size_capped_by_liquidity(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    # 深度 5 < 单笔上限 10 → 取 5
    assert abs(g.suggest_size_usdc(_opp(min_notional=5.0)) - 5.0) < 1e-9


def test_suggest_size_capped_by_total_exposure(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    # 总上限 50；已占用 48 → 剩 2；即使单笔上限 10、深度大 → 取 2
    g.reserve(48.0)
    assert abs(g.suggest_size_usdc(_opp(min_notional=1000.0)) - 2.0) < 1e-9


def test_assess_produces_signal_when_profitable(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    sig = g.assess(_opp(raw_edge=0.10, min_notional=1000.0))
    assert sig is not None
    assert sig.suggested_size_usdc == 10.0
    assert sig.net_edge > 0.015


def test_assess_blocked_when_halted(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    STATE.trip("测试急停")
    # 急停态下绝不产生信号
    assert g.assess(_opp(raw_edge=0.50, min_notional=1000.0)) is None


def test_assess_none_when_no_exposure_left(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    g.reserve(50.0)  # 用满总仓位
    assert g.assess(_opp()) is None


def test_assess_none_when_not_profitable(fixed_config):
    g = guard_module.RiskGuard(account_balance_usdc=100.0)
    # 毛偏差太小，扣成本后低于阈值
    assert g.assess(_opp(raw_edge=0.001, min_notional=1000.0)) is None
