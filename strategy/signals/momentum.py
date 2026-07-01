"""信号①：价格动量。

edge 假设：预测市场信息逐步扩散，价格朝某方向持续移动时往往还没走完。
持续同向且加速的价格 → 顺势修正概率。

输入：某结果的历史中间价序列（时间升序）。
输出：delta 与价格斜率同向、大小随斜率强度，confidence 随一致性。
"""
from __future__ import annotations

from strategy.signals.base import SignalOutput


NAME = "momentum"


def compute(mid_prices: list[float], *, lookback: int = 5,
            scale: float = 1.0) -> SignalOutput:
    """
    mid_prices: 中间价序列（升序，最新在末尾），元素为 0~1。
    lookback:   用最近多少个点估斜率。
    scale:      把“每步价格变化”映射到 delta 的系数。
    """
    pts = [p for p in mid_prices if p is not None]
    if len(pts) < max(3, lookback // 2 + 1):
        return SignalOutput.neutral(NAME)

    window = pts[-lookback:] if len(pts) >= lookback else pts
    n = len(window)

    # 简单线性斜率：末点减首点，按步数归一。
    slope = (window[-1] - window[0]) / (n - 1)

    # 一致性：窗口内同向步数占比 → 置信度。噪声反转会拉低它。
    diffs = [window[i + 1] - window[i] for i in range(n - 1)]
    if not diffs:
        return SignalOutput.neutral(NAME)
    same_dir = sum(1 for d in diffs if (d > 0) == (slope > 0) and d != 0)
    consistency = same_dir / len(diffs)

    delta = slope * scale
    # 置信度：一致性 × 斜率显著性（斜率越大越可信，封顶）。
    magnitude = min(abs(slope) * 20.0, 1.0)
    confidence = consistency * magnitude
    return SignalOutput(delta=delta, confidence=confidence, name=NAME)
