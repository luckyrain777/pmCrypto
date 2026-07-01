"""全局配置。

只放策略/风控/运行参数，**不放任何密钥**（密钥在 .env）。
所有数值都集中在这里，便于调参而不动逻辑代码。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields


# 允许网页控制台在运行时修改的字段白名单（含类型），防止乱改内部字段。
_MUTABLE_FIELDS = {
    "executor_mode": str,
    "poll_interval_sec": float,
    "max_markets_per_cycle": int,
    "account_balance_usdc": float,
    "min_profit_threshold": float,
    "min_liquidity_usdc": float,
    "enable_edge_strategy": bool,
    "enable_crypto_signal": bool,
    "edge_min_threshold": float,
    "kelly_fraction_min": float,
    "kelly_fraction_max": float,
    "kelly_max_single_pct": float,
    "halt_balance_daily": float,
    "halt_balance_total": float,
    "max_consecutive_losses": int,
    "edge_top_n_per_cycle": int,
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
    # 主循环轮询间隔（秒）。保守起步，避免撞 API 限频。
    poll_interval_sec: float = 45.0
    # 每轮最多扫描多少个市场（控制 API 压力）。
    max_markets_per_cycle: int = 300

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
    kelly_max_single_pct: float = 0.20    # 单笔 Kelly 硬封顶(占余额)
    # 两道保命熔断线（大胆但不删）+ 连亏熔断
    halt_balance_daily: float = 60.0      # 余额跌破此值当日停手
    halt_balance_total: float = 30.0      # 余额跌破此值彻底停（放宽自40→30）
    max_consecutive_losses: int = 5       # 连亏达此数触发熔断（信号可能失效）
    edge_top_n_per_cycle: int = 3         # 每轮只提示 edge 最大的前 N 个（精选）

    # ── API 稳健性 ────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    api_max_retries: int = 3              # 单次请求最大重试次数
    api_backoff_base_sec: float = 1.0     # 退避基数（指数退避）
    api_failure_threshold: int = 10       # 累计失败达此值触发全局急停

    # ── 存储 / 日志 ───────────────────────────────────────
    db_path: str = "data/pmcrypto.db"
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

                setattr(self, key, val)
                applied[key] = val

            # 维持 kelly_min <= kelly_max 不变式
            if self.kelly_fraction_min > self.kelly_fraction_max:
                self.kelly_fraction_min = self.kelly_fraction_max
                applied["kelly_fraction_min"] = self.kelly_fraction_min
        return applied


# 运行时配置写锁（网页控制台并发写保护）。
_config_lock = threading.Lock()

# 单例配置，全项目导入此对象。
CONFIG = Config()
