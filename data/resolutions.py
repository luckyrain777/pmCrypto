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


# 判定“已收敛可定胜负”的价格阈值。
_WIN_PRICE = 0.90
_LOSE_PRICE = 0.10


def resolve_from_client(client, market_id: str) -> Optional[str]:
    """用 SDK 查一个市场是否已结算；返回获胜 token_id 或 None（未决/无法判定）。

    client: polymarket PublicClient。
    """
    try:
        m = client.get_market(condition_id=market_id)
    except Exception:
        try:
            m = client.get_market(market_id=market_id)
        except Exception:
            return None

    state = getattr(m, "state", None)
    if state is None or not getattr(state, "closed", False):
        return None  # 未关闭，胜负未定

    # 已关闭：取价格最高的结果为获胜方（需明确收敛）。
    best_token, best_price = None, -1.0
    for side in (m.outcomes.yes, m.outcomes.no):
        price = getattr(side, "price", None)
        tid = getattr(side, "token_id", None)
        if price is None or tid is None:
            continue
        if float(price) > best_price:
            best_price, best_token = float(price), str(tid)

    if best_token is not None and best_price >= _WIN_PRICE:
        return best_token
    return None  # 价格未收敛，暂不判定


def refresh_resolutions(client, store: Store) -> int:
    """对 store 中所有市场检查结算，写入 resolutions 表。返回新增结算数。"""
    already = set(store.all_resolutions().keys())
    count = 0
    for market_id in store.distinct_market_ids():
        if market_id in already:
            continue
        winner = resolve_from_client(client, market_id)
        if winner is not None:
            store.save_resolution(market_id, winner, resolved_ts=time.time())
            count += 1
    return count
