"""主循环：把所有模块串起来，常驻轮询。

每轮：检查急停 → 抓数据 → 存快照 → 检测偏差 → 风控+定价 → 执行器处理信号。
同时在后台线程启动只读网页面板。

用法：
    python main.py            # 用真实 Polymarket 数据源常驻运行
    python main.py --once     # 只跑一轮即退出（冒烟测试）
    python main.py --backtest # 对已有历史快照跑回测后退出
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from config import CONFIG
from core.state import STATE
from core.activity import ACTIVITY
from data.client import MarketDataSource, PolymarketSource
from data.store import Store
from execution.base import Executor
from execution.manual import ManualExecutor
from notify.console import Notifier
from risk.guard import RiskGuard
from strategy import detector, edge_detector


def build_executors(store: Store, notifier: Notifier, guard) -> dict:
    """建好两种执行器，供 run_cycle 按运行时 CONFIG.executor_mode 选用。

    这样控制台运行时切 manual↔auto 立即生效，且 auto 的客户端缓存得以保留。
    """
    from execution.auto import AutoExecutor
    from execution.arb import ArbExecutor
    from data.portfolio import PortfolioReader
    # 只读组合查询器：auto 门6用它查链上在场持仓笔数（带缓存，失败降级）。
    portfolio_reader = PortfolioReader()
    return {
        "manual": ManualExecutor(store, notifier),
        "auto": AutoExecutor(store, notifier, guard,
                             portfolio_reader=portfolio_reader),
        "arb": ArbExecutor(store, notifier, guard),
    }


def _settle_cycle(store: Store, notifier: Notifier, guard=None) -> None:
    """每轮：①刷新已到期市场的结算结果(edge 验证的原料，【不依赖是否持仓】)
    ②有持仓台账时回填盈亏。任何异常都隔离，绝不拖垮主循环。

    修复：旧版"无 open 台账就整体跳过"会导致 resolutions 永远空、edge 验证
    永远 0 结算的死锁。结算原料的积累必须独立于是否下单。
    """
    try:
        # ① 结算原料：只查一小批"已到期未结算"市场，限量避免狂发 API。
        try:
            expired = store.expired_unresolved_market_ids(time.time(), limit=40)
            if expired:
                from data.resolutions import refresh_resolutions
                from polymarket import PublicClient
                n = refresh_resolutions(PublicClient(), store, market_ids=expired)
                if n:
                    notifier.info(f"新增 {n} 个市场结算结果（edge 验证原料 +{n}）。")
                    ACTIVITY.record("settle", f"拉取到 {n} 个市场结算结果", time.time())
        except Exception as exc:  # noqa: BLE001
            notifier.warning(f"刷新结算结果失败（下轮重试）：{exc}")

        # ② 持仓回填：有 open 台账才做（把已到期持仓转成已实现盈亏）。
        if store.open_trades():
            from execution.settlement import settle_open_trades
            settle_open_trades(store, notifier=notifier, guard=guard)
    except Exception as exc:  # noqa: BLE001
        notifier.warning(f"结算回填异常（不影响主循环）：{exc}")


def run_cycle(
    source: MarketDataSource,
    store: Store,
    guard: RiskGuard,
    executors,
    notifier: Notifier,
    crypto_source=None,
) -> None:
    """跑一轮：结算回填→抓→存→检测→风控→执行。"""
    # 先结算：把已到期持仓的已实现盈亏回填进 STATE（驱动连亏/当日止损熔断）。
    # 这一步不受 paused/halted 影响——它记录的是已发生的事实，不是新交易。
    _settle_cycle(store, notifier, guard)

    if CONFIG.paused:
        notifier.info("扫描已暂停（网页控制台可恢复）。")
        return
    if STATE.is_halted:
        notifier.warning(f"系统处于急停态，跳过本轮交易逻辑：{STATE.snapshot()['halt_reason']}")
        return

    # 按运行时模式选执行器（控制台切 manual↔auto 即时生效）。
    executor = (executors or {}).get(CONFIG.executor_mode) \
        or (executors or {}).get("manual")
    # 套利执行器：独立于方向性，受 enable_arb_auto 开关控制（内部自检）。
    arb_executor = (executors or {}).get("arb")

    try:
        markets = source.fetch_markets(limit=CONFIG.max_markets_per_cycle)
    except Exception as exc:  # noqa: BLE001
        notifier.warning(f"抓取市场失败：{exc}")
        return

    skipped = getattr(source, "last_skipped", 0)
    if skipped:
        samples = "; ".join(getattr(source, "last_skip_samples", [])[:3])
        notifier.warning(f"本轮跳过 {skipped} 个市场（样本：{samples}）")
    notifier.info(f"本轮抓取市场 {len(markets)} 个，开始扫描偏差…")

    if not markets:
        notifier.warning("本轮未取得任何有效市场——请检查 API 连通/字段映射。")
        return
    signal_count = 0
    edge_candidates = []  # 阶段B候选，收集后精选前N个
    for market in markets:
        store.save_market_snapshot(market)

        # 阶段A：套利偏差（当前快照即可判断）
        for opp in detector.detect(market):
            store.save_opportunity(opp)
            if CONFIG.enable_arb_auto and arb_executor is not None:
                # 套利自动：多腿逐腿下单+失败回滚（执行器内部自检开关/凭证/急停）。
                arb_executor.execute(opp)
                signal_count += 1
            else:
                # 仅提示：走风控评估 + 提示执行器（不自动下单）。
                signal = guard.assess(opp)
                if signal is not None:
                    executor.execute(signal)
                    signal_count += 1

        # 阶段B：方向性 edge（需历史序列）
        if CONFIG.enable_edge_strategy:
            history = store.market_history(market.market_id, limit=50)
            for opp in edge_detector.detect_edge(history):
                store.save_opportunity(opp)
                edge_candidates.append(opp)

            # 阶段B外部信号：加密市场概率误定价（现价+波动率）
            if crypto_source is not None:
                for opp in edge_detector.detect_crypto_edge(market, crypto_source):
                    store.save_opportunity(opp)
                    edge_candidates.append(opp)

    # 精选机会：只提示 edge 最大的前 N 个，把火力集中在高质量机会上
    edge_candidates.sort(key=lambda o: o.raw_edge, reverse=True)
    for opp in edge_candidates[:CONFIG.edge_top_n_per_cycle]:
        signal = guard.assess_edge(opp)
        if signal is not None:
            notifier.warning(
                "【阶段B·未验证edge·仅提示】以下信号的 edge 尚未经 edge_report 验证，"
                "切勿据此上真钱："
            )
            executor.execute(signal)
            signal_count += 1

    now = time.time()
    if signal_count == 0:
        notifier.info("本轮无可提示信号（机会稀少属正常）。")
        ACTIVITY.record("scan", f"扫描 {len(markets)} 个市场，无可提示机会", now)
    else:
        notifier.info(f"本轮产生 {signal_count} 个建议信号。")
        ACTIVITY.record("scan",
                        f"扫描 {len(markets)} 个市场，产生 {signal_count} 个信号", now)

    STATE.mark_cycle(len(markets), now)


def start_web(store: Store, notifier: Notifier, guard=None) -> None:
    """后台线程启动网页面板。失败不影响主循环。"""
    try:
        import uvicorn
        from web.server import create_app

        app = create_app(store, guard)
        config = uvicorn.Config(
            app, host=CONFIG.web_host, port=CONFIG.web_port, log_level="warning"
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        notifier.info(f"网页面板已启动：http://{CONFIG.web_host}:{CONFIG.web_port}")
    except Exception as exc:  # noqa: BLE001
        notifier.warning(f"网页面板启动失败（不影响主循环）：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="pmCrypto Polymarket 量化系统")
    parser.add_argument("--once", action="store_true", help="只跑一轮即退出")
    parser.add_argument("--backtest", action="store_true", help="对历史快照跑套利回测后退出")
    parser.add_argument("--edge-report", action="store_true",
                        help="阶段B：用结算结果验证方向性信号的 edge 后退出")
    parser.add_argument("--refresh-resolutions", action="store_true",
                        help="拉取并记录已关闭市场的结算结果（edge验证原料）")
    args = parser.parse_args()

    # 恢复上次在面板改过的运行时参数（持仓上限/单笔金额上限/开关等），
    # 否则重启后会回到代码默认值。密钥不在此文件，安全。
    CONFIG.load_runtime()

    notifier = Notifier()
    store = Store(CONFIG.db_path)

    if args.backtest:
        from backtest.engine import run_backtest
        result = run_backtest(store)
        notifier.info("=== 套利回测结果 ===\n" + result.summary())
        return 0

    if args.refresh_resolutions:
        from data.resolutions import refresh_resolutions
        from polymarket import PublicClient
        n = refresh_resolutions(PublicClient(), store)
        notifier.info(f"新增结算结果 {n} 条。")
        return 0

    if args.edge_report:
        from backtest.edge_report import run_edge_report
        rep = run_edge_report(store)
        notifier.info("=== 阶段B edge 验证报告 ===\n" + rep.summary())
        return 0

    source: MarketDataSource = PolymarketSource()
    guard = RiskGuard(account_balance_usdc=CONFIG.account_balance_usdc)
    executors = build_executors(store, notifier, guard)

    crypto_source = None
    if CONFIG.enable_crypto_signal:
        from data.crypto_price import CryptoPriceSource
        crypto_source = CryptoPriceSource()
        notifier.info("加密现价误定价信号已启用（外部源：Coinbase/Binance）。")

    notifier.info(
        f"pmCrypto 启动 | 模式={CONFIG.executor_mode} | "
        f"间隔={CONFIG.poll_interval_sec}s | 账户基数={CONFIG.account_balance_usdc} USDC"
    )
    if CONFIG.executor_mode == "manual":
        notifier.info("当前为阶段 C：只提示、不自动下单。下单需你人工确认。")

    if CONFIG.dry_run:
        notifier.info("dry-run 已开启：auto 模式也只干跑、不发送真实订单。")

    if args.once:
        # 一次性运行不启动网页面板（避免残留端口占用）。
        run_cycle(source, store, guard, executors, notifier, crypto_source)
        return 0

    start_web(store, notifier, guard)

    try:
        while True:
            run_cycle(source, store, guard, executors, notifier, crypto_source)
            time.sleep(CONFIG.poll_interval_sec)
    except KeyboardInterrupt:
        notifier.info("收到中断，退出。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
