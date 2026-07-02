"""波动率校准测试：用历史 K 线算已实现年化波动率 + 失败降级到常数。"""
import math

import pytest

from data.crypto_price import (
    CryptoPriceSource, realized_annual_vol, DEFAULT_ANNUAL_VOL,
)


# ── 纯函数：已实现年化波动率 ──────────────────────────────
def test_flat_prices_zero_vol():
    """价格完全不动 → 波动率 0。"""
    assert realized_annual_vol([100.0] * 30) == 0.0


def test_too_few_points_returns_none():
    """样本不足 → None（无法估计，交由调用方降级）。"""
    assert realized_annual_vol([100.0, 101.0]) is None
    assert realized_annual_vol([]) is None


def test_known_series_matches_manual_calc():
    """对一段已知收益序列，年化波动率应等于手算值。"""
    # 构造 daily closes；手算对数收益标准差 * sqrt(365)
    closes = [100, 102, 101, 103, 105, 104, 106, 108, 107, 110]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)  # 样本方差
    expected = math.sqrt(var) * math.sqrt(365.0)
    got = realized_annual_vol(closes)
    assert got is not None
    assert abs(got - expected) < 1e-9


def test_higher_volatility_series_gives_higher_vol():
    calm = realized_annual_vol([100, 100.5, 100.2, 100.6, 100.3, 100.7])
    wild = realized_annual_vol([100, 110, 95, 112, 90, 115])
    assert wild > calm > 0


# ── annual_vol：失败降级到常数 ────────────────────────────
def test_annual_vol_falls_back_to_constant_on_fetch_fail(monkeypatch):
    src = CryptoPriceSource()
    # 让 K 线拉取失败 → 应降级到 DEFAULT_ANNUAL_VOL
    monkeypatch.setattr(src, "_fetch_daily_closes", lambda asset: None)
    assert src.annual_vol("bitcoin") == DEFAULT_ANNUAL_VOL["bitcoin"]
    assert src.annual_vol("unknown_coin") == 0.60  # 未知资产的常数兜底


def test_annual_vol_uses_calibrated_when_available(monkeypatch):
    src = CryptoPriceSource()
    # 注入一段已知 closes → annual_vol 应返回校准值而非常数
    closes = [100, 110, 95, 112, 90, 115, 88, 120]
    monkeypatch.setattr(src, "_fetch_daily_closes", lambda asset: closes)
    expected = realized_annual_vol(closes)
    assert abs(src.annual_vol("bitcoin") - expected) < 1e-9
    assert src.annual_vol("bitcoin") != DEFAULT_ANNUAL_VOL["bitcoin"]


def test_annual_vol_caches(monkeypatch):
    """校准结果应缓存，避免每个市场都拉一次 K 线。"""
    src = CryptoPriceSource()
    calls = {"n": 0}
    def fake(asset):
        calls["n"] += 1
        return [100, 110, 95, 112, 90]
    monkeypatch.setattr(src, "_fetch_daily_closes", fake)
    src.annual_vol("bitcoin")
    src.annual_vol("bitcoin")
    assert calls["n"] == 1  # 第二次命中缓存
