"""优先结算"下注过的市场"：让每个结算尽快贡献一个 edge 验证 bet。

无差别拉所有过期市场，先拉到的往往是从没下注的老市场，不产生 bets。
优先拉"方向性策略下过注(signals 表有 edge_directional)"的过期市场，
bets 涨得快得多。
"""
from data.store import Store
from strategy.models import Market, OutcomeBook, Signal, OpportunityKind


def _mkt(mid, end_ts):
    return Market(market_id=mid, question="q",
                  outcomes=(OutcomeBook("Yes", mid+"_y", best_ask=0.5),
                            OutcomeBook("No", mid+"_n", best_ask=0.5)),
                  snapshot_ts=1.0, end_ts=end_ts)


def _sig(mid):
    return Signal(market_id=mid, kind=OpportunityKind.EDGE_DIRECTIONAL,
                  raw_edge=0.1, net_edge=0.09, suggested_size_usdc=5.0,
                  legs=((mid+"_y", 0.5),), reason="t", snapshot_ts=1.0)


def test_signaled_markets_come_first(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    # 两个都过期未结算：mA 下过方向性注，mB 没有
    store.save_market_snapshot(_mkt("mA", end_ts=100.0))
    store.save_market_snapshot(_mkt("mB", end_ts=100.0))
    store.save_signal(_sig("mA"), created_ts=1.0)

    ids = store.expired_unresolved_market_ids(now=1000.0)
    assert ids[0] == "mA", "下注过的市场应排在最前"
    assert set(ids) == {"mA", "mB"}


def test_limit_picks_signaled_first(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    for i in range(5):
        store.save_market_snapshot(_mkt(f"plain{i}", end_ts=100.0))
    store.save_market_snapshot(_mkt("signaled", end_ts=100.0))
    store.save_signal(_sig("signaled"), created_ts=1.0)

    # 限量 1 → 必须先给下注过的
    ids = store.expired_unresolved_market_ids(now=1000.0, limit=1)
    assert ids == ["signaled"]


def test_still_returns_plain_when_no_signals(tmp_path):
    """没有任何下注过的市场时，仍正常返回过期未结算市场（不空转）。"""
    store = Store(str(tmp_path / "t.db"))
    store.save_market_snapshot(_mkt("m1", end_ts=100.0))
    ids = store.expired_unresolved_market_ids(now=1000.0)
    assert ids == ["m1"]
