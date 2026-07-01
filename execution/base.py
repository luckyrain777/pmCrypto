"""执行器抽象接口。

main 主循环只认这个接口。阶段 C 注入 ManualExecutor（只提示不发单），
阶段 A 注入 AutoExecutor（真发单），切换只改 config.executor_mode 一行，
主循环与策略零改动 —— 这是“执行层可插拔”的核心。
"""
from __future__ import annotations

from typing import Protocol

from strategy.models import Signal


class Executor(Protocol):
    """执行器协议。所有实现都接收一个已通过风控的 Signal。"""

    def execute(self, signal: Signal) -> None:
        """处理一个交易信号。

        ManualExecutor: 打印 + 写日志 + 存库，不发单。
        AutoExecutor:   真实下单（切 A 后实现），并受 core.state 急停管辖。
        """
        ...
