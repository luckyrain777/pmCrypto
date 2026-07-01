"""风控闸门。

任何机会在变成“建议下单(Signal)”前必须过这里。两个职责：
  1) 急停检查：core.state 处于 HALTED 时，一律不产生任何信号。
  2) 仓位约束：根据账户基数与风控参数，给出建议名义下注额，并受
     单笔上限、总在场仓位上限、可成交深度共同约束。

第一阶段 C 不真发单，guard 起“过滤 + 建议仓位”作用；切 A 后这套闸门
立即变成真正的刹车，无需重写。
"""
from __future__ import annotations

from dataclasses import dataclass

from config import CONFIG
from core.state import STATE
from strategy.models import Opportunity, Signal
from strategy.pricing import evaluate
from strategy.kelly import compute_stake


@dataclass
class RiskGuard:
    """有状态的风控：跟踪当前在场总仓位，施加上限。"""
    account_balance_usdc: float
    current_exposure_usdc: float = 0.0

    def _max_single_usdc(self) -> float:
        return self.account_balance_usdc * CONFIG.max_position_pct

    def _remaining_exposure_usdc(self) -> float:
        cap = self.account_balance_usdc * CONFIG.max_total_exposure_pct
        return max(cap - self.current_exposure_usdc, 0.0)

    def suggest_size_usdc(self, opp: Opportunity) -> float:
        """给出建议名义下注额，取以下三者最小值：
        单笔上限 / 剩余可用总仓位 / 该机会最薄腿可成交深度。
        """
        return min(
            self._max_single_usdc(),
            self._remaining_exposure_usdc(),
            opp.min_leg_notional_usdc,
        )

    def assess(self, opp: Opportunity) -> Signal | None:
        """对一个候选机会做完整风控 + 定价评估，产出 Signal 或 None。

        返回 None 的情形：系统急停 / 无可用仓位 / 定价后不划算。
        """
        # 1) 全局急停优先：HALTED 时绝不产生任何交易信号。
        if STATE.is_halted:
            return None

        # 2) 先定建议仓位（不依赖定价）。
        size = self.suggest_size_usdc(opp)
        if size <= 0:
            return None  # 没有可用仓位额度

        # 3) 用该仓位评估净收益（滑点依赖成交额）。
        priced = evaluate(opp, target_notional_usdc=size)
        if not priced.tradable:
            return None  # 不划算 / 流动性不足

        return Signal(
            market_id=opp.market_id,
            kind=opp.kind,
            raw_edge=opp.raw_edge,
            net_edge=priced.net_edge,
            suggested_size_usdc=round(size, 2),
            legs=opp.legs,
            reason=priced.reason,
            snapshot_ts=opp.snapshot_ts,
            question=opp.question,
        )

    def assess_edge(self, opp: Opportunity) -> Signal | None:
        """阶段 B：对方向性 edge 机会用自适应 Kelly 定仓位，产出 Signal 或 None。

        复利核心：Kelly 按 self.account_balance_usdc（当前实时余额）算，
        赢了余额变大注变大、输了注变小，全自动。
        """
        if STATE.is_halted:
            return None

        sizing = compute_stake(
            p=opp.estimated_p,
            q=opp.legs[0][1],           # 买入价
            confidence=opp.confidence,
            balance_usdc=self.account_balance_usdc,
            remaining_exposure_usdc=self._remaining_exposure_usdc(),
            leg_liquidity_usdc=opp.min_leg_notional_usdc,
            min_edge=CONFIG.edge_min_threshold,
            kelly_min=CONFIG.kelly_fraction_min,
            kelly_max=CONFIG.kelly_fraction_max,
            max_single_pct=CONFIG.kelly_max_single_pct,
        )
        if sizing.stake_usdc <= 0:
            return None

        return Signal(
            market_id=opp.market_id,
            kind=opp.kind,
            raw_edge=opp.raw_edge,
            net_edge=opp.raw_edge,       # 方向性用 edge 本身作净收益近似
            suggested_size_usdc=sizing.stake_usdc,
            legs=opp.legs,
            reason=sizing.reason,
            snapshot_ts=opp.snapshot_ts,
            question=opp.question,
        )

    def reserve(self, size_usdc: float) -> None:
        """登记一笔已建议/已建仓的名义额，占用总仓位额度。

        阶段 C 是否调用取决于是否要模拟“已提示即占用”。切 A 后由执行器在
        真实建仓后调用，以维持总仓位上限的准确性。
        """
        self.current_exposure_usdc += size_usdc

    def release(self, size_usdc: float) -> None:
        """平仓后释放占用的仓位额度。"""
        self.current_exposure_usdc = max(self.current_exposure_usdc - size_usdc, 0.0)
