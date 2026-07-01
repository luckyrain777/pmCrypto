"""加密市场概率误定价信号（阶段B外部信号，最有效的一个）。

思路：Polymarket 上有大量 "Will the price of Bitcoin be above $X on DATE?"
这类市场。我们有实时现价 S 与年化波动率 σ，用对数正态（几何布朗运动，
风险中性 0 漂移——预测市场最中性假设）算出到期时 S_T > K 的真实概率 p_true，
与市场报价 q 相比即得 edge。这是所有外部信号里最客观、最可算的一个。

解析：从 question 文本抽 (asset, direction, strike)，从 end_date 抽到期时间。
定价：P(S_T > K) = Φ(d2)，d2 = (ln(S/K) - 0.5σ²T) / (σ√T)。
     above → p_true = Φ(d2)；below → 1 - Φ(d2)。

诚实边界：
- σ 用先验常数（非隐含波动率），故 p_true 是估计而非真值——edge 仍需回测验证。
- 极临近到期(T→0)时模型退化为"现价是否已过线"，此时最可靠。
- 无法解析的市场返回 None（不参与），绝不猜。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional


_ASSETS = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol": "solana",
    "dogecoin": "dogecoin", "doge": "dogecoin",
}

# above / reach / hit / exceed → 看涨突破；below / dip / under → 看跌
_ABOVE_WORDS = ("above", "reach", "hit", "exceed", "over", "surpass", ">=", ">")
_BELOW_WORDS = ("below", "dip", "under", "beneath", "<=", "<")


@dataclass(frozen=True)
class CryptoTerms:
    asset: str          # 归一化资产名（bitcoin/ethereum/...）
    strike: float       # 目标价 K
    direction: str      # "above" | "below"


def parse_market(question: str) -> Optional[CryptoTerms]:
    """从问题文本解析加密条款。无法确定则返回 None。"""
    if not question:
        return None
    q = question.lower()

    # 资产
    asset = None
    for kw, norm in _ASSETS.items():
        if re.search(rf"\b{re.escape(kw)}\b", q):
            asset = norm
            break
    if asset is None:
        return None

    # 目标价：$58,000 / $58000 / 58,000
    m = re.search(r"\$?\s*([\d]{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m)?", q)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    suffix = m.group(2)
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    strike = num

    # 方向
    direction = None
    if any(w in q for w in _ABOVE_WORDS):
        direction = "above"
    elif any(w in q for w in _BELOW_WORDS):
        direction = "below"
    if direction is None:
        return None

    return CryptoTerms(asset=asset, strike=strike, direction=direction)


def _norm_cdf(x: float) -> float:
    """标准正态 CDF，用 erf 实现（不依赖 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot: float, strike: float, annual_vol: float,
               years_to_expiry: float) -> float:
    """对数正态模型下，到期时 spot 突破 strike（above）的概率。"""
    if spot <= 0 or strike <= 0:
        return 0.0
    T = max(years_to_expiry, 0.0)
    # 极临近到期：模型退化为现价是否已过线。
    if T < 1e-9 or annual_vol <= 0:
        return 1.0 if spot > strike else 0.0
    sigma_sqrt_t = annual_vol * math.sqrt(T)
    d2 = (math.log(spot / strike) - 0.5 * annual_vol ** 2 * T) / sigma_sqrt_t
    return _norm_cdf(d2)


def estimate_true_prob(terms: CryptoTerms, spot: float, annual_vol: float,
                       years_to_expiry: float) -> float:
    """按方向返回估计真实概率 p_true。"""
    p_above = prob_above(spot, terms.strike, annual_vol, years_to_expiry)
    return p_above if terms.direction == "above" else (1.0 - p_above)


def confidence_from_expiry(years_to_expiry: float) -> float:
    """置信度：越临近到期，模型越可靠（波动率假设影响越小）。

    >30 天 → 低置信；≤1 天 → 高置信。线性映射，封顶 [0.3, 0.95]。
    """
    days = max(years_to_expiry * 365.0, 0.0)
    if days >= 30:
        return 0.3
    if days <= 1:
        return 0.95
    # 1~30 天线性
    return 0.95 - (days - 1) / 29.0 * (0.95 - 0.3)
