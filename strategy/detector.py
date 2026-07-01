"""偏差检测。

只接收标准化的 Market 对象，输出候选 Opportunity（尚未扣成本）。
不关心数据来源，因此实盘与回测共用同一套逻辑。

第一阶段实现两类客观偏差：
  ① Yes/No 互补偏差：yes_ask + no_ask 明显 < 1 → 同时买入可锁无风险差价。
  ② 互斥多结果概率和偏差：一组互斥结果的 ask 之和明显 < 1 → 全买可锁利。

注意：本层只判断“名义上是否存在偏差”，是否真正划算由 pricing 扣成本后决定。
"""
from __future__ import annotations

from strategy.models import Market, OutcomeBook, Opportunity, OpportunityKind


def _ask_notional(o: OutcomeBook) -> float:
    return o.ask_notional_usdc


def detect_yes_no_complement(market: Market) -> Opportunity | None:
    """检测单个二元市场的 Yes/No 互补偏差。

    需要 yes 和 no 两腿都有有效 best_ask。
    raw_edge = 1 - (yes_ask + no_ask)，> 0 才是机会。
    """
    if len(market.outcomes) != 2:
        return None

    legs_priced = [o for o in market.outcomes if o.best_ask is not None]
    if len(legs_priced) != 2:
        return None

    ask_sum = sum(o.best_ask for o in legs_priced)  # type: ignore[misc]
    raw_edge = 1.0 - ask_sum
    if raw_edge <= 0:
        return None

    min_notional = min(_ask_notional(o) for o in legs_priced)
    return Opportunity(
        market_id=market.market_id,
        kind=OpportunityKind.YES_NO_COMPLEMENT,
        raw_edge=raw_edge,
        legs=tuple((o.token_id, o.best_ask) for o in legs_priced),  # type: ignore[misc]
        min_leg_notional_usdc=min_notional,
        snapshot_ts=market.snapshot_ts,
        question=market.question,
    )


def detect_mutex_prob_sum(market: Market) -> Opportunity | None:
    """检测互斥多结果（>2）的概率和偏差。

    所有结果 ask 之和明显 < 1 → 全买锁利。
    要求每个结果都有有效 best_ask（缺腿无法套利）。
    """
    if len(market.outcomes) <= 2:
        return None

    if any(o.best_ask is None for o in market.outcomes):
        return None

    ask_sum = sum(o.best_ask for o in market.outcomes)  # type: ignore[misc]
    raw_edge = 1.0 - ask_sum
    if raw_edge <= 0:
        return None

    min_notional = min(_ask_notional(o) for o in market.outcomes)
    return Opportunity(
        market_id=market.market_id,
        kind=OpportunityKind.MUTEX_PROB_SUM,
        raw_edge=raw_edge,
        legs=tuple((o.token_id, o.best_ask) for o in market.outcomes),  # type: ignore[misc]
        min_leg_notional_usdc=min_notional,
        snapshot_ts=market.snapshot_ts,
        question=market.question,
    )


def detect(market: Market) -> list[Opportunity]:
    """对单个市场跑所有检测器，返回所有命中的候选机会。"""
    opps: list[Opportunity] = []
    yn = detect_yes_no_complement(market)
    if yn is not None:
        opps.append(yn)
    mx = detect_mutex_prob_sum(market)
    if mx is not None:
        opps.append(mx)
    return opps
