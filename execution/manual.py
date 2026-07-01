"""阶段 C 执行器：只提示，不发单。

收到信号 → 终端打印 + 写日志 + 存入 SQLite。绝不接触任何下单 API、
绝不需要私钥。这是“自动分析 + 人工确认”模式的落点：系统把功课做完，
最后一步由你人工决定要不要真的下单。
"""
from __future__ import annotations

import time

from data.store import Store
from notify.console import Notifier
from strategy.models import Signal


class ManualExecutor:
    def __init__(self, store: Store, notifier: Notifier):
        self._store = store
        self._notifier = notifier

    def execute(self, signal: Signal) -> None:
        # 1) 存库留痕
        self._store.save_signal(signal, created_ts=time.time())
        # 2) 终端 + 日志提示（人工确认用）
        self._notifier.announce_signal(signal)
