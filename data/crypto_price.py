"""加密现价数据源（阶段B外部信号——最有效的那一个）。

职责：拉取 BTC/ETH 等的实时现价，供 crypto_mispricing 信号用对数正态模型
算"到期时突破目标价的真实概率"。

设计（小而精，不庞大）：
- 主源 Coinbase 现货价，备源 Binance；两者都免费、无需 key。
- 现价带短时缓存（默认 30s），避免每个市场都发一次请求。
- 失败降级：拉不到返回 None，绝不抛异常拖垮主循环。
- 波动率：第一版用可配置的年化波动率常数（BTC 典型 ~55%）。预留从历史
  价格序列估算的接口，但不在第一版强做——避免过度设计。
"""
from __future__ import annotations

import time
from typing import Optional

import httpx


# 资产别名 → Coinbase / Binance 交易对。
_COINBASE = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
}
_BINANCE = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
}

# 各资产默认年化波动率（先验，可后续用历史价格校准）。
DEFAULT_ANNUAL_VOL = {
    "bitcoin": 0.55, "ethereum": 0.70, "solana": 0.90, "dogecoin": 1.10,
}


class CryptoPriceSource:
    def __init__(self, cache_ttl_sec: float = 30.0):
        self.cache_ttl = cache_ttl_sec
        self._cache: dict[str, tuple[float, float]] = {}  # asset -> (price, ts)

    def annual_vol(self, asset: str) -> float:
        return DEFAULT_ANNUAL_VOL.get(asset.lower(), 0.60)

    def spot(self, asset: str) -> Optional[float]:
        """返回资产现价（USD），失败返回 None。带缓存。"""
        key = asset.lower()
        now = time.time()
        cached = self._cache.get(key)
        if cached and (now - cached[1]) < self.cache_ttl:
            return cached[0]

        price = self._fetch_coinbase(key)
        if price is None:
            price = self._fetch_binance(key)
        if price is not None:
            self._cache[key] = (price, now)
        return price

    def _fetch_coinbase(self, asset: str) -> Optional[float]:
        pair = _COINBASE.get(asset)
        if not pair:
            return None
        try:
            r = httpx.get(
                f"https://api.coinbase.com/v2/prices/{pair}/spot", timeout=8
            )
            r.raise_for_status()
            return float(r.json()["data"]["amount"])
        except Exception:
            return None

    def _fetch_binance(self, asset: str) -> Optional[float]:
        pair = _BINANCE.get(asset)
        if not pair:
            return None
        try:
            r = httpx.get(
                f"https://api.binance.com/api/v3/ticker/price?symbol={pair}",
                timeout=8,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            return None
