"""edge 验证报告 —— 阶段 B 的科学命门。

用积累的历史快照 + 真实结算结果，回答唯一重要的问题：
    我们的方向性信号，到底有没有正 edge？

方法：
  1) 遍历每个已结算市场的历史快照序列。
  2) 在每个时点用 edge_detector 判断“是否下注、下注哪个结果”。
  3) 用真实结算结果结算每笔：赢则赚 (1 - 买入价)，输则亏 (买入价)。
  4) 汇总：下注数、胜率、累计/平均单笔收益、edge 的置信区间。
  5) 判据：平均单笔收益的 95% 置信区间下界 > 0 → edge 显著为正 → 可上真钱。

诚实边界（设计文档缺口 A）：
  - 快照离散抽样，会高/低估可成交性；结论仅作方向性参考。
  - 样本不足时置信区间很宽，会诚实地判为“证据不足，别上真钱”。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from data.store import Store
from strategy.edge_detector import detect_edge


@dataclass
class EdgeReport:
    bets: int = 0
    wins: int = 0
    per_bet_returns: list[float] = field(default_factory=list)  # 每笔单位注收益率
    markets_evaluated: int = 0
    markets_with_resolution: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.bets if self.bets else 0.0

    @property
    def mean_return(self) -> float:
        if not self.per_bet_returns:
            return 0.0
        return sum(self.per_bet_returns) / len(self.per_bet_returns)

    @property
    def std_return(self) -> float:
        n = len(self.per_bet_returns)
        if n < 2:
            return 0.0
        mu = self.mean_return
        var = sum((r - mu) ** 2 for r in self.per_bet_returns) / (n - 1)
        return math.sqrt(var)

    @property
    def ci95(self) -> tuple[float, float]:
        """平均单笔收益的 95% 置信区间（正态近似）。"""
        n = len(self.per_bet_returns)
        if n < 2:
            return (float("-inf"), float("inf"))
        se = self.std_return / math.sqrt(n)
        margin = 1.96 * se
        return (self.mean_return - margin, self.mean_return + margin)

    @property
    def edge_significantly_positive(self) -> bool:
        """判据：CI 下界 > 0 且样本量足够。"""
        lo, _ = self.ci95
        return self.bets >= 30 and lo > 0.0

    def summary(self) -> str:
        lo, hi = self.ci95
        verdict = ("edge 显著为正 ✓ 可进入小额灰度"
                   if self.edge_significantly_positive
                   else "证据不足/edge 不显著 ✗ 不可上真钱")
        return (
            f"评估市场 {self.markets_evaluated}（含结算 {self.markets_with_resolution}）\n"
            f"下注 {self.bets} 笔 | 胜率 {self.win_rate:.1%} | "
            f"平均单笔收益 {self.mean_return:+.4f} | 标准差 {self.std_return:.4f}\n"
            f"平均收益 95% 置信区间 [{lo:+.4f}, {hi:+.4f}]\n"
            f"判定：{verdict}"
        ).replace("✓", "[OK]").replace("✗", "[NO]")


def run_edge_report(store: Store, *, min_history: int = 3) -> EdgeReport:
    """对所有已结算市场跑 edge 验证。"""
    report = EdgeReport()
    resolutions = store.all_resolutions()

    for market_id in store.distinct_market_ids():
        report.markets_evaluated += 1
        winner = resolutions.get(market_id)
        if winner is None:
            continue  # 无结算结果，无法验证这笔
        report.markets_with_resolution += 1

        history = store.market_history(market_id, limit=500)
        if len(history) < min_history:
            continue

        # 统计独立性修复：每个市场只贡献【1】个独立下注样本。
        # 递增前缀会让同一市场在多个时点反复检出同一信号——这些样本高度
        # 自相关（伪重复/pseudoreplication），若都计入，用独立同分布假设算的
        # 95% 置信区间会被严重低估，让假 edge 通过验证。
        # 取【第一个触发信号的时点】：最早发现错价的决策点，最贴近实盘会怎么下，
        # 且不同市场之间相互独立。
        first_opp = None
        for end in range(min_history, len(history) + 1):
            opps = detect_edge(history[:end])
            if opps:
                first_opp = opps[0]
                break

        if first_opp is None:
            continue  # 该市场从未触发信号，不贡献样本

        bought_token = first_opp.legs[0][0]
        buy_price = first_opp.legs[0][1]
        won = (bought_token == winner)
        # 单位注收益率：赢 (1 - buy_price)，输 -buy_price。
        ret = (1.0 - buy_price) if won else (-buy_price)
        report.bets += 1
        report.wins += 1 if won else 0
        report.per_bet_returns.append(ret)

    return report
