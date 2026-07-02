"""已过期未结算市场查询：edge 验证原料的来源，不依赖是否持仓。"""
from data.store import Store
from strategy.models import Market, OutcomeBook


def _mkt(mid, end_ts):
    return Market(market_id=mid, question="q",
                  outcomes=(OutcomeBook("Yes", mid+"_y", best_ask=0.5),
                            OutcomeBook("No", mid+"_n", best_ask=0.5)),
                  snapshot_ts=1.0, end_ts=end_ts)


def test_returns_only_expired_unresolved(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.save_market_snapshot(_mkt("expired1", end_ts=100.0))   # 已过期
    store.save_market_snapshot(_mkt("expired2", end_ts=200.0))   # 已过期
    store.save_market_snapshot(_mkt("future", end_ts=9999999999.0))  # 未到期
    store.save_market_snapshot(_mkt("resolved", end_ts=100.0))   # 已过期但已结算
    store.save_resolution("resolved", "resolved_y", resolved_ts=150.0)

    ids = store.expired_unresolved_market_ids(now=1000.0)
    assert set(ids) == {"expired1", "expired2"}   # 未到期的、已结算的都排除


def test_respects_limit(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    for i in range(10):
        store.save_market_snapshot(_mkt(f"m{i}", end_ts=100.0))
    ids = store.expired_unresolved_market_ids(now=1000.0, limit=3)
    assert len(ids) == 3


def test_no_end_ts_excluded(tmp_path):
    """end_ts=0(未知到期)的市场不算过期，排除。"""
    store = Store(str(tmp_path / "t.db"))
    store.save_market_snapshot(_mkt("noend", end_ts=0.0))
    assert store.expired_unresolved_market_ids(now=1000.0) == []
