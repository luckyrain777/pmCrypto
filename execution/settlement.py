"""结算回填：把已结算市场的持仓台账转成已实现盈亏，驱动风控熔断。

这是“已实现盈亏 → 连亏/当日止损熔断”的数据地基。没有它，core.state 的
record_trade_result 永远拿不到数据，连亏熔断/当日止损形同虚设。

流程：对每笔 open 台账，查该市场是否已结算（resolutions 表）：
  - 已结算：比对台账买入的 token 与获胜 token。
      赢（token==winner）→ 每份到期值 1，pnl = shares*(1-cost_price) = shares - cost_usdc
      输（token!=winner）→ 归零，pnl = -cost_usdc
    调 STATE.record_trade_result 累计盈亏 + 判熔断，并把台账标记 closed（回填 pnl）。
  - 未结算：跳过，台账保持 open。

幂等：只处理 open 台账；已 closed 的不会重复结算。
"""
from __future__ import annotations

import time
from typing import Optional

from core.activity import ACTIVITY


def settle_open_trades(store, config=None, state=None, notifier=None,
                       guard=None) -> int:
    """结算所有可结算的 open 台账，返回本次平仓笔数。

    config/state 默认取全局单例；测试可注入独立实例。
    guard 若提供，平仓时释放其占用的仓位额度（exposure），避免额度只增不减
    导致 remaining_exposure 归零、新仓被静默压成 0。
    """
    if config is None:
        from config import CONFIG as config
    if state is None:
        from core.state import STATE as state

    # 当日亏损上限（USDC）与连亏上限，供熔断判定。
    daily_max_loss_usdc = config.account_balance_usdc * config.daily_max_loss_pct
    max_consec = config.max_consecutive_losses

    resolutions = store.all_resolutions()  # {market_id: winning_token_id}

    def _leg_pnl(t) -> float:
        if str(t["token_id"]) == str(resolutions[t["market_id"]]):
            return round(t["shares"] * (1.0 - t["cost_price"]), 6)  # 赢
        return round(-t["cost_usdc"], 6)                             # 输

    def _settleable(t) -> bool:
        w = resolutions.get(t["market_id"])
        return w is not None  # 该腿所在市场已结算且胜负已定

    # 按组归集 open 台账：group_id 有则用它（多腿套利同组），否则用 "single:<id>"
    # 让每条单腿方向性单自成一组（行为等价于旧的逐笔处理）。
    groups: dict[str, list] = {}
    for t in store.open_trades():
        key = t["group_id"] if t.get("group_id") else f"single:{t['id']}"
        groups.setdefault(key, []).append(t)

    closed = 0
    resolved_ts = time.time()
    for key, legs in groups.items():
        # 组内【所有】腿都可结算才处理；否则整组等下轮（避免部分结算重复喂熔断）。
        if not all(_settleable(t) for t in legs):
            continue

        group_pnl = 0.0
        for t in legs:
            pnl = _leg_pnl(t)
            store.close_trade(t["id"], realized_pnl_usdc=pnl, resolved_ts=resolved_ts)
            if guard is not None:
                guard.release(t["cost_usdc"])   # 释放该腿占用额度
            group_pnl += pnl
            closed += 1
        group_pnl = round(group_pnl, 6)

        # 关键：按【整组净盈亏】喂一次熔断——多腿套利的输腿不再被单独误记连亏。
        state.record_trade_result(
            pnl_usdc=group_pnl,
            daily_max_loss_usdc=daily_max_loss_usdc,
            max_consecutive_losses=max_consec,
        )
        verdict = "赢" if group_pnl > 0 else ("平" if group_pnl == 0 else "亏")
        tag = "套利组" if len(legs) > 1 else "单笔"
        ACTIVITY.record("settle",
                        f"结算平仓·{verdict}：{len(legs)} 腿净盈亏 {group_pnl:+.2f} USDC",
                        resolved_ts)
        if notifier is not None:
            notifier.info(
                f"【结算·{tag}·{verdict}】{len(legs)} 腿净盈亏 {group_pnl:+.2f} USDC")

    if notifier is not None and closed:
        notifier.info(f"本轮结算平仓 {closed} 笔。")
    return closed
