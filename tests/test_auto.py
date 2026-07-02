"""auto 执行器测试：dry-run + 多重安全门 + 下单路径（全用假client，不联网不花钱）。"""
import pytest

from config import Config
from core.state import STATE
from strategy.models import Signal, OpportunityKind
from execution import auto as auto_module


@pytest.fixture(autouse=True)
def reset_state():
    STATE.reset()
    yield
    STATE.reset()


class FakeNotifier:
    def __init__(self): self.infos = []; self.warns = []
    def info(self, m): self.infos.append(m)
    def warning(self, m): self.warns.append(m)


class FakeStore:
    def __init__(self): self.saved = []; self.trades = []
    def save_signal(self, sig, created_ts): self.saved.append(sig)
    def record_trade(self, **kw): self.trades.append(kw); return len(self.trades)


class FakeGuard:
    def __init__(self, bal=100.0): self.account_balance_usdc = bal; self.reserved = 0.0
    def reserve(self, x): self.reserved += x


class Accepted:
    """FOK 成交：trade_ids 非空即视为真成交。"""
    ok = True; order_id = "ORD1"; trade_ids = ["T1"]
    making_amount = 10.0; taking_amount = 20.0; status = "matched"


class NotFilled:
    """FOK 被 kill：ok=True 但无成交（trade_ids 空）→ 不应记账。"""
    ok = True; order_id = "ORD2"; trade_ids = []
    making_amount = 0.0; taking_amount = 0.0; status = "unmatched"


class Rejected:
    ok = False; code = "X"; message = "insufficient"; trade_ids = []


class FakeClient:
    def __init__(self, resp): self.resp = resp; self.orders = []
    def get_balance_allowance(self, *, asset_type):
        class B: balance = 100.0
        return B()
    def place_market_order(self, **kw): self.orders.append(kw); return self.resp


def _edge_signal(size=10.0, price=0.5):
    return Signal(
        market_id="m", kind=OpportunityKind.EDGE_DIRECTIONAL,
        raw_edge=0.1, net_edge=0.09, suggested_size_usdc=size,
        legs=(("token123456", price),), reason="test", snapshot_ts=1.0)


def _mk(monkeypatch, **cfg):
    c = Config(**cfg)
    monkeypatch.setattr(auto_module, "CONFIG", c)
    return c


def test_dry_run_does_not_order(monkeypatch):
    _mk(monkeypatch, dry_run=True)
    n, s, g = FakeNotifier(), FakeStore(), FakeGuard()
    ex = auto_module.AutoExecutor(s, n, g)
    ex._client = FakeClient(Accepted())  # 即便有 client 也不该用
    ex.execute(_edge_signal())
    assert any("DRY-RUN" in m for m in n.infos)
    assert ex._client.orders == []       # 未真正下单
    assert len(s.saved) == 1             # 但留痕


def test_halted_rejects(monkeypatch):
    _mk(monkeypatch, dry_run=True)
    STATE.trip("halt")
    n = FakeNotifier()
    ex = auto_module.AutoExecutor(FakeStore(), n, FakeGuard())
    ex.execute(_edge_signal())
    assert any("急停" in m for m in n.warns)


def test_non_directional_skipped(monkeypatch):
    _mk(monkeypatch, dry_run=True)
    n = FakeNotifier()
    ex = auto_module.AutoExecutor(FakeStore(), n, FakeGuard())
    sig = Signal("m", OpportunityKind.YES_NO_COMPLEMENT, 0.05, 0.03, 10.0,
                 (("t", 0.5),), "arb", 1.0)
    ex.execute(sig)
    assert any("暂不自动执行" in m for m in n.warns)


def test_live_without_edge_verified_rejected(monkeypatch):
    _mk(monkeypatch, dry_run=False, edge_verified=False)
    n = FakeNotifier()
    ex = auto_module.AutoExecutor(FakeStore(), n, FakeGuard())
    ex._client = FakeClient(Accepted())
    ex.execute(_edge_signal())
    assert any("edge 尚未验证" in m for m in n.warns)
    assert ex._client.orders == []


def test_live_verified_but_no_creds_rejected(monkeypatch):
    _mk(monkeypatch, dry_run=False, edge_verified=True)
    # 让 _ensure_client 因无凭证失败
    monkeypatch.setattr("data.credentials.load_credentials", lambda env_path=".env": None)
    n = FakeNotifier()
    ex = auto_module.AutoExecutor(FakeStore(), n, FakeGuard())
    ex.execute(_edge_signal())
    assert any("未在 .env" in m for m in n.warns)


def test_live_places_order_and_reserves(monkeypatch):
    _mk(monkeypatch, dry_run=False, edge_verified=True)
    n, s, g = FakeNotifier(), FakeStore(), FakeGuard()
    ex = auto_module.AutoExecutor(s, n, g)
    # 绕过真实客户端建立，直接注入 fake
    ex._client = FakeClient(Accepted())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal(size=10.0, price=0.5))
    assert len(ex._client.orders) == 1
    o = ex._client.orders[0]
    # FOK 市价单：按金额(amount)买入，非挂历史价的限价单
    assert o["side"] == "BUY" and o["token_id"] == "token123456"
    assert o["order_type"] == "FOK"
    assert abs(o["amount"] - 10.0) < 1e-6     # 花 10 USDC
    assert o["max_price"] > 0.5               # 防滑点上限 > 信号价
    assert g.reserved == 10.0                # 占用仓位
    assert len(s.saved) == 1
    # 成交(trade_ids 非空)才写入持仓台账
    assert len(s.trades) == 1
    t = s.trades[0]
    assert t["token_id"] == "token123456"
    assert abs(t["shares"] - 20.0) < 1e-6     # 10 USDC / 0.5 = 20 份
    assert abs(t["cost_usdc"] - 10.0) < 1e-6


def test_fok_not_filled_no_record(monkeypatch):
    """FOK 被 kill（ok=True 但 trade_ids 空）→ 不记账、不占额度、不计成交。"""
    _mk(monkeypatch, dry_run=False, edge_verified=True)
    n, s, g = FakeNotifier(), FakeStore(), FakeGuard()
    ex = auto_module.AutoExecutor(s, n, g)
    ex._client = FakeClient(NotFilled())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal(size=10.0, price=0.5))
    assert s.trades == []              # 无幽灵台账
    assert g.reserved == 0.0           # 无幽灵占用
    assert any("未成交" in m for m in n.warns)


def test_live_rejected_order_no_reserve(monkeypatch):
    _mk(monkeypatch, dry_run=False, edge_verified=True)
    n, g = FakeNotifier(), FakeGuard()
    ex = auto_module.AutoExecutor(FakeStore(), n, g)
    ex._client = FakeClient(Rejected())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal())
    assert any("下单被拒" in m for m in n.warns)
    assert g.reserved == 0.0                 # 未占用
    assert getattr(ex._store, "trades", []) == []  # 下单失败不写台账


# ── 门6：最大在场持仓数（链上持仓笔数封顶）──────────────────
class FakeReader:
    """假 PortfolioReader：可控返回的持仓笔数或 None（查询失败）。"""
    def __init__(self, n_positions):
        self._n = n_positions
    def snapshot(self, *a, **k):
        if self._n is None:
            return None
        class Snap:
            positions = [{"i": i} for i in range(self._n)]
        return Snap()


def test_max_positions_blocks_when_at_limit(monkeypatch):
    """链上已有 10 笔在场、上限 10 → 拒绝再开新仓，不下单。"""
    _mk(monkeypatch, dry_run=False, edge_verified=True, max_open_positions=10)
    n, g = FakeNotifier(), FakeGuard()
    ex = auto_module.AutoExecutor(FakeStore(), n, g, portfolio_reader=FakeReader(10))
    ex._client = FakeClient(Accepted())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal())
    assert ex._client.orders == []                     # 未下单
    assert any("在场持仓" in m for m in n.warns)
    assert g.reserved == 0.0


def test_max_positions_allows_below_limit(monkeypatch):
    """链上 9 笔、上限 10 → 放行，正常下单。"""
    _mk(monkeypatch, dry_run=False, edge_verified=True, max_open_positions=10)
    n, g = FakeNotifier(), FakeGuard()
    ex = auto_module.AutoExecutor(FakeStore(), n, g, portfolio_reader=FakeReader(9))
    ex._client = FakeClient(Accepted())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal())
    assert len(ex._client.orders) == 1                 # 已下单


def test_max_positions_query_fail_allows(monkeypatch):
    """持仓查询失败(None) → 保守放行（笔数上限非安全熔断），但告警。"""
    _mk(monkeypatch, dry_run=False, edge_verified=True, max_open_positions=10)
    n, g = FakeNotifier(), FakeGuard()
    ex = auto_module.AutoExecutor(FakeStore(), n, g, portfolio_reader=FakeReader(None))
    ex._client = FakeClient(Accepted())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal())
    assert len(ex._client.orders) == 1                 # 放行下单
    assert any("持仓数未知" in m for m in n.warns)     # 有告警


def test_max_positions_no_reader_skips_gate(monkeypatch):
    """未注入 reader（如旧调用）→ 不启用该门，正常下单。"""
    _mk(monkeypatch, dry_run=False, edge_verified=True, max_open_positions=10)
    n, g = FakeNotifier(), FakeGuard()
    ex = auto_module.AutoExecutor(FakeStore(), n, g)   # 无 portfolio_reader
    ex._client = FakeClient(Accepted())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal())
    assert len(ex._client.orders) == 1


# ── credentials ───────────────────────────────────────────
def test_load_credentials_missing_file(tmp_path):
    from data.credentials import load_credentials
    assert load_credentials(str(tmp_path / "nope.env")) is None


def test_load_credentials_incomplete(tmp_path):
    from data.credentials import load_credentials
    p = tmp_path / ".env"
    p.write_text("POLYGON_PRIVATE_KEY=abc\nCLOB_API_KEY=\n", encoding="utf-8")
    assert load_credentials(str(p)) is None


def test_load_credentials_complete(tmp_path):
    from data.credentials import load_credentials
    p = tmp_path / ".env"
    p.write_text("POLYGON_PRIVATE_KEY=0xabcd\nCLOB_API_KEY=k\n"
                 "CLOB_API_SECRET=s\nCLOB_API_PASSPHRASE=p\n", encoding="utf-8")
    c = load_credentials(str(p))
    assert c is not None and c.api_key == "k"
    assert "…abcd" in c.redacted()  # 摘要只露尾部
