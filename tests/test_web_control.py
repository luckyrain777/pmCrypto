"""网页控制台 API 测试：配置读写 / 运维动作 / edge 报告 / 暂停生效。"""
import pytest
from starlette.testclient import TestClient

import config as config_module
from config import CONFIG
from core.state import STATE
from data.store import Store
from web.server import create_app


@pytest.fixture(autouse=True)
def clean_state():
    """快照并恢复全局 CONFIG/STATE，避免测试间污染。"""
    snap = CONFIG.as_dict()
    STATE.reset()
    yield
    CONFIG.apply({k: v for k, v in snap.items() if k in
                  ("executor_mode", "paused", "enable_edge_strategy",
                   "enable_crypto_signal", "poll_interval_sec")})
    CONFIG.paused = snap["paused"]
    CONFIG.executor_mode = snap["executor_mode"]
    STATE.reset()


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "web_test.db")  # 每个测试独立目录，避免文件锁冲突
    return TestClient(create_app(Store(db)))


def test_state_returns_config_and_run_state(client):
    d = client.get("/api/state").json()
    assert "run_state" in d and "config" in d
    assert "executor_mode" in d["config"]


def test_config_post_applies_whitelisted(client):
    r = client.post("/api/config", json={"poll_interval_sec": 33,
                                         "enable_crypto_signal": False,
                                         "bogus_field": 1})
    d = r.json()
    assert d["applied"]["poll_interval_sec"] == 33
    assert d["applied"]["enable_crypto_signal"] is False
    assert "bogus_field" not in d["applied"]


def test_config_switch_to_auto_returns_note(client):
    d = client.post("/api/config", json={"executor_mode": "auto"}).json()
    assert d["applied"]["executor_mode"] == "auto"
    assert "auto" in d["note"]


def test_control_halt_and_resume(client):
    d = client.post("/api/control", json={"action": "halt"}).json()
    assert d["ok"] and d["run_state"]["state"] == "halted"
    d = client.post("/api/control", json={"action": "resume"}).json()
    assert d["ok"] and d["run_state"]["state"] == "running"


def test_control_pause_unpause(client):
    d = client.post("/api/control", json={"action": "pause"}).json()
    assert d["config"]["paused"] is True
    d = client.post("/api/control", json={"action": "unpause"}).json()
    assert d["config"]["paused"] is False


def test_control_unknown_action_400(client):
    r = client.post("/api/control", json={"action": "nuke"})
    assert r.status_code == 400


def test_edge_report_endpoint(client):
    d = client.post("/api/edge-report").json()
    assert "summary" in d and "bets" in d and "significant" in d


def test_go_live_blocked_without_edge_and_creds(client, monkeypatch):
    # 无凭证 + 空库(edge不显著) → 应被拒绝并列出 blockers
    monkeypatch.setattr("data.credentials.load_credentials",
                        lambda env_path=".env": None)
    d = client.post("/api/go-live").json()
    assert d["ok"] is False
    assert len(d["blockers"]) >= 1
    # 未被切到真钱
    assert d["config"]["dry_run"] is True or d["config"]["executor_mode"] == "manual"


def test_go_safe_returns_to_safe(client):
    # 先手动切到危险态，再 go-safe 应拉回
    client.post("/api/config", json={"executor_mode": "auto", "dry_run": False})
    d = client.post("/api/go-safe").json()
    assert d["ok"] is True
    assert d["config"]["executor_mode"] == "manual"
    assert d["config"]["dry_run"] is True


def test_paused_skips_cycle(monkeypatch):
    """paused=True 时 run_cycle 直接返回，不抓取。"""
    import main
    calls = {"fetch": 0}

    class DummySource:
        def fetch_markets(self, limit):
            calls["fetch"] += 1
            return []

    class DummyNotifier:
        def info(self, m): pass
        def warning(self, m): pass

    CONFIG.paused = True
    try:
        main.run_cycle(DummySource(), None, None, None, DummyNotifier())
        assert calls["fetch"] == 0  # 暂停时未抓取
    finally:
        CONFIG.paused = False
