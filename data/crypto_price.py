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

import math
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

# 各资产默认年化波动率（拉不到历史 K 线时的降级兜底）。
DEFAULT_ANNUAL_VOL = {
    "bitcoin": 0.55, "ethereum": 0.70, "solana": 0.90, "dogecoin": 1.10,
}

# 校准用的日 K 线根数（≈30 天）与缓存时长（波动率变化慢，缓存 1 小时足够）。
_VOL_KLINE_DAYS = 30
_VOL_CACHE_TTL = 3600.0


def realized_annual_vol(closes) -> Optional[float]:
    """从一段日收盘价序列算已实现年化波动率。

    方法：日对数收益的样本标准差 × sqrt(365)。这是把“过去真实发生的波动”
    直接量化，替代写死的常数——用市场当前状态而非先验拍脑袋。

    需 ≥3 个收盘价（≥2 个收益）才可估计；不足返回 None（交由调用方降级）。
    价格完全不动返回 0.0。
    """
    if closes is None or len(closes) < 3:
        return None
    rets = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev is None or cur is None or prev <= 0 or cur <= 0:
            continue
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)  # 样本方差
    return math.sqrt(var) * math.sqrt(365.0)


class CryptoPriceSource:
    def __init__(self, cache_ttl_sec: float = 30.0):
        self.cache_ttl = cache_ttl_sec
        self._cache: dict[str, tuple[float, float]] = {}  # asset -> (price, ts)
        self._vol_cache: dict[str, tuple[float, float]] = {}  # asset -> (vol, ts)

    def annual_vol(self, asset: str) -> float:
        """资产年化波动率：优先用近 30 日真实 K 线校准，失败降级到常数。

        校准结果缓存 1 小时（波动率变化慢）。这消除了写死常数导致的
        系统性定价偏差——用市场当前真实波动，而非先验拍脑袋。
        """
        key = asset.lower()
        fallback = DEFAULT_ANNUAL_VOL.get(key, 0.60)

        now = time.time()
        cached = self._vol_cache.get(key)
        if cached and (now - cached[1]) < _VOL_CACHE_TTL:
            return cached[0]

        closes = self._fetch_daily_closes(key)
        vol = realized_annual_vol(closes)
        if vol is None or vol <= 0.0:
            return fallback  # 拉取失败/样本不足 → 降级（不缓存，下轮再试）
        self._vol_cache[key] = (vol, now)
        return vol

    def _fetch_daily_closes(self, asset: str) -> Optional[list]:
        """拉近 _VOL_KLINE_DAYS 根日 K 线的收盘价（Binance）。失败返回 None。

        Binance klines：每根为数组，收盘价在索引 4（字符串数字）。
        """
        pair = _BINANCE.get(asset)
        if not pair:
            return None
        try:
            r = httpx.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": pair, "interval": "1d",
                        "limit": _VOL_KLINE_DAYS},
                timeout=8,
            )
            r.raise_for_status()
            rows = r.json()
            closes = [float(row[4]) for row in rows if len(row) > 4]
            return closes or None
        except Exception:
            return None

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
