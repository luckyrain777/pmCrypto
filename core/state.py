"""全局运行状态 + 急停开关（kill switch）。

这是整个机器人的“总闸”。主循环每轮检查它；risk.guard 与未来的
execution.auto 都必须受它管辖。任何危险信号（连续亏损、API 连续失败、
数据断流、余额异动）都应触发 trip()，使系统进入 HALTED 态、停止一切交易动作。

第一阶段 C 不真发单，但这套骨架先建好，切 A 后立即变成真正的刹车。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum


class RunState(str, Enum):
    RUNNING = "running"   # 正常运行，允许产生信号/下单
    HALTED = "halted"     # 急停，仅维持监控，禁止一切交易动作


@dataclass
class GlobalState:
    """线程安全的全局状态。web 面板与主循环可能并发读，故加锁。"""
    _state: RunState = RunState.RUNNING
    _halt_reason: str = ""
    _api_failure_count: int = 0
    _consecutive_losses: int = 0
    _daily_pnl_usdc: float = 0.0
    _last_cycle_ts: float = 0.0        # 上次完成一轮扫描的时间（供倒计时）
    _last_markets_scanned: int = 0     # 上轮实际扫描的市场数
    _real_trades: int = 0             # 真实成交笔数（auto 下单成功累加）
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── 查询 ──────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._state is RunState.RUNNING

    @property
    def is_halted(self) -> bool:
        return not self.is_running

    def snapshot(self) -> dict:
        """供 web 面板/日志读取的只读快照。"""
        with self._lock:
            return {
                "state": self._state.value,
                "halt_reason": self._halt_reason,
                "api_failure_count": self._api_failure_count,
                "consecutive_losses": self._consecutive_losses,
                "daily_pnl_usdc": round(self._daily_pnl_usdc, 4),
                "last_cycle_ts": self._last_cycle_ts,
                "last_markets_scanned": self._last_markets_scanned,
                "real_trades": self._real_trades,
            }

    def mark_cycle(self, markets_scanned: int, ts: float) -> None:
        """主循环每轮结束时记录，用于面板倒计时与扫描量展示。"""
        with self._lock:
            self._last_cycle_ts = ts
            self._last_markets_scanned = markets_scanned

    def record_real_trade(self) -> None:
        """真实成交一笔（auto 下单成功时调用）。"""
        with self._lock:
            self._real_trades += 1

    # ── 急停控制 ──────────────────────────────────────────
    def trip(self, reason: str) -> None:
        """触发急停。幂等：已急停则只在原因为空时补写原因。"""
        with self._lock:
            if self._state is RunState.RUNNING:
                self._state = RunState.HALTED
                self._halt_reason = reason

    def reset(self) -> None:
        """人工解除急停（清零计数）。仅应在排查后手动调用。"""
        with self._lock:
            self._state = RunState.RUNNING
            self._halt_reason = ""
            self._api_failure_count = 0
            self._consecutive_losses = 0

    # ── 计数器：由 client / 执行器更新，达阈值自动 trip ──────
    def record_api_failure(self, threshold: int) -> None:
        with self._lock:
            self._api_failure_count += 1
            if self._api_failure_count >= threshold and self._state is RunState.RUNNING:
                self._state = RunState.HALTED
                self._halt_reason = (
                    f"API 连续失败 {self._api_failure_count} 次，触发全局急停"
                )

    def record_api_success(self) -> None:
        """一次成功请求清零失败计数（避免偶发失败累积误触发）。"""
        with self._lock:
            self._api_failure_count = 0

    def record_trade_result(self, pnl_usdc: float, daily_max_loss_usdc: float,
                            max_consecutive_losses: int = 0) -> None:
        """记录一笔已结算交易盈亏（切 A 后使用）。

        连续亏损累加；当日累计亏损触及上限、或连亏达上限，则急停。
        max_consecutive_losses<=0 表示不启用连亏熔断。
        """
        with self._lock:
            self._daily_pnl_usdc += pnl_usdc
            if pnl_usdc < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

            if self._state is not RunState.RUNNING:
                return
            if self._daily_pnl_usdc <= -abs(daily_max_loss_usdc):
                self._state = RunState.HALTED
                self._halt_reason = (
                    f"当日亏损 {self._daily_pnl_usdc:.2f} USDC 触及上限，触发全局急停"
                )
            elif (max_consecutive_losses > 0
                  and self._consecutive_losses >= max_consecutive_losses):
                self._state = RunState.HALTED
                self._halt_reason = (
                    f"连续亏损 {self._consecutive_losses} 笔达上限，"
                    f"信号可能失效，触发全局急停"
                )

    def reset_daily(self) -> None:
        """每日重置盈亏统计（不解除已有急停）。"""
        with self._lock:
            self._daily_pnl_usdc = 0.0
            self._consecutive_losses = 0


# 全项目共享的单例。
STATE = GlobalState()
