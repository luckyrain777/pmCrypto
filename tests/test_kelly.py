"""自适应 Kelly 单元测试：已知 p/q/置信度 → 已知仓位。"""
from strategy.kelly import compute_stake, adaptive_kelly_fraction


def _stake(**kw):
    base = dict(
        p=0.60, q=0.50, confidence=1.0,
        balance_usdc=100.0, remaining_exposure_usdc=1000.0,
        leg_liquidity_usdc=1000.0, min_edge=0.05,
        kelly_min=0.25, kelly_max=0.50, max_single_pct=0.20,
    )
    base.update(kw)
    return compute_stake(**base)


def test_adaptive_fraction_scales_with_confidence():
    assert adaptive_kelly_fraction(0.0, kelly_min=0.25, kelly_max=0.50) == 0.25
    assert adaptive_kelly_fraction(1.0, kelly_min=0.25, kelly_max=0.50) == 0.50
    assert adaptive_kelly_fraction(0.5, kelly_min=0.25, kelly_max=0.50) == 0.375


def test_no_bet_below_min_edge():
    # edge = 0.52-0.50 = 0.02 < 0.05 门槛
    s = _stake(p=0.52, q=0.50)
    assert s.stake_usdc == 0.0
    assert "不出手" in s.reason


def test_kelly_formula_full_confidence():
    # p=0.60,q=0.50 → f* = 0.10/0.50 = 0.20；置信1.0→分数0.50→f_kelly=0.10
    # f_final = min(0.10, 0.20封顶) = 0.10；注 = 100*0.10 = 10
    s = _stake(p=0.60, q=0.50, confidence=1.0)
    assert abs(s.f_star - 0.20) < 1e-9
    assert abs(s.kelly_fraction - 0.50) < 1e-9
    assert abs(s.stake_usdc - 10.0) < 1e-6


def test_low_confidence_smaller_stake():
    # 置信0 → 分数0.25 → f_kelly=0.05 → 注=5
    s = _stake(p=0.60, q=0.50, confidence=0.0)
    assert abs(s.stake_usdc - 5.0) < 1e-6


def test_single_cap_enforced():
    # 极大 edge 会让 f_kelly 超 20%，被硬封顶
    # p=0.90,q=0.50 → f*=0.40/0.50=0.80；置信1→分数0.5→f_kelly=0.40>0.20→封顶0.20
    s = _stake(p=0.90, q=0.50, confidence=1.0)
    assert abs(s.f_final - 0.20) < 1e-9
    assert abs(s.stake_usdc - 20.0) < 1e-6


def test_compounding_via_balance():
    # 复利核心：注随余额变。同参数，余额翻倍→注翻倍。
    s1 = _stake(balance_usdc=100.0)
    s2 = _stake(balance_usdc=200.0)
    assert abs(s2.stake_usdc - 2 * s1.stake_usdc) < 1e-6


def test_liquidity_caps_stake():
    # 深度只有 3 → 注被削到 3
    s = _stake(p=0.90, q=0.50, leg_liquidity_usdc=3.0)
    assert abs(s.stake_usdc - 3.0) < 1e-6


def test_invalid_q_rejected():
    assert _stake(p=0.99, q=1.0).stake_usdc == 0.0
    assert _stake(p=0.5, q=0.0).stake_usdc == 0.0
