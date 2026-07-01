"""回测引擎：用历史快照回放，验证策略方向性。

把 store 里按时间排序的历史 Market 逐条喂给 **同一套** detector + guard，
统计：发现多少候选机会、多少通过风控成为可下注信号、净收益分布如何。

⚠️ 诚实声明（设计文档第 9 节缺口 A）：
  - 快照是离散抽样，两次快照之间的瞬时机会看不到，结果会高/低估可成交性。
  - 因此回测结论仅作“策略方向性参考”，**不作必然盈利证明**。
  - 切到全自动前，回测之外还必须再过一道“小额实盘灰度”。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import CONFIG
from data.store import Store
from risk.guard import RiskGuard
from strategy import detector


@dataclass
class BacktestResult:
    markets_replayed: int = 0
    opportunities_found: int = 0
    signals_generated: int = 0
    net_edges: list[float] = field(default_factory=list)

    @property
    def avg_net_edge(self) -> float:
        if not self.net_edges:
            return 0.0
        return sum(self.net_edges) / len(self.net_edges)

    @property
    def max_net_edge(self) -> float:
        return max(self.net_edges) if self.net_edges else 0.0

    def summary(self) -> str:
        return (
            f"回测回放市场 {self.markets_replayed} 条 | "
            f"发现候选机会 {self.opportunities_found} 个 | "
            f"通过风控信号 {self.signals_generated} 个 | "
            f"平均净收益 {self.avg_net_edge:.4f} | "
            f"最大净收益 {self.max_net_edge:.4f}\n"
            f"[注意] 仅作方向性参考，非盈利保证；切全自动前还需小额灰度。"
        )


def run_backtest(store: Store, account_balance_usdc: float | None = None) -> BacktestResult:
    """对 store 中全部历史快照跑一遍策略，返回统计结果。

    回测中每条市场用独立的 guard（不累积仓位），评估“若此刻出现该机会、
    在风控约束下是否可下注、净收益多少”。
    """
    balance = account_balance_usdc or CONFIG.account_balance_usdc
    result = BacktestResult()

    for market in store.replay_markets():
        result.markets_replayed += 1
        opps = detector.detect(market)
        result.opportunities_found += len(opps)
        # 每条快照用全新 guard，避免历史回放中仓位错误累积。
        guard = RiskGuard(account_balance_usdc=balance)
        for opp in opps:
            signal = guard.assess(opp)
            if signal is not None:
                result.signals_generated += 1
                result.net_edges.append(signal.net_edge)

    return result
