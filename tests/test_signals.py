"""统计信号 + combiner 单元测试。"""
from strategy.signals import momentum, book_imbalance, volume_divergence
from strategy.signals.base import SignalOutput
from strategy.signals.combiner import combine


# ── momentum ──────────────────────────────────────────────
def test_momentum_uptrend_positive_delta():
    s = momentum.compute([0.30, 0.32, 0.34, 0.36, 0.38])
    assert s.delta > 0
    assert s.confidence > 0


def test_momentum_downtrend_negative_delta():
    s = momentum.compute([0.50, 0.48, 0.46, 0.44, 0.42])
    assert s.delta < 0


def test_momentum_insufficient_data_neutral():
    s = momentum.compute([0.5])
    assert s.confidence == 0.0
    assert s.delta == 0.0


def test_momentum_noisy_low_confidence():
    # 来回震荡 → 一致性低 → 置信度低
    noisy = momentum.compute([0.50, 0.55, 0.49, 0.54, 0.50])
    clean = momentum.compute([0.50, 0.52, 0.54, 0.56, 0.58])
    assert noisy.confidence < clean.confidence


# ── book_imbalance ────────────────────────────────────────
def test_book_imbalance_buy_pressure_positive():
    s = book_imbalance.compute(bid_notional=900.0, ask_notional=100.0)
    assert s.delta > 0
    assert s.confidence > 0


def test_book_imbalance_sell_pressure_negative():
    s = book_imbalance.compute(bid_notional=100.0, ask_notional=900.0)
    assert s.delta < 0


def test_book_imbalance_thin_neutral():
    s = book_imbalance.compute(bid_notional=2.0, ask_notional=3.0)
    assert s.confidence == 0.0


# ── volume_divergence ─────────────────────────────────────
def test_volume_divergence_surge_flat_price():
    # 深度后期显著放大，价格几乎不动 → 触发
    depth = [100, 100, 100, 100, 100, 200, 220, 210, 205, 215]
    mids = [0.50] * 10
    s = volume_divergence.compute(depth, mids)
    assert s.confidence > 0


def test_volume_divergence_no_surge_neutral():
    depth = [100] * 10
    mids = [0.50] * 10
    s = volume_divergence.compute(depth, mids)
    assert s.confidence == 0.0


# ── combiner ──────────────────────────────────────────────
def test_combine_shifts_p_toward_signals():
    q = 0.40
    signals = [
        SignalOutput(delta=0.10, confidence=1.0, name="momentum"),
        SignalOutput(delta=0.05, confidence=0.5, name="book_imbalance"),
    ]
    est = combine(q, signals)
    assert est.p > q          # 正 delta 推高 p
    assert est.edge == est.p - q
    assert 0.0 <= est.p <= 1.0


def test_combine_clamps_to_01():
    est = combine(0.95, [SignalOutput(delta=0.5, confidence=1.0, name="momentum")])
    assert est.p <= 1.0


def test_combine_zero_confidence_no_shift():
    est = combine(0.40, [SignalOutput(delta=0.9, confidence=0.0, name="momentum")])
    assert abs(est.p - 0.40) < 1e-9   # 置信0不影响
