"""只读组合查询：用【公开地址】查真实余额/持仓/盈亏。

只读、不签名、不下单——查这些只需要钱包地址，不需要私钥参与交易。
地址来源优先级：
  1) .env 的 POLYMARKET_ADDRESS（最可靠，尤其 Polymarket 的 proxy 钱包）
  2) 从 POLYGON_PRIVATE_KEY 本地推导 EOA 地址（可能与 Polymarket 存款地址不同）

带缓存（默认 30s），失败降级返回 None（不拖垮控制台）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional


def resolve_address(env_path: str = ".env") -> Optional[str]:
    """解析用于查询的公开地址。"""
    try:
        from dotenv import dotenv_values
    except Exception:
        return None
    if not os.path.exists(env_path):
        return None
    vals = dotenv_values(env_path)

    addr = (vals.get("POLYMARKET_ADDRESS") or "").strip()
    if addr:
        return addr

    # 回落：从私钥本地推导 EOA 地址（仅用于只读查询，不用于签名）。
    pk = (vals.get("POLYGON_PRIVATE_KEY") or "").strip()
    if pk:
        try:
            from eth_account import Account
            return Account.from_key(pk).address
        except Exception:
            return None
    return None


@dataclass
class PortfolioSnapshot:
    address: str
    cash_usdc: Optional[float]      # 现金 USDC 余额（需凭证查；None=查不到）
    positions_value_usdc: float     # 持仓市值合计
    total_value_usdc: float         # 现金 + 持仓市值
    positions: list          # [{title, outcome, size, avg_price, cur_price, current_value, cash_pnl}]
    total_cash_pnl_usdc: float


class PortfolioReader:
    def __init__(self, cache_ttl_sec: float = 30.0):
        self.cache_ttl = cache_ttl_sec
        self._cache: Optional[PortfolioSnapshot] = None
        self._cache_ts: float = 0.0
        self._client = None
        self._secure = None

    def _pub(self):
        if self._client is None:
            from polymarket import PublicClient
            self._client = PublicClient()
        return self._client

    def _query_cash(self, env_path: str) -> Optional[float]:
        """用凭证查现金 USDC 余额（只读）。无凭证/失败返回 None。

        Polymarket 余额是最小单位（6 位小数），除以 1e6 得美元。
        """
        from data.credentials import load_credentials
        creds = load_credentials(env_path)
        if creds is None:
            return None
        try:
            if self._secure is None:
                from polymarket import SecureClient, ApiKeyCreds
                self._secure = SecureClient.create(
                    private_key=creds.private_key,
                    credentials=ApiKeyCreds(key=creds.api_key,
                                            secret=creds.api_secret,
                                            passphrase=creds.api_passphrase))
            ba = self._secure.get_balance_allowance(asset_type="COLLATERAL")
            raw = getattr(ba, "balance", None)
            if raw is None:
                return None
            return round(float(raw) / 1e6, 2)
        except Exception:
            return None

    def snapshot(self, env_path: str = ".env") -> Optional[PortfolioSnapshot]:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self.cache_ttl:
            return self._cache

        addr = resolve_address(env_path)
        if not addr:
            return None

        try:
            c = self._pub()
            # 持仓市值合计
            pv = c.get_portfolio_values(user=addr)
            positions_value = 0.0
            for item in pv:
                v = getattr(item, "value", None)
                if v is not None:
                    positions_value += float(v)
            # 现金 USDC 余额（需凭证）
            cash = self._query_cash(env_path)
            total_value = (cash or 0.0) + positions_value

            # 持仓明细（取前若干条）
            positions = []
            total_pnl = 0.0
            try:
                pag = c.list_positions(user=addr, page_size=50)
                for p in pag.iter_items():
                    cp = float(getattr(p, "cash_pnl", None) or 0)
                    total_pnl += cp
                    positions.append({
                        "title": getattr(p, "title", None) or "",
                        "outcome": getattr(p, "outcome", None) or "",
                        "size": float(getattr(p, "size", None) or 0),
                        "avg_price": float(getattr(p, "avg_price", None) or 0),
                        "cur_price": float(getattr(p, "cur_price", None) or 0),
                        "current_value": float(getattr(p, "current_value", None) or 0),
                        "cash_pnl": cp,
                    })
                    if len(positions) >= 50:
                        break
            except Exception:
                pass

            snap = PortfolioSnapshot(
                address=addr,
                cash_usdc=cash,
                positions_value_usdc=round(positions_value, 2),
                total_value_usdc=round(total_value, 2),
                positions=positions,
                total_cash_pnl_usdc=round(total_pnl, 2),
            )
            self._cache, self._cache_ts = snap, now
            return snap
        except Exception:
            return None
