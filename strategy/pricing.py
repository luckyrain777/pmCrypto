"""净收益计算。

把 detector 给出的名义偏差(raw_edge)扣掉所有成本，得到 net_edge。
只有 net_edge 超过阈值的机会才值得提示。

成本三项：
  ① 手续费：按 fee_rate，对每条腿的名义成交额收取。
  ② 滑点：你想成交的量越大、盘口越薄，吃进的均价越差。这里按可成交深度
     估算，并乘以 slippage_safety_factor **强制保守**——宁可少算机会，
     也不可高估收益（高估收益 = 拿真钱去亏）。
  ③ 流动性过滤：最薄一档名义量低于 min_liquidity_usdc 直接判不可成交。

诚实声明：滑点是“估”的。真实下单时盘口在变、还有别的机器人抢单，所以这里
刻意保守。小额灰度阶段应用“实际成交价 vs 估算价”反向校准本模型。
"""
from __future__ import annotations

from dataclasses import dataclass

from config import CONFIG
from strategy.models import Opportunity


@dataclass(frozen=True)
class PricedOpportunity:
    """扣成本后的机会评估结果。"""
    opp: Opportunity
    net_edge: float          # 扣成本后净收益（比例）
    tradable: bool           # 是否通过流动性/阈值过滤
    reason: str              # 判定说明


def _slippage_cost(opp: Opportunity, target_notional_usdc: float) -> float:
    """估算滑点成本（占名义额比例），强制保守。

    模型（保守且简单）：若想成交的名义额 target 超过最薄一档可成交量
    min_leg_notional，则按超出比例线性惩罚；再乘安全系数放大。
    当 target 远小于深度时，滑点趋近 0。

    返回值是“成本占名义额的比例”，与 raw_edge 同量纲，可直接相减。
    """
    depth = max(opp.min_leg_notional_usdc, 1e-9)
    # 需求/深度 比值越大，滑点越重。clamp 到 [0, 1] 再放大。
    fill_ratio = min(target_notional_usdc / depth, 1.0)
    # 基础滑点：用半个 fill_ratio 作为均价恶化的粗略估计。
    base_slip = 0.5 * fill_ratio
    return base_slip * CONFIG.slippage_safety_factor


def evaluate(opp: Opportunity, target_notional_usdc: float) -> PricedOpportunity:
    """对一个机会按拟下注名义额评估净收益。

    target_notional_usdc：打算在这个机会上投入的名义金额（由 guard 决定）。
    """
    # 流动性硬过滤：最薄腿太浅，直接不可成交。
    if opp.min_leg_notional_usdc < CONFIG.min_liquidity_usdc:
        return PricedOpportunity(
            opp=opp,
            net_edge=0.0,
            tradable=False,
            reason=(
                f"流动性不足：最薄腿 {opp.min_leg_notional_usdc:.2f} USDC "
                f"< 阈值 {CONFIG.min_liquidity_usdc:.2f}"
            ),
        )

    fee = CONFIG.fee_rate * len(opp.legs)          # 每条腿都收一次手续费
    slip = _slippage_cost(opp, target_notional_usdc)
    net_edge = opp.raw_edge - fee - slip

    if net_edge < CONFIG.min_profit_threshold:
        return PricedOpportunity(
            opp=opp,
            net_edge=net_edge,
            tradable=False,
            reason=(
                f"净收益 {net_edge:.4f} < 阈值 {CONFIG.min_profit_threshold:.4f}"
                f"（毛偏差 {opp.raw_edge:.4f} − 费 {fee:.4f} − 滑点 {slip:.4f}）"
            ),
        )

    return PricedOpportunity(
        opp=opp,
        net_edge=net_edge,
        tradable=True,
        reason=(
            f"可提示：净收益 {net_edge:.4f}"
            f"（毛偏差 {opp.raw_edge:.4f} − 费 {fee:.4f} − 滑点 {slip:.4f}）"
        ),
    )
