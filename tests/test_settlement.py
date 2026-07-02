"""结算回填盈亏测试：把已结算市场的持仓台账转成已实现盈亏 + 驱动熔断。"""
import pytest

from config import Config
from core.state import GlobalState
from data.store import Store
from execution.settlement import settle_open_trades


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def _cfg(**kw):
    base = dict(account_balance_usdc=100.0, daily_max_loss_pct=0.20,
                max_consecutive_losses=3)
    base.update(kw)
    return Config(**base)


def test_arb_group_aggregated_no_false_consecutive_loss(store):
    """多腿套利(同一 group_id)：整体盈利,即使有输腿也不该记连亏。

    多腿套利的两腿可能跨【不同市场】但属同一套利组。逐腿结算会有一输一赢,
    若逐腿喂熔断,盈利套利会被误记连亏。应按 group 聚合净盈亏再判熔断。
    （此机制保护任何未来的多腿套利，与具体策略无关。）
    """
    # 两腿:mktA买中(赢),mktB没中(输),但整体是盈利套利,同 group_id='g1'
    store.record_trade("mktA", "tokWIN", 0.30, 100.0, 30.0, 1.0, group_id="g1")
    store.record_trade("mktB", "tokX",   0.30, 100.0, 30.0, 1.0, group_id="g1")
    store.save_resolution("mktA", "tokWIN", resolved_ts=2.0)   # A腿赢:+70
    store.save_resolution("mktB", "tokWINB", resolved_ts=2.0)  # B腿输:-30
    st = GlobalState()
    settle_open_trades(store, _cfg(), st)
    # 组净盈亏 = +70 -30 = +40 (盈利) → 连亏应为 0,不误触
    assert st.snapshot()["consecutive_losses"] == 0, "盈利套利被误记连亏"
    assert abs(st.snapshot()["daily_pnl_usdc"] - 40.0) < 1e-6


def test_win_backfills_positive_pnl(store):
    """买中获胜结果：pnl = 份数*(1-成本价) = shares - cost。"""
    tid = store.record_trade("0xm", "tokWIN", cost_price=0.40, shares=100.0,
                             cost_usdc=40.0, created_ts=1.0)
    store.save_resolution("0xm", "tokWIN", resolved_ts=2.0)
    st = GlobalState()
    n = settle_open_trades(store, _cfg(), st)
    assert n == 1
    # 赢：100 份到期各值 1 → 收 100，成本 40 → pnl +60
    assert store.all_trades()[0]["status"] == "closed"
    assert abs(store.all_trades()[0]["realized_pnl_usdc"] - 60.0) < 1e-6
    assert abs(st.snapshot()["daily_pnl_usdc"] - 60.0) < 1e-6
    assert st.snapshot()["consecutive_losses"] == 0


def test_loss_backfills_negative_pnl(store):
    """买错（获胜是别的 token）：pnl = -成本。"""
    tid = store.record_trade("0xm", "tokLOSE", cost_price=0.40, shares=100.0,
                             cost_usdc=40.0, created_ts=1.0)
    store.save_resolution("0xm", "tokWIN", resolved_ts=2.0)  # 赢的是别的
    st = GlobalState()
    settle_open_trades(store, _cfg(), st)
    assert abs(store.all_trades()[0]["realized_pnl_usdc"] + 40.0) < 1e-6
    assert abs(st.snapshot()["daily_pnl_usdc"] + 40.0) < 1e-6
    assert st.snapshot()["consecutive_losses"] == 1


def test_unsettled_market_skipped(store):
    """市场还没结算（无 resolution）→ 台账保持 open，不回填。"""
    store.record_trade("0xm", "tok", 0.5, 10.0, 5.0, 1.0)
    st = GlobalState()
    n = settle_open_trades(store, _cfg(), st)
    assert n == 0
    assert len(store.open_trades()) == 1


def test_consecutive_losses_trip_halt(store):
    """连亏达上限 → 触发全局急停（熔断数据地基接通的证明）。"""
    for i in range(3):
        store.record_trade(f"0xm{i}", "tokLOSE", 0.5, 10.0, 5.0, 1.0)
        store.save_resolution(f"0xm{i}", "tokWIN", resolved_ts=2.0)
    st = GlobalState()
    settle_open_trades(store, _cfg(max_consecutive_losses=3), st)
    assert st.is_halted
    assert "连续亏损" in st.snapshot()["halt_reason"]


def test_daily_loss_trips_halt(store):
    """当日亏损触及上限 → 急停。"""
    # 账户100、日损上限20% = 20 USDC；一笔亏25即触发
    store.record_trade("0xm", "tokLOSE", 0.5, 50.0, 25.0, 1.0)
    store.save_resolution("0xm", "tokWIN", resolved_ts=2.0)
    st = GlobalState()
    settle_open_trades(store, _cfg(), st)
    assert st.is_halted
    assert "当日亏损" in st.snapshot()["halt_reason"]


class _FakeGuard:
    def __init__(self, exposure=0.0):
        self.current_exposure_usdc = exposure
        self.released = []
    def release(self, x):
        self.released.append(x)
        self.current_exposure_usdc = max(self.current_exposure_usdc - x, 0.0)


def test_settle_releases_guard_exposure(store):
    """结算平仓应释放 guard 占用的仓位额度（防额度只增不减锁死新仓）。"""
    store.record_trade("0xm", "tokWIN", 0.4, 100.0, 40.0, 1.0)
    store.save_resolution("0xm", "tokWIN", resolved_ts=2.0)
    st = GlobalState()
    g = _FakeGuard(exposure=40.0)  # 下单时占用了 40
    settle_open_trades(store, _cfg(), st, guard=g)
    assert g.released == [40.0]
    assert g.current_exposure_usdc == 0.0  # 额度回落


def test_settle_is_idempotent(store):
    """已平仓的台账不会被重复结算（幂等）。"""
    store.record_trade("0xm", "tokWIN", 0.4, 100.0, 40.0, 1.0)
    store.save_resolution("0xm", "tokWIN", resolved_ts=2.0)
    st = GlobalState()
    settle_open_trades(store, _cfg(), st)
    # 再跑一次：已 closed，不应再动 pnl
    n2 = settle_open_trades(store, _cfg(), st)
    assert n2 == 0
    assert abs(st.snapshot()["daily_pnl_usdc"] - 60.0) < 1e-6
