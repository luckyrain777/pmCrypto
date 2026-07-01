"""信号③：买卖盘失衡。

edge 假设：盘口一侧深度远大于另一侧，反映短期买/卖压力方向，可能领先价格。
bid 深度 >> ask 深度 → 买压强 → 概率上修；反之下修。

输入：当前结果的 bid_size / ask_size（用名义额更合理，但份额亦可近似）。
输出：delta 与失衡方向同向，confidence 随失衡强度与总深度。
"""
from __future__ import annotations

from strategy.signals.base import SignalOutput


NAME = "book_imbalance"


def compute(bid_notional: float, ask_notional: float, *,
            min_total: float = 10.0, scale: float = 0.1) -> SignalOutput:
    """
    bid_notional/ask_notional: 买/卖侧名义可成交额（USDC）。
    min_total: 总深度低于此值视为噪声，不判断。
    scale:     失衡比例映射到 delta 的系数（保守，失衡不等于必然成交）。
    """
    total = bid_notional + ask_notional
    if total < min_total:
        return SignalOutput.neutral(NAME)

    # 失衡比 ∈ [-1, 1]：+1 全是买盘，-1 全是卖盘。
    imbalance = (bid_notional - ask_notional) / total
    delta = imbalance * scale
    # 置信度：失衡越极端、总深度越大越可信（深度用对数封顶）。
    import math
    depth_factor = min(math.log10(total + 1) / 3.0, 1.0)  # ~1000 USDC 时接近 1
    confidence = abs(imbalance) * depth_factor
    return SignalOutput(delta=delta, confidence=confidence, name=NAME)
