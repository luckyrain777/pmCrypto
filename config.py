"""全局配置。

只放策略/风控/运行参数，**不放任何密钥**（密钥在 .env）。
所有数值都集中在这里，便于调参而不动逻辑代码。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, fields


# 允许网页控制台在运行时修改的字段白名单（含类型），防止乱改内部字段。
_MUTABLE_FIELDS = {
    "executor_mode": str,
    "poll_interval_sec": float,
    "max_markets_per_cycle": int,
    "prefer_short_expiry_days": int,
    "account_balance_usdc": float,
    "min_profit_threshold": float,
    "min_liquidity_usdc": float,
    "market_max_slippage_pct": float,
    "enable_edge_strategy": bool,
    "enable_crypto_signal": bool,
    "enable_arb_auto": bool,
    "edge_min_threshold": float,
    "kelly_fraction_min": float,
    "kelly_fraction_max": float,
    "kelly_max_single_pct": float,
    "max_single_order_usdc": float,
    "halt_balance_daily": float,
    "halt_balance_total": float,
    "max_consecutive_losses": int,
    "edge_top_n_per_cycle": int,
    "max_open_positions": int,
    "paused": bool,
    "dry_run": bool,
    "edge_verified": bool,
}


@dataclass
class Config:
    # ── 运行 ───────────────────────────────────────────────
    # 执行器模式：'manual'（阶段C，只提示不发单）/ 'auto'（阶段A，真发单）
    executor_mode: str = "manual"
    # 是否暂停扫描（网页控制台可切换；暂停时主循环空转不交易）
    paused: bool = False
    # 干跑：True 时 auto 执行器走完全部逻辑但不真正发送订单（零风险验证）。
    # 默认 True 是双保险——即使切了 auto，不显式关掉 dry_run 也不会花钱。
    dry_run: bool = True
    # edge 是否已通过验证。默认 False。只有 edge_report 判 [OK] 后才应置 True。
    # auto 在“非 dry_run 且 edge 未验证”时拒绝真实下单（科学门槛的代码兜底）。
    edge_verified: bool = False
    # 套利自动下单开关（Yes/No 互补、多结果概率和）。默认 False。
    # 套利无需 edge 验证（无风险、纯算术），故与方向性的 edge_verified 独立；
    # 但仍需凭证齐全 + 非 dry_run 才真下单。
    enable_arb_auto: bool = False
    # 主循环轮询间隔（秒）。保守起步，避免撞 API 限频。
    poll_interval_sec: float = 45.0
    # 每轮最多扫描多少个市场（控制 API 压力）。
    max_markets_per_cycle: int = 300
    # 优先扫描“近到期”市场的窗口（天）：只抓从现在起 N 天内到期、未关闭的市场，
    # 并按到期升序。近到期市场结算快 → 已结算样本积累快 → edge 验证更快达标。
    # 0 = 不过滤（回到扫描全部市场的旧行为）。
    prefer_short_expiry_days: int = 14

    # ── 账户基数（仅影响风控的“占比”换算，不含任何敏感信息）──
    account_balance_usdc: float = 100.0

    # ── 策略阈值 ──────────────────────────────────────────
    # 净收益低于此比例则不提示（已覆盖成本 + 安全垫）。0.015 = 1.5%
    min_profit_threshold: float = 0.015
    # 盘口深度（可成交名义量，USDC）低于此值的市场直接丢弃。
    min_liquidity_usdc: float = 20.0

    # ── 成本参数 ──────────────────────────────────────────
    # Polymarket 交易手续费率（按需调整为实际值）。0.0 表示当前免手续费。
    fee_rate: float = 0.0
    # 滑点安全系数：估算滑点时额外乘以此系数，**强制保守**。
    # 1.0 = 不放大；1.5 = 把估算滑点放大 50% 以留安全垫。
    slippage_safety_factor: float = 1.5
    # FOK 市价单的最大滑点容差：下单 max_price = 信号价 *(1+此值)。
    # 超过则 FOK 直接取消（不成交），防止吃到远离信号价的深盘口。0.03 = 3%。
    market_max_slippage_pct: float = 0.03

    # ── 风控闸门 ──────────────────────────────────────────
    max_position_pct: float = 0.10       # 单笔最多占账户 10%（阶段A套利）
    max_total_exposure_pct: float = 0.50  # 同时在场总仓位上限 50%
    daily_max_loss_pct: float = 0.20      # 单日亏损触及即熔断（切A后生效）

    # ── 阶段B：方向性 edge + 自适应 Kelly 复利 ─────────────
    enable_edge_strategy: bool = True     # 是否启用阶段B方向性策略
    enable_crypto_signal: bool = True     # 是否启用加密现价误定价外部信号
    edge_min_threshold: float = 0.05      # 机会稀缺性门槛：edge 低于此不出手
    kelly_fraction_min: float = 0.25      # 自适应 Kelly 分数下界(¼ Kelly)
    kelly_fraction_max: float = 0.50      # 自适应 Kelly 分数上界(½ Kelly)
    kelly_max_single_pct: float = 0.20    # 单笔 Kelly 硬封顶(占余额比例)
    # 单笔金额硬上限(USDC，绝对值)：所有策略(方向性+套利)单笔都不超此值。
    # 实测阶段的“保险丝”——设成 1~2 USDC 可把每笔死死摁住。0 = 不限。
    max_single_order_usdc: float = 0.0
    # 两道保命熔断线（大胆但不删）+ 连亏熔断
    halt_balance_daily: float = 60.0      # 余额跌破此值当日停手
    halt_balance_total: float = 30.0      # 余额跌破此值彻底停（放宽自40→30）
    max_consecutive_losses: int = 5       # 连亏达此数触发熔断（信号可能失效）
    edge_top_n_per_cycle: int = 3         # 每轮只提示 edge 最大的前 N 个（精选）
    # 真钱自动交易：钱包当前在场持仓达此笔数时，拒绝再自动开新仓（平仓后自动腾额度）。
    # 口径为链上真实持仓总数（含手动开的仓）。首次上真钱的“同时持仓数”护栏。
    max_open_positions: int = 10

    # ── API 稳健性 ────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    api_max_retries: int = 3              # 单次请求最大重试次数
    api_backoff_base_sec: float = 1.0     # 退避基数（指数退避）
    api_failure_threshold: int = 10       # 累计失败达此值触发全局急停

    # ── 存储 / 日志 ───────────────────────────────────────
    db_path: str = "data/pmcrypto.db"
    # 运行时可变参数的持久化文件：面板改的参数写这里，重启后 load 恢复。
    # 只存白名单字段(mutable_dict)，绝不含密钥。
    runtime_config_path: str = "data/config.runtime.json"
    # 是否已从磁盘加载过运行时参数。防"未加载就保存→用内存默认覆盖磁盘"的数据丢失。
    _loaded: bool = field(default=False, repr=False)
    log_path: str = "logs/pmcrypto.log"

    # ── 网页面板 ──────────────────────────────────────────
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # ── 运行时读写（供网页控制台）────────────────────────
    def as_dict(self) -> dict:
        """导出所有字段（供网页展示）。"""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def mutable_dict(self) -> dict:
        """只导出网页可修改的字段。"""
        return {k: getattr(self, k) for k in _MUTABLE_FIELDS}

    def apply(self, updates: dict) -> dict:
        """按白名单+类型校验地写入若干字段。返回 {字段: 新值} 实际生效的部分。

        非白名单字段被忽略；类型不合法的被跳过；数值做基本 clamp。
        线程安全（网页线程写、主循环读）。
        """
        # 防数据丢失：首次修改前，若还没从磁盘加载过，先加载——否则 save_runtime
        # 会用内存默认值覆盖磁盘上已存的其它参数。（load_runtime 自带锁，须在锁外调）
        if not self._loaded:
            self.load_runtime()
        applied = {}
        with _config_lock:
            for key, raw in updates.items():
                if key not in _MUTABLE_FIELDS:
                    continue
                typ = _MUTABLE_FIELDS[key]
                try:
                    if typ is bool:
                        val = raw if isinstance(raw, bool) else \
                            str(raw).lower() in ("true", "1", "yes", "on")
                    elif typ is int:
                        val = int(raw)
                    elif typ is float:
                        val = float(raw)
                    else:
                        val = str(raw)
                except (TypeError, ValueError):
                    continue

                # 逐字段合理性约束
                if key == "executor_mode" and val not in ("manual", "auto"):
                    continue
                if key in ("kelly_fraction_min", "kelly_fraction_max",
                           "kelly_max_single_pct", "min_profit_threshold",
                           "edge_min_threshold") and not (0.0 <= val <= 1.0):
                    continue
                if key == "poll_interval_sec" and val < 5.0:
                    val = 5.0
                if key == "max_markets_per_cycle" and val < 1:
                    val = 1
                if key == "max_open_positions" and val < 1:
                    val = 1
                if key == "prefer_short_expiry_days" and val < 0:
                    val = 0
                if key == "max_single_order_usdc" and val < 0:
                    val = 0.0

                setattr(self, key, val)
                applied[key] = val

            # 维持 kelly_min <= kelly_max 不变式
            if self.kelly_fraction_min > self.kelly_fraction_max:
                self.kelly_fraction_min = self.kelly_fraction_max
                applied["kelly_fraction_min"] = self.kelly_fraction_min
        # 锁外写盘：面板改的参数持久化，重启后 load_runtime 恢复（不丢设置）。
        if applied:
            self.save_runtime()
        return applied

    # ── 运行时持久化：面板参数写盘 + 重启恢复 ──────────────
    def save_runtime(self) -> None:
        """把白名单可变字段写入 runtime_config_path（不含密钥）。失败静默。"""
        try:
            path = self.runtime_config_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.mutable_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 持久化失败不应影响交易主流程

    def load_runtime(self) -> None:
        """启动时读取持久化参数并应用（只认白名单，脏数据/损坏文件安全忽略）。"""
        self._loaded = True  # 标记已尝试加载：后续 apply 不再重复触发，且允许安全 save
        try:
            with open(self.runtime_config_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return  # 文件不存在/损坏 → 保持默认，不崩
        if isinstance(data, dict):
            # 复用 apply 的白名单+类型校验；但 apply 会再触发 save_runtime，
            # 这里是加载不该回写，故直接走白名单 setattr 避免多余写盘。
            with _config_lock:
                for key, raw in data.items():
                    if key not in _MUTABLE_FIELDS:
                        continue
                    typ = _MUTABLE_FIELDS[key]
                    try:
                        if typ is bool:
                            val = raw if isinstance(raw, bool) else \
                                str(raw).lower() in ("true", "1", "yes", "on")
                        elif typ is int:
                            val = int(raw)
                        elif typ is float:
                            val = float(raw)
                        else:
                            val = str(raw)
                    except (TypeError, ValueError):
                        continue
                    setattr(self, key, val)


# 运行时配置写锁（网页控制台并发写保护）。
_config_lock = threading.Lock()

# 单例配置，全项目导入此对象。
CONFIG = Config()
