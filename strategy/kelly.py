"""自适应分数 Kelly 仓位计算（纯函数，易测）。

Polymarket 二元赌局：花 q 买某结果，赢变 1（净赚 1-q），输变 0（亏 q）。
Kelly 最优下注比例（占当前余额）：

    f* = (p - q) / (1 - q)        # 即 edge / (1 - q)

大胆修正①——自适应 Kelly 分数：
    固定 ¼ Kelly 太胆小。这里让 Kelly 分数随“对 edge 的信心”在
    [kelly_min, kelly_max] 间线性放大：置信度越高、下注越接近满 Kelly。
    信心不足则缩回。侵略性精确跟随证据强度。

三重削减（安全前提，不可删——Kelly 的最优性以“永不破产”为前提）：
    f_final = min(f* × kelly_fraction, max_single_pct, remaining_exposure_pct)

大胆加码②——机会稀缺性：edge 低于 min_edge 的机会直接不下注（返回 0），
把火力留给少数高 edge 机会。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KellySizing:
    f_star: float          # 原始 Kelly 比例
    kelly_fraction: float  # 本次采用的自适应分数
    f_final: float         # 三重削减后的最终比例
    stake_usdc: float      # 建议下注名义额
    reason: str


def adaptive_kelly_fraction(confidence: float, *,
                            kelly_min: float, kelly_max: float) -> float:
    """置信度 [0,1] 线性映射到 [kelly_min, kelly_max]。"""
    c = max(0.0, min(1.0, confidence))
    return kelly_min + (kelly_max - kelly_min) * c


def compute_stake(
    *,
    p: float,
    q: float,
    confidence: float,
    balance_usdc: float,
    remaining_exposure_usdc: float,
    leg_liquidity_usdc: float,
    min_edge: float,
    kelly_min: float,
    kelly_max: float,
    max_single_pct: float,
    max_single_usdc: float = 0.0,
) -> KellySizing:
    """计算一个机会的建议下注额。

    p:  合成估计真实概率
    q:  市场报价（买入成本）
    confidence: 合成总体置信度，驱动自适应 Kelly 分数
    balance_usdc: 当前实时余额（复利的核心——注随余额变）
    remaining_exposure_usdc: 风控剩余可用总仓位
    leg_liquidity_usdc: 该机会可成交深度
    min_edge: 机会稀缺性门槛，edge 低于此不出手
    kelly_min/max: 自适应 Kelly 分数区间
    max_single_pct: 单笔占余额硬上限
    """
    edge = p - q

    # 机会稀缺性：小 edge 直接放弃，避免被手续费/方差磨死。
    if edge < min_edge:
        return KellySizing(0.0, 0.0, 0.0, 0.0,
                           f"edge {edge:.4f} < 门槛 {min_edge:.4f}，不出手")

    # q 接近 1 时分母趋 0，Kelly 会爆炸；q 无效则拒绝。
    if q <= 0.0 or q >= 1.0:
        return KellySizing(0.0, 0.0, 0.0, 0.0, f"报价 q={q} 无效")

    f_star = edge / (1.0 - q)
    kelly_fraction = adaptive_kelly_fraction(
        confidence, kelly_min=kelly_min, kelly_max=kelly_max)

    # 三重削减
    f_kelly = f_star * kelly_fraction
    f_final = min(f_kelly, max_single_pct)

    stake = balance_usdc * f_final
    # 再受剩余总仓位与流动性约束
    stake = min(stake, remaining_exposure_usdc, leg_liquidity_usdc)
    # 单笔金额硬上限（USDC）：实测保险丝，>0 时无条件封顶。0=不限。
    if max_single_usdc > 0:
        stake = min(stake, max_single_usdc)
    stake = max(stake, 0.0)

    if stake <= 0.0:
        return KellySizing(f_star, kelly_fraction, f_final, 0.0,
                           "仓位/流动性约束后无可下注额度")

    return KellySizing(
        f_star=f_star,
        kelly_fraction=kelly_fraction,
        f_final=f_final,
        stake_usdc=round(stake, 2),
        reason=(
            f"edge {edge:.4f} | f*={f_star:.3f} | "
            f"自适应分数 {kelly_fraction:.3f}(置信 {confidence:.2f}) | "
            f"f_final={f_final:.3f} | 注 {stake:.2f} USDC"
        ),
    )
