"""拉取/记录市场结算结果 —— edge 验证的原料。

判断“谁赢了”的稳健方法：市场 closed 后，获胜结果价格趋近 1.0、输方趋近 0.0。
因此对已关闭市场，取价格最高（≈1）的结果为获胜方。这不依赖不确定的 UMA
字段，只依赖“结算后价格收敛”这一市场铁律。

用法：定期（或收盘后）对已积累快照的市场调用 refresh_resolutions，
把结算结果写入 store.resolutions 表，供 edge_report 回测使用。
"""
from __future__ import annotations

import time
from typing import Optional

from data.store import Store


# 判定“已完全收敛可定胜负”的价格阈值。
# 用 0.99 而非 0.90：市场刚 closed 时价格可能仅到 0.90~0.95（临时失衡，
# 尚未最终结算），此时判胜负会误判、回填错误盈亏、误触/误抑熔断。
# 要求 ≥0.99 才认定，宁可晚一轮结算，也不接受错误结算。
_WIN_PRICE = 0.99
_LOSE_PRICE = 0.01


def resolve_from_client(client, market_id: str) -> Optional[str]:
    """用 SDK 查一个市场是否已定胜负；返回获胜 token_id 或 None（未决/查不到）。

    用 list_markets(condition_ids=[...]) 按 condition_id 查（get_market 的参数是
    数字 id/slug，不接受 condition_id）。

    判定依据【价格收敛】而非 state.closed —— 实测发现很多已定局的市场 closed
    仍为 False（标志滞后/不可靠），但价格早已收敛到 0/1。故只要某结果价格
    ≥ _WIN_PRICE 即认定它获胜（我们只对已到期市场调用，收敛价基本就是终局）。
    """
    # 先查活跃市场；查不到再查【已关闭】市场——短周期加密市场结算后会从
    # 活跃列表消失，必须带 closed=True 才查得回它的最终收敛价（bets 的关键来源）。
    def _fetch(**kw):
        try:
            return list(client.list_markets(condition_ids=[market_id],
                                            **kw).iter_items())
        except Exception:
            return []
    items = _fetch() or _fetch(closed=True)
    if not items:
        return None
    m = items[0]

    # 取价格最高的结果；收敛到 ≥_WIN_PRICE 才认定获胜，否则未定。
    best_token, best_price = None, -1.0
    try:
        sides = (m.outcomes.yes, m.outcomes.no)
    except Exception:
        return None
    for side in sides:
        price = getattr(side, "price", None)
        tid = getattr(side, "token_id", None)
        if price is None or tid is None:
            continue
        if float(price) > best_price:
            best_price, best_token = float(price), str(tid)

    if best_token is not None and best_price >= _WIN_PRICE:
        return best_token
    return None  # 价格未收敛，暂不判定


def refresh_resolutions(client, store: Store, market_ids=None) -> int:
    """检查市场结算并写入 resolutions 表，返回新增结算数。

    market_ids：指定只查这些市场（None 则查全部）。主循环传入
    "已过期未结算" 的一小批，避免每轮对上千市场狂发 API。
    """
    already = set(store.all_resolutions().keys())
    targets = market_ids if market_ids is not None else store.distinct_market_ids()
    count = 0
    for market_id in targets:
        if market_id in already:
            continue
        winner = resolve_from_client(client, market_id)
        if winner is not None:
            store.save_resolution(market_id, winner, resolved_ts=time.time())
            count += 1
    return count
