"""核心数据结构。

策略层只认这些标准化对象，不关心数据从哪来（实时 API 还是历史快照）。
这是“策略与数据解耦”的关键：同一套 detector/pricing 既能跑实盘也能跑回测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class OutcomeBook:
    """单个结果（outcome）的盘口快照。

    在 Polymarket 中，一个市场由若干互斥结果组成（如 Yes/No，或多候选）。
    每个结果都是一个可独立买卖的 token。
    """
    outcome: str            # 结果名称，如 "Yes" / "No" / 候选人名
    token_id: str           # 该结果对应的 CLOB token id
    best_ask: Optional[float] = None   # 最优卖价（你买入的价格），0~1
    best_bid: Optional[float] = None   # 最优买价（你卖出的价格），0~1
    ask_size: float = 0.0   # 最优卖价处可成交数量（份额）
    bid_size: float = 0.0   # 最优买价处可成交数量（份额）

    @property
    def ask_notional_usdc(self) -> float:
        """最优卖价档位的名义可成交金额（USDC）。份额 × 单价。"""
        if self.best_ask is None:
            return 0.0
        return self.ask_size * self.best_ask


@dataclass(frozen=True)
class Market:
    """一个市场的标准化快照，含其所有结果的盘口。"""
    market_id: str          # 市场唯一标识（condition_id 或 slug）
    question: str           # 市场问题文本
    outcomes: tuple[OutcomeBook, ...]   # 所有互斥结果
    snapshot_ts: float = 0.0            # 快照时间戳（epoch 秒），回测靠它排序
    end_ts: float = 0.0                 # 市场到期时间（epoch 秒），0=未知；加密定价需要

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2


class OpportunityKind(str, Enum):
    YES_NO_COMPLEMENT = "yes_no_complement"      # Yes/No 互补偏差（阶段A套利）
    MUTEX_PROB_SUM = "mutex_prob_sum"            # 互斥多结果概率和偏差（阶段A套利）
    EDGE_DIRECTIONAL = "edge_directional"        # 方向性误定价（阶段B，单腿+Kelly）


@dataclass(frozen=True)
class Opportunity:
    """detector 发现的一个候选机会（尚未扣成本）。

    阶段A套利：raw_edge=名义偏差，legs=多腿。
    阶段B方向性：raw_edge=p-q，legs=单腿(要买的结果)，并带 estimated_p/confidence。
    """
    market_id: str
    kind: OpportunityKind
    raw_edge: float
    legs: tuple[tuple[str, float], ...]
    min_leg_notional_usdc: float
    snapshot_ts: float = 0.0
    # 阶段B专用：合成估计概率与置信度（阶段A留默认）。
    estimated_p: float = 0.0
    confidence: float = 0.0
    # 市场问题文本快照：产生机会时一并存下，面板显示名字而非 0x 地址。
    question: str = ""


@dataclass(frozen=True)
class Signal:
    """经 pricing + guard 后产生的最终建议（阶段C只提示，不发单）。"""
    market_id: str
    kind: OpportunityKind
    raw_edge: float          # 名义偏差
    net_edge: float          # 扣成本后净收益（比例）
    suggested_size_usdc: float   # 风控给出的建议下注名义金额
    legs: tuple[tuple[str, float], ...]
    reason: str = ""         # 说明文本（为什么提示/被风控如何缩仓）
    snapshot_ts: float = 0.0
    # 市场问题文本快照：由 opportunity 透传，面板显示名字而非 0x 地址。
    question: str = ""
