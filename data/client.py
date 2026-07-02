"""市场数据源。

唯一接触外部 Polymarket API 的地方。其余所有代码只依赖 MarketDataSource
协议与标准化的 strategy.models.Market，因此更换 SDK/API 只动这一个文件。

设计要点：
- MarketDataSource 是抽象协议；策略/回测/测试可注入任意实现（含假数据源）。
- PolymarketSource 是真实实现，封装官方统一 SDK（polymarket-client），
  内置限频 + 退避重试 + 失败计数，失败累计达阈值触发 core.state 全局急停。
- 注意：旧的 py-clob-client 已于 2026-05 归档且对生产失效；这里用新统一 SDK。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from config import CONFIG
from core.state import STATE
from strategy.models import Market, OutcomeBook


class MarketDataSource(Protocol):
    """数据源协议。任何实现只需提供 fetch_markets()。"""

    def fetch_markets(self, limit: int) -> list[Market]:
        ...


def _to_float(x) -> Optional[float]:
    """把 Decimal/None 安全转成 float。"""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class PolymarketSource:
    """真实数据源：封装统一 SDK 的 PublicClient（只读，无需凭证）。

    懒加载 SDK：只有真正调用时才 import，避免未安装时整个项目无法导入/测试。
    """

    def __init__(self):
        self._client = None
        # 上一轮抓取中被跳过的市场数与原因样本，供主循环打印，避免“静默吞异常”。
        self.last_skipped: int = 0
        self.last_skip_samples: list[str] = []

    def _ensure_client(self):
        if self._client is None:
            # 懒导入：把对 SDK 的硬依赖限制在真实拉取路径。
            from polymarket import PublicClient  # type: ignore
            self._client = PublicClient()
        return self._client

    def _call_with_retry(self, fn, *args, **kwargs):
        """带指数退避的重试。成功清零失败计数；连续失败累计触发急停。"""
        last_exc: Optional[Exception] = None
        for attempt in range(CONFIG.api_max_retries):
            try:
                result = fn(*args, **kwargs)
                STATE.record_api_success()
                return result
            except Exception as exc:  # noqa: BLE001 - 外部 API 各类异常都需退避
                last_exc = exc
                backoff = CONFIG.api_backoff_base_sec * (2 ** attempt)
                time.sleep(backoff)
        # 重试用尽：记一次失败（达阈值会自动触发全局急停）。
        STATE.record_api_failure(CONFIG.api_failure_threshold)
        raise last_exc if last_exc else RuntimeError("API 调用失败")

    def fetch_markets(self, limit: int) -> list[Market]:
        client = self._ensure_client()
        ts = time.time()

        # 1) 列市场（Gamma 层，二元 yes/no 结构）。
        # 优先近到期：只取从现在起 N 天内到期、未关闭的市场，按到期升序，
        # 让迭代器优先吐出快结算的市场——加速已结算样本积累、edge 验证达标。
        # 窗口为 0 时回到旧行为（不加过滤，扫描全部）。
        kwargs = self._short_expiry_kwargs()
        paginator = self._call_with_retry(client.list_markets, **kwargs)
        markets_iter = self._take(paginator.iter_items(), limit)

        # 2) 逐个市场取盘口，转成标准 Market。
        results: list[Market] = []
        self.last_skipped = 0
        self.last_skip_samples = []
        for m in markets_iter:
            try:
                outcomes = self._build_outcomes(client, m)
            except Exception as exc:  # noqa: BLE001
                # 单个市场失败不应拖垮整轮；记录原因（不再静默）。
                self._note_skip(f"{getattr(m, 'id', '?')}: {exc!r}")
                continue
            if not outcomes:
                self._note_skip(f"{getattr(m, 'id', '?')}: 无有效盘口/腿")
                continue
            results.append(
                Market(
                    market_id=str(getattr(m, "condition_id", None) or m.id),
                    question=m.question or "",
                    outcomes=tuple(outcomes),
                    snapshot_ts=ts,
                    end_ts=self._end_ts(m),
                    slug=str(getattr(m, "slug", None) or ""),
                    category=self._category(m),
                )
            )
        return results

    @staticmethod
    def _short_expiry_kwargs() -> dict:
        """构造 list_markets 的“近到期优先”过滤参数。

        窗口 = CONFIG.prefer_short_expiry_days（天）。>0 时：
          - closed=False：只看未关闭市场；
          - end_date_min=现在 / end_date_max=现在+窗口：只看近期到期；
          - order='endDate', ascending=True：按到期升序，快结算的排前面。
        窗口=0 返回空 dict（不加过滤，扫描全部）。
        """
        days = getattr(CONFIG, "prefer_short_expiry_days", 0)
        if not days or days <= 0:
            return {}
        now = datetime.now(timezone.utc)
        return {
            "closed": False,
            "end_date_min": now,
            "end_date_max": now + timedelta(days=days),
            "order": "endDate",
            "ascending": True,
        }

    @staticmethod
    def _category(m) -> str:
        """事件类型：优先市场自带 category，空则取第一个 tag 的 label，再空返回''。"""
        cat = getattr(m, "category", None)
        if cat:
            return str(cat)
        tags = getattr(m, "tags", None) or ()
        for t in tags:
            label = getattr(t, "label", None)
            if label:
                return str(label)
        return ""

    @staticmethod
    def _end_ts(m) -> float:
        """从 SDK market 抽取到期时间(epoch秒)，取不到返回 0。"""
        state = getattr(m, "state", None)
        end = getattr(state, "end_date", None) if state else None
        if end is None:
            return 0.0
        try:
            return end.timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _take(iterable, n: int) -> list:
        out = []
        for i, item in enumerate(iterable):
            if i >= n:
                break
            out.append(item)
        return out

    def _note_skip(self, reason: str) -> None:
        self.last_skipped += 1
        if len(self.last_skip_samples) < 5:
            self.last_skip_samples.append(reason)

    def _build_outcomes(self, client, m) -> list[OutcomeBook]:
        """把一个 SDK Market 的 yes/no 两腿 + 盘口 转成标准 OutcomeBook 列表。

        用批量 get_order_books（keyword-only：token_ids=[...]）一次取两腿，
        减少 API 调用。best_ask 取最低卖价，best_bid 取最高买价（显式排序，
        不假设盘口已排序）。
        """
        sides = [s for s in (m.outcomes.yes, m.outcomes.no)
                 if getattr(s, "token_id", None) is not None]
        if not sides:
            return []

        token_ids = [str(s.token_id) for s in sides]
        books_raw = self._call_with_retry(client.get_order_books, token_ids=token_ids)
        # 按 token_id 建索引，稳妥对齐（不依赖返回顺序）。
        by_token = {str(ob.token_id): ob for ob in books_raw}

        books: list[OutcomeBook] = []
        for side in sides:
            tid = str(side.token_id)
            ob = by_token.get(tid)
            if ob is None:
                continue
            best_ask = min(ob.asks, key=lambda lv: lv.price) if ob.asks else None
            best_bid = max(ob.bids, key=lambda lv: lv.price) if ob.bids else None
            books.append(
                OutcomeBook(
                    outcome=side.label,
                    token_id=tid,
                    best_ask=_to_float(best_ask.price) if best_ask else None,
                    best_bid=_to_float(best_bid.price) if best_bid else None,
                    ask_size=_to_float(best_ask.size) or 0.0 if best_ask else 0.0,
                    bid_size=_to_float(best_bid.size) or 0.0 if best_bid else 0.0,
                )
            )
        return books


class StaticSource:
    """假数据源：返回预置的 Market 列表。用于测试、回测、离线演示。"""

    def __init__(self, markets: list[Market]):
        self._markets = markets

    def fetch_markets(self, limit: int) -> list[Market]:
        return self._markets[:limit]
