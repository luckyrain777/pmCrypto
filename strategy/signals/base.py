"""统计信号的公共类型。

每个信号接收“同一市场、同一结果”的历史快照序列（时间升序，最新在末尾），
输出一个 SignalOutput：对市场报价的概率修正量 delta 及其置信度。

约定：
- delta > 0 表示信号认为“真实概率高于当前报价”（看涨该结果）。
- delta < 0 表示看跌。
- confidence ∈ [0,1]：信号对本次判断的把握，用于加权合成与自适应仓位。
- 数据不足/无法判断时返回 neutral()（delta=0, confidence=0），不参与合成。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalOutput:
    delta: float          # 概率修正量，可正可负
    confidence: float     # [0,1]
    name: str = ""        # 信号名，便于调试/记录

    @staticmethod
    def neutral(name: str = "") -> "SignalOutput":
        return SignalOutput(delta=0.0, confidence=0.0, name=name)


def mid_price(best_ask, best_bid):
    """中间价：有买卖价取均值；只有一边取那一边；都无返回 None。"""
    if best_ask is not None and best_bid is not None:
        return (best_ask + best_bid) / 2.0
    return best_ask if best_ask is not None else best_bid
