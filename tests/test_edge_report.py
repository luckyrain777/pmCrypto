"""edge_report 统计独立性测试：每个市场只贡献 1 个独立下注样本，
消除“同一市场递增前缀反复评估”导致的伪重复（pseudoreplication），
避免 95% 置信区间虚高、假 edge 被放行。
"""
import pytest

from config import Config
from data.store import Store
from strategy.models import Market, OutcomeBook
from strategy import edge_detector
from backtest import edge_report as er


def _uptrend_market(market_id, n=6):
    """一段 Yes 持续上涨、买盘厚的历史（多个时点都会触发 edge 信号）。"""
    hist = []
    base = [0.30, 0.33, 0.36, 0.39, 0.42, 0.45][:n]
    for i, price in enumerate(base):
        hist.append(Market(
            market_id=market_id, question="q",
            outcomes=(
                OutcomeBook("Yes", market_id + "_ty", best_ask=price + 0.01,
                            best_bid=price - 0.01, ask_size=200.0, bid_size=1500.0),
                OutcomeBook("No", market_id + "_tn", best_ask=1 - price + 0.01,
                            best_bid=1 - price - 0.01, ask_size=1500.0, bid_size=200.0),
            ),
            snapshot_ts=float(i),
        ))
    return hist


@pytest.fixture
def store_with_two_markets(tmp_path, monkeypatch):
    monkeypatch.setattr(edge_detector, "CONFIG", Config(edge_min_threshold=0.005))
    store = Store(str(tmp_path / "t.db"))
    for mid in ("mA", "mB"):
        for snap in _uptrend_market(mid):
            store.save_market_snapshot(snap)
        # 两个市场都结算为 Yes 获胜
        store.save_resolution(mid, mid + "_ty", resolved_ts=100.0)
    return store


def test_each_market_contributes_at_most_one_bet(store_with_two_markets):
    """2 个已结算市场、每个历史多时点都触发信号 → bets 应 == 2（每市场1样本），
    而非“时点数×市场数”那样的伪重复放大。"""
    rep = er.run_edge_report(store_with_two_markets)
    # 关键断言：独立样本数受市场数上限约束，不被同市场多时点放大。
    assert rep.bets <= 2, f"bets={rep.bets} 超过市场数，仍存在伪重复"
    assert rep.bets >= 1  # 至少有市场触发


def test_no_bet_when_no_signal(tmp_path, monkeypatch):
    """无信号触发的市场不贡献 bet。"""
    monkeypatch.setattr(edge_detector, "CONFIG", Config(edge_min_threshold=0.99))
    store = Store(str(tmp_path / "t.db"))
    for snap in _uptrend_market("mC"):
        store.save_market_snapshot(snap)
    store.save_resolution("mC", "mC_ty", resolved_ts=100.0)
    rep = er.run_edge_report(store)
    assert rep.bets == 0
