"""套利执行器测试：多腿逐腿 FOK 下单 + 失败回滚（防半腿裸赌）。"""
import pytest

from config import Config
from core.state import STATE
from strategy.models import Opportunity, OpportunityKind
from execution import arb as arb_module


@pytest.fixture(autouse=True)
def reset_state():
    STATE.reset(); yield; STATE.reset()


class FakeNotifier:
    def __init__(self): self.infos=[]; self.warns=[]
    def info(self,m): self.infos.append(m)
    def warning(self,m): self.warns.append(m)


class FakeStore:
    def __init__(self): self.trades=[]
    def record_trade(self,**kw): self.trades.append(kw); return len(self.trades)


class FakeGuard:
    def __init__(self): self.reserved=0.0
    def reserve(self,x): self.reserved+=x
    def release(self,x): self.reserved=max(self.reserved-x,0.0)


class Filled:
    ok=True; order_id="O"; trade_ids=["t"]; taking_amount=10.0; making_amount=5.0
class NotFilled:
    ok=True; order_id="O"; trade_ids=[]; taking_amount=0.0; making_amount=0.0


class FakeClient:
    """按 side 序列返回预设结果，记录所有下单。"""
    def __init__(self, results): self.results=list(results); self.orders=[]
    def place_market_order(self,**kw):
        self.orders.append(kw)
        return self.results.pop(0) if self.results else NotFilled()
    def get_balance_allowance(self,*,asset_type):
        class B: balance=1000.0
        return B()


def _arb_opp():
    """两腿套利机会（Yes/No 互补）。"""
    return Opportunity(
        market_id="m", kind=OpportunityKind.YES_NO_COMPLEMENT, raw_edge=0.15,
        legs=(("tokA", 0.47), ("tokB", 0.47)), min_leg_notional_usdc=100.0,
        snapshot_ts=1.0, question="q")


def _mk(monkeypatch, **cfg):
    base = {"dry_run": False, "enable_arb_auto": True}
    base.update(cfg)          # 允许用例覆盖默认
    c = Config(**base)
    monkeypatch.setattr(arb_module, "CONFIG", c)
    return c


def test_both_legs_fill_records_trades(monkeypatch):
    """两腿都成交 → 记两条台账，无回滚。"""
    _mk(monkeypatch)
    n, s = FakeNotifier(), FakeStore()
    ex = arb_module.ArbExecutor(s, n)
    ex._client = FakeClient([Filled(), Filled()])
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_arb_opp())
    buys = [o for o in ex._client.orders if o["side"]=="BUY"]
    sells = [o for o in ex._client.orders if o["side"]=="SELL"]
    assert len(buys)==2 and len(sells)==0     # 两腿买入,无回滚
    assert len(s.trades)==2
    # 关键:两腿必须【等份数】才能锁定套利(而非等金额)
    assert "shares" in buys[0] and buys[0]["shares"]>0
    assert buys[0]["shares"]==buys[1]["shares"], "两腿份数不等,锁不住套利"
    assert buys[0]["order_type"]=="FOK"
    # 台账份数=实际下单份数
    assert s.trades[0]["shares"]==buys[0]["shares"]


def test_second_leg_fails_rolls_back_first(monkeypatch):
    """第一腿成、第二腿未成 → 回滚(卖回第一腿)，不留半腿，不记台账。"""
    _mk(monkeypatch)
    n, s = FakeNotifier(), FakeStore()
    ex = arb_module.ArbExecutor(s, n)
    ex._client = FakeClient([Filled(), NotFilled(), Filled()])  # 第3个给回滚SELL用
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_arb_opp())
    sells = [o for o in ex._client.orders if o["side"]=="SELL"]
    assert len(sells)==1                       # 回滚了第一腿
    assert sells[0]["token_id"]=="tokA"
    assert s.trades==[]                        # 半腿失败不记台账
    assert any("回滚" in m for m in n.warns)


def test_rollback_failure_trips_halt_and_records(monkeypatch):
    """第二腿失败→回滚第一腿,但回滚SELL也失败→必须急停+记裸头寸(不再隐形)。"""
    _mk(monkeypatch)
    n, s, g = FakeNotifier(), FakeStore(), FakeGuard()
    ex = arb_module.ArbExecutor(s, n, g)
    # 第1腿买成; 第2腿买失败; 回滚第1腿的SELL也失败(FOK被kill)
    ex._client = FakeClient([Filled(), NotFilled(), NotFilled()])
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_arb_opp())
    # 回滚失败→裸头寸不能隐形:必须急停 + 记台账留痕
    assert STATE.is_halted, "回滚失败却没急停,裸头寸会隐形"
    assert len(s.trades)>=1, "裸头寸必须记台账,不能隐形"
    assert any("人工" in m or "裸" in m for m in n.warns)
    # 裸头寸必须占用 exposure(否则风控失控,允许继续开新仓)
    assert g.reserved>0, "裸头寸未占用额度,风控会失控"


def test_single_order_usdc_cap_shrinks_shares(monkeypatch):
    """单笔金额硬上限 → 收紧每腿份数 N（实测保险丝，各策略通用）。"""
    _mk(monkeypatch, max_single_order_usdc=10.0)  # 每腿最多 10 USDC
    n, s = FakeNotifier(), FakeStore()
    ex = arb_module.ArbExecutor(s, n)
    ex._client = FakeClient([Filled(), Filled()])
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_arb_opp())  # min_leg_notional=100, 若不限 N≈212
    buys = [o for o in ex._client.orders if o["side"] == "BUY"]
    # 10 USDC / 0.47 ≈ 21.28 份，远小于不限时的 ~212
    assert abs(buys[0]["shares"] - round(10.0/0.47, 2)) < 1e-6
    assert buys[0]["shares"] == buys[1]["shares"]  # 仍等份数


def test_cost_too_high_skips(monkeypatch):
    """毛edge不足以覆盖双向滑点 → 净收益≤0 → 跳过不下单(防白做/亏损)。"""
    _mk(monkeypatch)  # 默认 market_max_slippage_pct=0.03, 两腿=0.06
    n, s = FakeNotifier(), FakeStore()
    ex = arb_module.ArbExecutor(s, n)
    ex._client = FakeClient([Filled(), Filled()])
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    # raw_edge=0.05 < 估滑点0.06 → 净负
    opp = Opportunity(market_id="m", kind=OpportunityKind.YES_NO_COMPLEMENT,
                      raw_edge=0.05, legs=(("tokA",0.47),("tokB",0.48)),
                      min_leg_notional_usdc=100.0, snapshot_ts=1.0, question="q")
    ex.execute(opp)
    assert ex._client.orders == []             # 不下单
    assert s.trades == []


def test_three_legs_middle_fails_rolls_back_two(monkeypatch):
    """三腿:前两腿成、第三腿失败 → 回滚前两腿。"""
    _mk(monkeypatch)
    n, s = FakeNotifier(), FakeStore()
    ex = arb_module.ArbExecutor(s, n)
    # 买1成 买2成 买3失败; 回滚SELL1成 SELL2成
    ex._client = FakeClient([Filled(), Filled(), NotFilled(), Filled(), Filled()])
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    opp = Opportunity(market_id="m", kind=OpportunityKind.MUTEX_PROB_SUM,
                      raw_edge=0.20, legs=(("t1",0.30),("t2",0.30),("t3",0.30)),
                      min_leg_notional_usdc=100.0, snapshot_ts=1.0, question="q")
    ex.execute(opp)
    sells = [o for o in ex._client.orders if o["side"]=="SELL"]
    assert len(sells)==2                       # 回滚了前两腿
    assert s.trades == []                       # 未全成不记正常台账
    assert not STATE.is_halted                  # 回滚成功,无需急停


def test_halted_rejects(monkeypatch):
    _mk(monkeypatch)
    STATE.trip("halt")
    n=FakeNotifier()
    ex=arb_module.ArbExecutor(FakeStore(), n)
    ex._client=FakeClient([Filled(),Filled()])
    monkeypatch.setattr(ex,"_ensure_client",lambda:True)
    ex.execute(_arb_opp())
    assert ex._client.orders==[]               # 急停不下任何单


def test_switch_off_rejects(monkeypatch):
    """套利自动开关关闭 → 不执行。"""
    _mk(monkeypatch, enable_arb_auto=False)
    n=FakeNotifier()
    ex=arb_module.ArbExecutor(FakeStore(), n)
    ex._client=FakeClient([Filled(),Filled()])
    monkeypatch.setattr(ex,"_ensure_client",lambda:True)
    ex.execute(_arb_opp())
    assert ex._client.orders==[]


def test_dry_run_no_order(monkeypatch):
    _mk(monkeypatch, dry_run=True)
    n, s=FakeNotifier(), FakeStore()
    ex=arb_module.ArbExecutor(s, n)
    ex._client=FakeClient([Filled(),Filled()])
    monkeypatch.setattr(ex,"_ensure_client",lambda:True)
    ex.execute(_arb_opp())
    assert ex._client.orders==[]               # 干跑不下真单
    assert any("DRY-RUN" in m or "干跑" in m for m in n.infos)
