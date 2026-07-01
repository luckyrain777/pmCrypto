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
    def __init__(self): self.saved = []
    def save_signal(self, sig, created_ts): self.saved.append(sig)


class FakeGuard:
    def __init__(self, bal=100.0): self.account_balance_usdc = bal; self.reserved = 0.0
    def reserve(self, x): self.reserved += x


class Accepted:
    ok = True; order_id = "ORD1"


class Rejected:
    ok = False; code = "X"; message = "insufficient"


class FakeClient:
    def __init__(self, resp): self.resp = resp; self.orders = []
    def get_balance_allowance(self, *, asset_type):
        class B: balance = 100.0
        return B()
    def place_limit_order(self, **kw): self.orders.append(kw); return self.resp


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
    assert o["side"] == "BUY" and o["token_id"] == "token123456"
    assert abs(o["size"] - 20.0) < 1e-6      # 10 USDC / 0.5 = 20 份
    assert g.reserved == 10.0                # 占用仓位
    assert len(s.saved) == 1


def test_live_rejected_order_no_reserve(monkeypatch):
    _mk(monkeypatch, dry_run=False, edge_verified=True)
    n, g = FakeNotifier(), FakeGuard()
    ex = auto_module.AutoExecutor(FakeStore(), n, g)
    ex._client = FakeClient(Rejected())
    monkeypatch.setattr(ex, "_ensure_client", lambda: True)
    ex.execute(_edge_signal())
    assert any("下单被拒" in m for m in n.warns)
    assert g.reserved == 0.0                 # 未占用


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
