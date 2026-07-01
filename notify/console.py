"""终端打印 + 写日志文件。

同一条信息既打到终端（你盯屏幕用）也写入日志文件（事后留痕）。
日志绝不写入任何密钥/私钥。
"""
from __future__ import annotations

import logging
import os
import sys

from config import CONFIG
from strategy.models import Signal


def _ensure_utf8_console() -> None:
    """Windows 默认控制台编码常为 GBK，输出中文/符号会抛 UnicodeEncodeError。
    强制把 stdout/stderr 重新配置为 UTF-8（Python 3.7+）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 某些环境 stream 不可重配，忽略即可
                pass


class Notifier:
    def __init__(self, log_path: str = CONFIG.log_path):
        _ensure_utf8_console()
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        self._logger = logging.getLogger("pmcrypto")
        if not self._logger.handlers:
            self._logger.setLevel(logging.INFO)
            fmt = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s"
            )
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            self._logger.addHandler(fh)
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            self._logger.addHandler(sh)

    def info(self, msg: str) -> None:
        self._logger.info(msg)

    def warning(self, msg: str) -> None:
        self._logger.warning(msg)

    def announce_signal(self, signal: Signal) -> None:
        legs = ", ".join(f"{tid[:8]}…@{px:.3f}" for tid, px in signal.legs)
        self._logger.info(
            "【建议下单 · 人工确认】市场 %s | 类型 %s | 净收益 %.4f "
            "| 建议名义额 %.2f USDC | 腿 [%s] | %s",
            signal.market_id,
            signal.kind.value,
            signal.net_edge,
            signal.suggested_size_usdc,
            legs,
            signal.reason,
        )
