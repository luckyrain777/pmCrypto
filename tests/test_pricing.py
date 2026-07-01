"""pricing 单元测试：已知偏差+成本 → 已知净收益。

通过 monkeypatch 把 CONFIG 替换为确定的测试参数，避免受默认值变动影响。
"""
import dataclasses

import pytest

import config as config_module
from config import Config
from strategy.models import Opportunity, OpportunityKind
from strategy import pricing as pricing_module


@pytest.fixture
def fixed_config(monkeypatch):
    """确定性配置：费率 0、安全系数 1.0、阈值 1.5%、流动性门槛 20。"""
    cfg = Config(
        fee_rate=0.0,
        slippage_safety_factor=1.0,
        min_profit_threshold=0.015,
        min_liquidity_usdc=20.0,
    )
    monkeypatch.setattr(pricing_module, "CONFIG", cfg)
    return cfg


def _opp(raw_edge, min_notional, n_legs=2):
    return Opportunity(
        market_id="m",
        kind=OpportunityKind.YES_NO_COMPLEMENT,
        raw_edge=raw_edge,
        legs=tuple((f"t{i}", 0.4) for i in range(n_legs)),
        min_leg_notional_usdc=min_notional,
        snapshot_ts=1.0,
    )


def test_liquidity_filter_rejects_thin(fixed_config):
    # 最薄腿 10 < 门槛 20 → 直接不可成交
    res = pricing_module.evaluate(_opp(0.10, min_notional=10.0), target_notional_usdc=5.0)
    assert res.tradable is False
    assert "流动性不足" in res.reason


def test_net_edge_with_zero_slippage(fixed_config):
    # 深度 1000 远大于成交额 5 → 滑点≈0；费率 0 → 净收益≈毛偏差
    res = pricing_module.evaluate(_opp(0.05, min_notional=1000.0), target_notional_usdc=5.0)
    # fill_ratio = 5/1000 = 0.005; slip = 0.5*0.005*1.0 = 0.0025
    assert res.tradable is True
    assert abs(res.net_edge - (0.05 - 0.0025)) < 1e-9


def test_slippage_grows_with_size(fixed_config):
    # 成交额等于深度 → fill_ratio=1 → slip=0.5；毛偏差 0.05 → 净收益 -0.45 → 不可成交
    res = pricing_module.evaluate(_opp(0.05, min_notional=100.0), target_notional_usdc=100.0)
    assert res.tradable is False
    assert abs(res.net_edge - (0.05 - 0.5)) < 1e-9


def test_safety_factor_amplifies_slippage(monkeypatch):
    # 安全系数 2.0 → 滑点翻倍，更保守
    cfg = Config(fee_rate=0.0, slippage_safety_factor=2.0,
                 min_profit_threshold=0.0, min_liquidity_usdc=20.0)
    monkeypatch.setattr(pricing_module, "CONFIG", cfg)
    res = pricing_module.evaluate(_opp(0.10, min_notional=100.0), target_notional_usdc=50.0)
    # fill_ratio=0.5; slip=0.5*0.5*2.0=0.5; net=0.10-0.5=-0.40
    assert abs(res.net_edge - (0.10 - 0.5)) < 1e-9


def test_fee_per_leg(monkeypatch):
    # 费率 0.01，3 条腿 → 费 0.03
    cfg = Config(fee_rate=0.01, slippage_safety_factor=0.0,  # 关掉滑点单独验费
                 min_profit_threshold=0.0, min_liquidity_usdc=1.0)
    monkeypatch.setattr(pricing_module, "CONFIG", cfg)
    res = pricing_module.evaluate(_opp(0.10, min_notional=1000.0, n_legs=3),
                                  target_notional_usdc=5.0)
    assert abs(res.net_edge - (0.10 - 0.03)) < 1e-9


def test_below_threshold_not_tradable(monkeypatch):
    cfg = Config(fee_rate=0.0, slippage_safety_factor=0.0,
                 min_profit_threshold=0.05, min_liquidity_usdc=1.0)
    monkeypatch.setattr(pricing_module, "CONFIG", cfg)
    # 毛偏差 0.02 < 阈值 0.05 → 不可成交
    res = pricing_module.evaluate(_opp(0.02, min_notional=1000.0), target_notional_usdc=5.0)
    assert res.tradable is False
