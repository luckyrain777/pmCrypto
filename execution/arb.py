"""套利执行器：多腿逐腿 FOK 下单 + 失败回滚（防半腿裸赌）。

套利（Yes/No 互补、多结果概率和）是多腿机会：必须【所有腿都成交】
才锁定无风险利润。若只成交一部分腿，就变成了裸露的单边头寸——套利退化为赌博。

策略：逐腿 FOK 买入（要么全成要么不成）。一旦某腿未成交，立即对【已成交的腿】
下 SELL FOK 回滚，把仓位平回去，不留半腿。回滚也可能有小亏（点差），但远好于
持有裸头寸。

与方向性 auto 的区别：套利无需 edge 验证（无风险、纯算术），受独立开关
CONFIG.enable_arb_auto 控制；但仍需 非 dry_run + 凭证齐全 + 非急停。
"""
from __future__ import annotations

import math
import time
from typing import Optional

from config import CONFIG
from core.state import STATE
from core.activity import ACTIVITY
from strategy.models import Opportunity


class ArbExecutor:
    def __init__(self, store, notifier, guard=None):
        self._store = store
        self._notifier = notifier
        self._guard = guard
        self._client = None
        self._creds = None

    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        from data.credentials import load_credentials
        self._creds = load_credentials()
        if self._creds is None:
            self._notifier.warning(
                "拒绝套利真实下单：未在 .env 找到完整凭证。")
            return False
        try:
            from polymarket import SecureClient, ApiKeyCreds
            self._client = SecureClient.create(
                private_key=self._creds.private_key,
                credentials=ApiKeyCreds(
                    key=self._creds.api_key, secret=self._creds.api_secret,
                    passphrase=self._creds.api_passphrase))
            return True
        except Exception as exc:  # noqa: BLE001
            self._notifier.warning(f"SecureClient 建立失败，拒绝套利下单：{exc}")
            return False

    @staticmethod
    def _filled(resp) -> bool:
        """FOK 成交判据：trade_ids 非空。"""
        return bool(getattr(resp, "ok", False)) and bool(getattr(resp, "trade_ids", None))

    def _buy_leg(self, token_id: str, price: float, shares: float):
        """对一条腿下 FOK 市价买单【按精确份数】，返回 resp（或 None 异常）。

        用 shares 而非 amount：FOK 保证要么精确成交 shares 份、要么取消。
        这样各腿份数确定且相等，才能真正锁定套利（等份数是锁利前提）。
        """
        # 对齐 tick：向上取整到 2 位小数（对 0.01/0.001 tick 都合规，上限只放宽）
        max_price = min(math.ceil(price * (1.0 + CONFIG.market_max_slippage_pct) * 100) / 100, 0.99)
        try:
            return self._client.place_market_order(
                token_id=token_id, side="BUY", shares=shares,
                order_type="FOK", max_price=max_price)
        except Exception as exc:  # noqa: BLE001
            self._notifier.warning(f"套利腿下单异常：{exc}")
            return None

    def _rollback(self, filled_legs) -> bool:
        """对已成交的腿逐个下 SELL FOK 平回。返回 True=全部回滚成功。

        回滚也检查成交（trade_ids）——若某腿卖不掉（FOK 被 kill 或异常），
        说明留下了裸头寸，返回 False，交由上层急停 + 记账留痕（绝不隐形）。
        """
        all_ok = True
        for token_id, price, shares in filled_legs:
            # 对齐 tick：向下取整到 2 位小数（卖出下限，向下只放宽，合规）
            min_price = math.floor(price * (1.0 - CONFIG.market_max_slippage_pct) * 100) / 100
            try:
                resp = self._client.place_market_order(
                    token_id=token_id, side="SELL", shares=shares,
                    order_type="FOK", min_price=min_price)
            except Exception as exc:  # noqa: BLE001
                resp = None
                self._notifier.warning(f"回滚腿 {token_id[:10]}… 下单异常：{exc}")
            if resp is not None and self._filled(resp):
                self._notifier.warning(
                    f"已回滚腿 {token_id[:10]}…（卖出 {shares} 份）。")
            else:
                all_ok = False
                self._notifier.warning(
                    f"⚠️ 回滚腿 {token_id[:10]}… 未成交，留下裸头寸 {shares} 份！")
        return all_ok

    def execute(self, opp: Opportunity) -> None:
        # 门1：急停
        if STATE.is_halted:
            self._notifier.warning("急停中，拒绝套利下单。")
            return
        # 门2：套利自动开关
        if not CONFIG.enable_arb_auto:
            return
        # 门3：至少两腿才算套利
        legs = list(opp.legs)
        if len(legs) < 2:
            return
        prices = [p for _t, p in legs]
        if any(p <= 0 for p in prices):
            return

        # 门4：成本校验——套利利润必须能覆盖【双向滑点】才值得下。
        # 每腿买入吃一次滑点（估 slippage_safety_factor 已含保守放大）。
        # 净收益 = raw_edge − 各腿滑点之和；≤0 则做了白做（甚至亏），放弃。
        est_slip = CONFIG.market_max_slippage_pct * len(legs)
        net_edge = opp.raw_edge - est_slip
        if net_edge <= 0:
            self._notifier.info(
                f"套利机会 {opp.kind.value} 净收益 {net_edge:.4f}≤0"
                f"（毛 {opp.raw_edge:.4f} − 估滑点 {est_slip:.4f}），不划算，跳过。")
            return

        # 等份数 N：套利锁利要求各腿【份数相等】(而非等金额)。
        # N 受最薄腿深度约束——用最高腿价把名义额换成份数，确保各腿都买得起 N 份。
        cap_usdc = opp.min_leg_notional_usdc
        # 单笔金额硬上限（实测保险丝）：每腿金额 = N*price 不超上限 → 收紧 N。
        if CONFIG.max_single_order_usdc > 0:
            cap_usdc = min(cap_usdc, CONFIG.max_single_order_usdc)
        N = round(cap_usdc / max(prices), 2)
        if N <= 0:
            return

        # 门5：dry-run（默认保护）
        if CONFIG.dry_run:
            self._notifier.info(
                f"【DRY-RUN 干跑·不发送】套利 {opp.kind.value} {len(legs)} 腿 "
                f"各 {N} 份 | 净收益 {net_edge:.4f}")
            return

        # 门6：凭证 + 客户端
        if not self._ensure_client():
            return

        # 逐腿 FOK 【按等份数 N】买入；任一腿失败即回滚已成交腿。
        filled = []  # [(token_id, price, N)]
        for token_id, price in legs:
            resp = self._buy_leg(token_id, price, N)
            if resp is None or not self._filled(resp):
                self._notifier.warning(
                    f"套利腿 {token_id[:10]}… 未成交（FOK 取消），触发回滚。")
                ok = self._rollback(filled)
                if not ok:
                    self._handle_naked(opp, filled)
                return
            filled.append((token_id, price, N))

        # 全部腿成交：锁定套利，记台账（每腿一条，等份数 N）。
        # 同一笔套利的各腿打同一 group_id，结算时按组聚合盈亏（输腿不误触连亏）。
        now = time.time()
        gid = f"arb-{opp.market_id}-{now}"
        for token_id, price, shares in filled:
            self._store.record_trade(
                market_id=opp.market_id, token_id=token_id,
                cost_price=price, shares=shares, cost_usdc=round(shares * price, 2),
                created_ts=now, group_id=gid)
        STATE.record_real_trade()
        if self._guard is not None:
            self._guard.reserve(sum(sh * p for _t, p, sh in filled))
        self._notifier.info(
            f"【套利已成交】{opp.kind.value} {len(filled)} 腿各 {N} 份全部成交，"
            f"净收益 {net_edge:.4f}。")
        spent = round(sum(sh * p for _t, p, sh in filled), 2)
        ACTIVITY.record("order",
                        f"套利成交 {len(filled)} 腿，花费 ${spent}，净收益 {net_edge:.3f}",
                        now)

    def _handle_naked(self, opp: Opportunity, filled) -> None:
        """回滚失败→留下裸头寸：绝不让它隐形。记台账 + 占用额度 + 全局急停。"""
        now = time.time()
        gid = f"arb-naked-{opp.market_id}-{now}"
        for token_id, price, shares in filled:
            self._store.record_trade(
                market_id=opp.market_id, token_id=token_id,
                cost_price=price, shares=shares, cost_usdc=round(shares * price, 2),
                created_ts=now, group_id=gid)
        # 裸头寸必须占用额度：否则 exposure 不增，风控会误以为有余额继续开新仓，
        # 导致多个单边裸头寸堆积、资金失控。占用直到人工平仓。
        if self._guard is not None:
            self._guard.reserve(sum(sh * p for _t, p, sh in filled))
        STATE.trip("套利回滚失败，留有裸头寸，需人工平仓")
        self._notifier.warning(
            "⚠️ 套利回滚失败，已记裸头寸台账、占用额度并触发全局急停。请立即人工平仓！")
