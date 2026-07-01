"""信号②：深度-价格背离（成交量背离的代理实现）。

edge 假设：有资金在建仓时，盘口深度/挂单会先增长，而价格尚未反应 → 领先信号。
理想输入是真实成交量，但当前快照未含成交量，故用“盘口总深度的增长”作代理。

诚实标注：这是代理指标，不如真实成交量精确。若后续 client 能提供成交量，
应替换本信号的输入而无需改动下游（信号接口不变）。

输入：某结果的 (总深度序列, 中间价序列)，时间升序。
输出：深度显著放大但价格几乎不动 → 低置信度的“蓄势”信号（方向取近期微弱倾向）。
"""
from __future__ import annotations

from strategy.signals.base import SignalOutput


NAME = "volume_divergence"


def compute(depth_series: list[float], mid_series: list[float], *,
            lookback: int = 5, surge_ratio: float = 1.5,
            scale: float = 0.05) -> SignalOutput:
    """
    depth_series: 盘口总深度（USDC）序列，升序。
    mid_series:   同期中间价序列，升序。
    surge_ratio:  近期深度相对更早期深度的放大倍数阈值。
    scale:        映射系数（保守）。
    """
    depths = [d for d in depth_series if d is not None]
    mids = [m for m in mid_series if m is not None]
    if len(depths) < lookback or len(mids) < lookback:
        return SignalOutput.neutral(NAME)

    recent = depths[-lookback:]
    early = depths[:-lookback] or depths[:1]
    recent_avg = sum(recent) / len(recent)
    early_avg = sum(early) / len(early)
    if early_avg <= 0:
        return SignalOutput.neutral(NAME)

    surge = recent_avg / early_avg
    price_move = abs(mids[-1] - mids[-lookback])

    # 背离条件：深度明显放大(surge≥阈值) 且 价格几乎没动。
    if surge < surge_ratio or price_move > 0.02:
        return SignalOutput.neutral(NAME)

    # 方向：取窗口内价格的微弱倾向（哪怕很小）。
    micro_dir = mids[-1] - mids[-lookback]
    direction = 1.0 if micro_dir >= 0 else -1.0
    delta = direction * scale * min(surge - 1.0, 1.0)
    # 置信度：放量越猛越可信，但因是代理指标，整体压低（乘 0.6）。
    confidence = min((surge - surge_ratio) / surge_ratio, 1.0) * 0.6
    confidence = max(confidence, 0.0)
    return SignalOutput(delta=delta, confidence=confidence, name=NAME)
