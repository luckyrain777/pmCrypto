"""密钥/凭证加载 —— 只从本地 .env 读，绝不硬编码、绝不写日志。

dry-run 模式完全不需要凭证；只有真实下单(非 dry_run)才需要。
缺失任何一项都返回 None，交由 auto 执行器拒绝真实下单。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TradingCredentials:
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str

    def redacted(self) -> str:
        """安全摘要（供日志/展示），绝不暴露完整密钥。"""
        pk = self.private_key
        tail = pk[-4:] if len(pk) >= 4 else "?"
        return f"钱包私钥(…{tail}) + API凭证已加载"


def load_credentials(env_path: str = ".env") -> Optional[TradingCredentials]:
    """从 .env 加载交易凭证。任一缺失返回 None。"""
    # 懒加载 dotenv，避免非交易路径的硬依赖。
    try:
        from dotenv import dotenv_values
    except Exception:
        return None

    if not os.path.exists(env_path):
        return None

    vals = dotenv_values(env_path)
    pk = (vals.get("POLYGON_PRIVATE_KEY") or "").strip()
    key = (vals.get("CLOB_API_KEY") or "").strip()
    secret = (vals.get("CLOB_API_SECRET") or "").strip()
    passphrase = (vals.get("CLOB_API_PASSPHRASE") or "").strip()

    if not (pk and key and secret and passphrase):
        return None

    return TradingCredentials(
        private_key=pk, api_key=key, api_secret=secret, api_passphrase=passphrase
    )
