"""阶段 A 执行器：真实自动下单（完整实现，默认 dry-run 零风险）。

多重安全门（任一不满足即拒绝真实下单）：
  1) 全局急停：core.state HALTED 时拒绝。
  2) dry_run：CONFIG.dry_run=True 时走完全部逻辑但只打印、不发送订单。
     —— 默认 True，是“代码就绪但不花钱”的双保险。
  3) edge 验证门：非 dry_run 且 CONFIG.edge_verified=False 时拒绝
     （科学门槛的代码兜底：未验证 edge 不许真下单）。
  4) 凭证门：非 dry_run 需从 .env 加载专用钱包私钥+API凭证，缺失则拒绝。
  5) 机会类型：第一版只自动执行方向性单腿(EDGE_DIRECTIONAL)；套利多腿
     需原子成交，暂不自动执行（仍在 manual 提示）。

成交后：更新 core.state（盈亏/连亏熔断由结算后回填）、guard 仓位占用。
复利：下单前用真实链上 USDC 余额刷新 guard.account_balance_usdc。
"""
from __future__ import annotations

import time
from typing import Optional

from config import CONFIG
from core.state import STATE
from data.store import Store
from notify.console import Notifier
from strategy.models import Signal, OpportunityKind


class AutoExecutor:
    def __init__(self, store: Store, notifier: Notifier, guard=None):
        self._store = store
        self._notifier = notifier
        self._guard = guard
        self._client = None          # 懒建的 SecureClient
        self._creds = None           # 懒加载的凭证

    # ── 真实客户端（仅非 dry_run 时建立）──────────────────
    def _ensure_client(self) -> bool:
        """确保 SecureClient 就绪。成功返回 True。"""
        if self._client is not None:
            return True
        from data.credentials import load_credentials
        self._creds = load_credentials()
        if self._creds is None:
            self._notifier.warning(
                "拒绝真实下单：未在 .env 找到完整凭证"
                "（POLYGON_PRIVATE_KEY / CLOB_API_KEY / SECRET / PASSPHRASE）。")
            return False
        try:
            from polymarket import SecureClient, ApiKeyCreds
            self._client = SecureClient.create(
                private_key=self._creds.private_key,
                credentials=ApiKeyCreds(
                    key=self._creds.api_key,
                    secret=self._creds.api_secret,
                    passphrase=self._creds.api_passphrase,
                ),
            )
            self._notifier.info("SecureClient 已就绪（" + self._creds.redacted() + "）。")
            return True
        except Exception as exc:  # noqa: BLE001
            self._notifier.warning(f"SecureClient 建立失败，拒绝下单：{exc}")
            return False

    def _refresh_balance(self) -> Optional[float]:
        """查真实链上 USDC(抵押品)余额，刷新 guard 以支持复利。"""
        try:
            ba = self._client.get_balance_allowance(asset_type="COLLATERAL")
            bal = float(getattr(ba, "balance", None) or getattr(ba, "amount", 0))
            if self._guard is not None and bal > 0:
                self._guard.account_balance_usdc = bal
            return bal
        except Exception as exc:  # noqa: BLE001
            self._notifier.warning(f"余额查询失败：{exc}")
            return None

    # ── 执行入口 ──────────────────────────────────────────
    def execute(self, signal: Signal) -> None:
        # 门1：急停
        if STATE.is_halted:
            self._notifier.warning("急停中，拒绝下单。")
            return

        # 门5：机会类型
        if signal.kind != OpportunityKind.EDGE_DIRECTIONAL:
            self._notifier.warning(
                f"auto 暂不自动执行 {signal.kind.value}（需原子多腿），已跳过。")
            return

        token_id, price = signal.legs[0]
        size_usdc = signal.suggested_size_usdc
        if price <= 0 or size_usdc <= 0:
            return
        size_shares = round(size_usdc / price, 2)

        # 门2：dry-run（默认）—— 走完逻辑，只打印不发送
        if CONFIG.dry_run:
            self._notifier.info(
                f"【DRY-RUN 干跑·不发送】将 BUY token {token_id[:10]}… "
                f"价 {price:.3f} × {size_shares} 份 (≈{size_usdc:.2f} USDC) | "
                f"净edge {signal.net_edge:.4f} | {signal.reason}")
            self._store.save_signal(signal, created_ts=time.time())
            return

        # 门3：edge 验证
        if not CONFIG.edge_verified:
            self._notifier.warning(
                "拒绝真实下单：edge 尚未验证（CONFIG.edge_verified=False）。"
                "请先跑 edge 报告并确认 [OK] 后再开真钱。")
            return

        # 门4：凭证 + 客户端
        if not self._ensure_client():
            return

        # 复利：用真实余额刷新 guard，并据此可能收缩本次名义额
        bal = self._refresh_balance()
        if bal is not None and bal < size_usdc:
            size_usdc = bal
            size_shares = round(size_usdc / price, 2)
            if size_shares <= 0:
                self._notifier.warning("余额不足以下这一单，跳过。")
                return

        # 真实下单（限价单，价格=信号买入价，防滑点）
        try:
            resp = self._client.place_limit_order(
                token_id=token_id, price=price, size=size_shares, side="BUY")
        except Exception as exc:  # noqa: BLE001
            self._notifier.warning(f"下单异常：{exc}")
            return

        if getattr(resp, "ok", False):
            self._notifier.info(
                f"【已下单】order_id={getattr(resp,'order_id','?')} | "
                f"BUY {token_id[:10]}… 价 {price:.3f} × {size_shares} 份")
            self._store.save_signal(signal, created_ts=time.time())
            STATE.record_real_trade()
            if self._guard is not None:
                self._guard.reserve(size_usdc)
        else:
            self._notifier.warning(
                f"下单被拒：code={getattr(resp,'code','?')} "
                f"msg={getattr(resp,'message','?')}")
