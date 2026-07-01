"""信号合成：把多个统计信号合成为对某结果的估计真实概率 p。

p = clamp( q + Σ_i (weight_i × confidence_i × delta_i),  0, 1 )

其中 q 是市场当前报价（中间价）。每个信号的贡献按 (权重 × 置信度) 加权——
置信度低的信号自动被压小，数据不足的信号(confidence=0)不参与。

权重初期取配置默认值；后续由 edge 回测校准（这是“权重不是拍脑袋”的兑现点）。
合成同时输出一个总体置信度，供自适应 Kelly 使用。
"""
from __future__ import annotations

from dataclasses import dataclass

from strategy.signals.base import SignalOutput


# 默认权重（回测校准前的先验）。可在 config 覆盖。
DEFAULT_WEIGHTS = {
    "momentum": 1.0,
    "book_imbalance": 1.0,
    "volume_divergence": 0.6,   # 代理指标，先验权重压低
}


@dataclass(frozen=True)
class CombinedEstimate:
    q: float               # 市场报价（中间价）
    p: float               # 合成后的估计真实概率
    edge: float            # p - q
    confidence: float      # 总体置信度 [0,1]
    contributions: tuple[tuple[str, float], ...]  # 各信号加权贡献，便于调试


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def combine(q: float, signals: list[SignalOutput],
            weights: dict[str, float] | None = None) -> CombinedEstimate:
    w = weights or DEFAULT_WEIGHTS
    total_adjust = 0.0
    contributions = []
    conf_num = 0.0
    conf_den = 0.0
    for s in signals:
        wi = w.get(s.name, 0.0)
        contrib = wi * s.confidence * s.delta
        total_adjust += contrib
        contributions.append((s.name, contrib))
        # 总体置信度：按权重加权平均各信号置信度。
        conf_num += wi * s.confidence
        conf_den += wi
    p = _clamp01(q + total_adjust)
    confidence = (conf_num / conf_den) if conf_den > 0 else 0.0
    return CombinedEstimate(
        q=q,
        p=p,
        edge=p - q,
        confidence=confidence,
        contributions=tuple(contributions),
    )
