"""运行时配置持久化：面板改的参数写盘，重启后 load 恢复（不丢设置）。"""
import json

from config import Config


def test_apply_persists_to_file(tmp_path):
    p = str(tmp_path / "cfg.json")
    c = Config(runtime_config_path=p)
    c.apply({"max_open_positions": 7, "max_single_order_usdc": 1.5})
    data = json.loads(open(p, encoding="utf-8").read())
    assert data["max_open_positions"] == 7
    assert data["max_single_order_usdc"] == 1.5


def test_load_runtime_restores_values(tmp_path):
    p = str(tmp_path / "cfg.json")
    # 第一个实例：改参数并写盘
    c1 = Config(runtime_config_path=p)
    c1.apply({"max_open_positions": 5, "max_single_order_usdc": 2.0,
              "enable_arb_auto": True})
    # 第二个实例（模拟重启）：默认值 → load 后恢复
    c2 = Config(runtime_config_path=p)
    assert c2.max_open_positions == 10   # 默认
    c2.load_runtime()
    assert c2.max_open_positions == 5     # 恢复
    assert c2.max_single_order_usdc == 2.0
    assert c2.enable_arb_auto is True


def test_load_runtime_missing_file_ok(tmp_path):
    """无配置文件（首次运行）→ load 不报错，保持默认。"""
    c = Config(runtime_config_path=str(tmp_path / "nope.json"))
    c.load_runtime()
    assert c.max_open_positions == 10


def test_load_runtime_ignores_unknown_and_secrets(tmp_path):
    """加载只认白名单字段，脏数据/非白名单字段被忽略（安全）。"""
    p = str(tmp_path / "cfg.json")
    open(p, "w", encoding="utf-8").write(json.dumps({
        "max_open_positions": 8,
        "POLYGON_PRIVATE_KEY": "leaked",   # 非白名单，必须忽略
        "bogus_field": 123,
    }))
    c = Config(runtime_config_path=p)
    c.load_runtime()
    assert c.max_open_positions == 8
    assert not hasattr(c, "POLYGON_PRIVATE_KEY")
    assert not hasattr(c, "bogus_field")


def test_apply_autoloads_before_save(tmp_path):
    """根治数据丢失：未显式 load 的实例 apply 无关字段时，
    必须先自动加载文件，绝不能用内存默认值覆盖磁盘已存的其它参数。"""
    import json
    p = str(tmp_path / "cfg.json")
    # 文件里已有上次存的 10
    json.dump({"max_single_order_usdc": 10.0, "max_open_positions": 8},
              open(p, "w", encoding="utf-8"))
    # 新实例，未调 load_runtime（模拟老进程/忘了加载）
    c = Config(runtime_config_path=p)
    assert c.max_single_order_usdc == 0.0  # 内存还是默认
    # 只改一个无关字段
    c.apply({"paused": True})
    # 文件里的 10 必须还在（不被内存默认 0 覆盖）
    data = json.load(open(p, encoding="utf-8"))
    assert data["max_single_order_usdc"] == 10.0, "已存参数被内存默认覆盖了！"
    assert data["max_open_positions"] == 8
    assert data["paused"] is True
    # 内存也应被文件值填充
    assert c.max_single_order_usdc == 10.0


def test_corrupt_file_does_not_crash(tmp_path):
    """配置文件损坏 → load 不崩，保持默认（不拖垮启动）。"""
    p = str(tmp_path / "cfg.json")
    open(p, "w", encoding="utf-8").write("{not valid json")
    c = Config(runtime_config_path=p)
    c.load_runtime()   # 不抛异常
    assert c.max_open_positions == 10
