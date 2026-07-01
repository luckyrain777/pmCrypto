"""detector 单元测试：已知盘口 → 已知偏差判定。"""
from strategy.models import Market, OutcomeBook
from strategy.detector import (
    detect,
    detect_yes_no_complement,
    detect_mutex_prob_sum,
)


def _binary_market(yes_ask, no_ask, size=1000.0, mid="m1"):
    return Market(
        market_id=mid,
        question="测试市场",
        outcomes=(
            OutcomeBook("Yes", "tok_yes", best_ask=yes_ask, best_bid=None, ask_size=size),
            OutcomeBook("No", "tok_no", best_ask=no_ask, best_bid=None, ask_size=size),
        ),
        snapshot_ts=100.0,
    )


def test_yes_no_complement_detects_edge():
    # yes 0.45 + no 0.50 = 0.95 < 1 → 偏差 0.05
    m = _binary_market(0.45, 0.50)
    opp = detect_yes_no_complement(m)
    assert opp is not None
    assert abs(opp.raw_edge - 0.05) < 1e-9


def test_yes_no_no_edge_when_sum_ge_one():
    # 0.55 + 0.50 = 1.05 ≥ 1 → 无机会
    m = _binary_market(0.55, 0.50)
    assert detect_yes_no_complement(m) is None


def test_yes_no_requires_both_asks():
    # 缺一腿的 ask → 无法套利
    m = _binary_market(0.45, None)
    assert detect_yes_no_complement(m) is None


def test_min_leg_notional_uses_thinnest_leg():
    # yes 深度 0.45*100=45，no 深度 0.50*200=100 → 最薄取 45
    m = Market(
        market_id="m2", question="q",
        outcomes=(
            OutcomeBook("Yes", "ty", best_ask=0.45, ask_size=100.0),
            OutcomeBook("No", "tn", best_ask=0.50, ask_size=200.0),
        ),
        snapshot_ts=1.0,
    )
    opp = detect_yes_no_complement(m)
    assert abs(opp.min_leg_notional_usdc - 45.0) < 1e-9


def test_mutex_prob_sum_detects_edge():
    # 三结果 0.30+0.30+0.30=0.90 < 1 → 偏差 0.10
    m = Market(
        market_id="m3", question="三选一",
        outcomes=(
            OutcomeBook("A", "ta", best_ask=0.30, ask_size=500.0),
            OutcomeBook("B", "tb", best_ask=0.30, ask_size=500.0),
            OutcomeBook("C", "tc", best_ask=0.30, ask_size=500.0),
        ),
        snapshot_ts=1.0,
    )
    opp = detect_mutex_prob_sum(m)
    assert opp is not None
    assert abs(opp.raw_edge - 0.10) < 1e-9
    assert len(opp.legs) == 3


def test_mutex_skips_binary():
    m = _binary_market(0.30, 0.30)
    assert detect_mutex_prob_sum(m) is None


def test_detect_runs_all():
    m = _binary_market(0.45, 0.50)
    opps = detect(m)
    assert len(opps) == 1  # 只命中 yes/no
