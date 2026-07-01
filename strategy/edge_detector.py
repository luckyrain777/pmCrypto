"""阶段 B detector：方向性误定价检测。

对一个市场，取其历史快照序列 → 算三个统计信号 → combiner 合成估计概率 p
→ 若 |p - q| 足够大，产出一个方向性 Opportunity（单腿：买被低估的那个结果）。

与阶段 A 套利并存、互不干扰：本 detector 产出 EDGE_DIRECTIONAL 机会，
由 guard 的 Kelly 模式定仓位。

诚实边界：edge 只是“信号认为存在”，是否真有 edge 必须由 edge_report 用
结算结果回测验证。未验证前，这些机会只应在 manual 模式提示，不上真钱。
"""
from __future__ import annotations

import time

from config import CONFIG
from strategy.models import Market, Opportunity, OpportunityKind
from strategy.signals import momentum, book_imbalance, volume_divergence
from strategy.signals.base import SignalOutput, mid_price
from strategy.signals.combiner import combine
from strategy.signals import crypto_mispricing as cm


def _series_for_outcome(history: list[Market], outcome_idx: int):
    """从历史快照序列抽取某结果的 (中间价序列, 总深度序列)。"""
    mids, depths = [], []
    for mkt in history:
        if outcome_idx >= len(mkt.outcomes):
            continue
        o = mkt.outcomes[outcome_idx]
        mp = mid_price(o.best_ask, o.best_bid)
        if mp is None:
            continue
        mids.append(mp)
        bid_notional = (o.best_bid or 0.0) * o.bid_size
        ask_notional = o.ask_notional_usdc
        depths.append(bid_notional + ask_notional)
    return mids, depths


def detect_edge(history: list[Market]) -> list[Opportunity]:
    """对某市场的历史序列检测方向性误定价。history 时间升序，末尾为最新。"""
    if not history:
        return []
    latest = history[-1]
    opps: list[Opportunity] = []

    for idx, o in enumerate(latest.outcomes):
        q = mid_price(o.best_ask, o.best_bid)
        if q is None or o.best_ask is None:
            continue  # 无报价无法买入

        mids, depths = _series_for_outcome(history, idx)
        if len(mids) < 3:
            continue  # 历史太短，信号不可靠

        sig_mom = momentum.compute(mids)
        sig_book = book_imbalance.compute(
            bid_notional=(o.best_bid or 0.0) * o.bid_size,
            ask_notional=o.ask_notional_usdc,
        )
        sig_vol = volume_divergence.compute(depths, mids)

        est = combine(q, [sig_mom, sig_book, sig_vol])

        # 只在“看涨该结果”且 edge 达门槛时产出（买入方向）。
        if est.edge < CONFIG.edge_min_threshold:
            continue

        # 用买入价 best_ask 作为实际成本腿价。
        opps.append(Opportunity(
            market_id=latest.market_id,
            kind=OpportunityKind.EDGE_DIRECTIONAL,
            raw_edge=est.edge,
            legs=((o.token_id, o.best_ask),),
            min_leg_notional_usdc=o.ask_notional_usdc,
            snapshot_ts=latest.snapshot_ts,
            estimated_p=est.p,
            confidence=est.confidence,
            question=latest.question,
        ))
    return opps


def detect_crypto_edge(market: Market, crypto_source, *,
                       now: float | None = None) -> list[Opportunity]:
    """加密市场专用：用现价+波动率的对数正态模型算真实概率，找误定价。

    market: 最新快照（需含 end_ts 与可解析的加密问题文本）。
    crypto_source: 提供 spot(asset)/annual_vol(asset) 的现价源。
    now: 当前 epoch 秒（便于测试注入）；None 则取系统时间。

    仅对二元(Yes/No)加密市场生效：outcomes[0]=Yes(命题成立), [1]=No。
    """
    terms = cm.parse_market(market.question)
    if terms is None:
        return []
    if market.end_ts <= 0 or len(market.outcomes) != 2:
        return []

    spot = crypto_source.spot(terms.asset)
    if spot is None:
        return []  # 现价拉取失败，降级不产出

    now = time.time() if now is None else now
    years = max((market.end_ts - now) / (365.0 * 24 * 3600), 0.0)
    vol = crypto_source.annual_vol(terms.asset)

    # 命题(above/below)成立的真实概率 = Yes 的 p_true。
    p_yes = cm.estimate_true_prob(terms, spot, vol, years)
    confidence = cm.confidence_from_expiry(years)

    opps: list[Opportunity] = []
    yes_o, no_o = market.outcomes[0], market.outcomes[1]
    # 两条腿分别评估：买 Yes 用 p_yes，买 No 用 1 - p_yes。
    for o, p_true in ((yes_o, p_yes), (no_o, 1.0 - p_yes)):
        if o.best_ask is None:
            continue
        edge = p_true - o.best_ask
        if edge < CONFIG.edge_min_threshold:
            continue
        opps.append(Opportunity(
            market_id=market.market_id,
            kind=OpportunityKind.EDGE_DIRECTIONAL,
            raw_edge=edge,
            legs=((o.token_id, o.best_ask),),
            min_leg_notional_usdc=o.ask_notional_usdc,
            snapshot_ts=market.snapshot_ts,
            estimated_p=p_true,
            confidence=confidence,
            question=market.question,
        ))
    return opps
