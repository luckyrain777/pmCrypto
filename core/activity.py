"""活动流：记录系统每轮的实际动作，供网页面板实时展示。

痛点：run_cycle/arb/settlement 的关键动作(扫描结果、真钱下单、结算、风控)
以前只打进控制台日志，面板看不到，用户被迫去看后台。这个内存环形缓冲让面板
能拉到"系统刚才为我做了什么"的时间线。

线程安全（主循环写、web 线程读）。只在内存，进程重启清空（历史留痕仍在日志文件）。
"""
from __future__ import annotations

import threading
from collections import deque


class ActivityLog:
    """线程安全的活动环形缓冲。kind 用于前端着色（scan/order/settle/risk/info）。"""

    def __init__(self, maxlen: int = 200):
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, kind: str, message: str, ts: float) -> None:
        with self._lock:
            self._buf.append({"kind": kind, "message": message, "ts": ts})

    def recent(self, limit: int = 50) -> list[dict]:
        """最近 limit 条，倒序（最新在前）。"""
        with self._lock:
            items = list(self._buf)
        items.reverse()
        return items[:limit]


# 全项目共享的单例。
ACTIVITY = ActivityLog()
