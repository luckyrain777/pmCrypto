"""本地网页控制台。

/api/state    GET  当前运行状态 + 完整可变配置 + 市场/机会/信号（供页面刷新）
/api/config   POST 修改运行时参数（白名单+类型校验，见 config.Config.apply）
/api/control  POST 运维动作：halt/resume（急停/解除）、pause/unpause（暂停/恢复扫描）
/api/edge-report POST 跑一次 edge 验证报告并返回文本结论

安全说明：默认仅监听 127.0.0.1（本机）。executor_mode 可经 /api/config 直接切到
auto——但 auto 执行器在真实下单逻辑实现前会拒绝执行，故当前切换不会真的花钱。
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import CONFIG
from core.state import STATE
from data.store import Store

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _build_dashboard(store: Store, guard, portfolio_reader=None) -> dict:
    """组装资金总览 / 风险余量 / edge 进度 / 系统健康，供顶部横幅展示。"""
    import time
    st = STATE.snapshot()
    cfg = CONFIG

    # 资金：优先用【公开地址】查到的真实链上组合价值；查不到回落模拟基数。
    real = portfolio_reader.snapshot() if portfolio_reader else None
    if real is not None:
        # 余额优先显示现金 USDC；查不到现金则退回总价值。
        balance = real.cash_usdc if real.cash_usdc is not None else real.total_value_usdc
        balance_is_real = True
        real_positions = real.positions
        real_pnl = real.total_cash_pnl_usdc
        real_total = real.total_value_usdc
        real_posval = real.positions_value_usdc
    else:
        balance = guard.account_balance_usdc if guard else cfg.account_balance_usdc
        balance_is_real = False
        real_positions = []
        real_pnl = None
        real_total = None
        real_posval = None

    exposure = guard.current_exposure_usdc if guard else 0.0
    is_live = cfg.executor_mode == "auto" and not cfg.dry_run

    # 今日信号统计（按 created_ts 落在今天算）
    now = time.time()
    day_start = now - (now % 86400)
    sigs = store.recent_signals(limit=500)
    today_sigs = [s for s in sigs if (s.get("created_ts") or 0) >= day_start]

    # edge 进度：市场总数 / 已结算数
    try:
        markets_total = len(store.distinct_market_ids())
        resolved_total = len(store.all_resolutions())
    except Exception:
        markets_total = resolved_total = 0

    # 风险安全余量
    daily_buffer = balance - cfg.halt_balance_daily
    total_buffer = balance - cfg.halt_balance_total
    losses_left = max(cfg.max_consecutive_losses - st.get("consecutive_losses", 0), 0)

    return {
        "is_live": is_live,
        "mode_label": "真钱交易中" if is_live else "安全（只提示）",
        "balance_usdc": round(balance, 2),
        "balance_is_real": balance_is_real,
        "exposure_usdc": round(exposure, 2),
        "exposure_pct": round(exposure / balance * 100, 1) if balance > 0 else 0.0,
        "daily_pnl_usdc": st.get("daily_pnl_usdc", 0.0),
        "total_pnl_usdc": real_pnl,
        "total_value_usdc": real_total,
        "positions_value_usdc": real_posval,
        "positions": real_positions,
        "positions_count": len(real_positions),
        "signals_today": len(today_sigs),        # 建议信号数（非真实成交）
        "real_trades": st.get("real_trades", 0),  # 真实成交笔数
        "poll_interval_sec": cfg.poll_interval_sec,
        "last_cycle_ts": st.get("last_cycle_ts", 0.0),
        "markets_per_cycle": cfg.max_markets_per_cycle,
        "scan_hint": f"每轮扫描最多 {cfg.max_markets_per_cycle} 个市场"
                     "（Polymarket 全站有数千个，可在高级设置调大）",
        "risk": {
            "daily_buffer_usdc": round(daily_buffer, 2),
            "total_buffer_usdc": round(total_buffer, 2),
            "consecutive_losses_left": losses_left,
            "halt_balance_daily": cfg.halt_balance_daily,
            "halt_balance_total": cfg.halt_balance_total,
        },
        "edge_progress": {
            "markets_total": markets_total,
            "resolved_total": resolved_total,
            "hint": ("已有结算数据，可点『运行 edge 报告』查看是否达标"
                     if resolved_total > 0
                     else "尚无市场结算，需等待市场收盘后再验证 edge"),
        },
    }


def create_app(store: Store, guard=None) -> FastAPI:
    app = FastAPI(title="pmCrypto 控制台", docs_url=None, redoc_url=None)

    # 只读组合查询器（用公开地址查真实余额/持仓），带缓存。
    from data.portfolio import PortfolioReader
    portfolio_reader = PortfolioReader()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
            return f.read()

    @app.get("/api/state")
    def api_state() -> JSONResponse:
        return JSONResponse({
            "run_state": STATE.snapshot(),
            "config": CONFIG.mutable_dict(),
            "dashboard": _build_dashboard(store, guard, portfolio_reader),
            "markets": store.latest_market_snapshots(limit=100),
            "opportunities": store.recent_opportunities(limit=50),
            "signals": store.recent_signals(limit=50),
        })

    @app.post("/api/config")
    async def api_config(request: Request) -> JSONResponse:
        body = await request.json()
        applied = CONFIG.apply(body if isinstance(body, dict) else {})
        note = ""
        if applied.get("executor_mode") == "auto":
            note = ("已切到 auto 模式。注意：真实下单逻辑尚未实现，"
                    "auto 执行器目前会拒绝执行，不会真的下单。")
        return JSONResponse({"applied": applied,
                             "config": CONFIG.mutable_dict(), "note": note})

    @app.post("/api/control")
    async def api_control(request: Request) -> JSONResponse:
        body = await request.json()
        action = (body or {}).get("action")
        msg = ""
        if action == "halt":
            STATE.trip("网页控制台手动急停")
            msg = "已触发全局急停。"
        elif action == "resume":
            STATE.reset()
            msg = "已解除急停，系统恢复运行。"
        elif action == "pause":
            CONFIG.apply({"paused": True})
            msg = "已暂停扫描（主循环空转，不产生信号）。"
        elif action == "unpause":
            CONFIG.apply({"paused": False})
            msg = "已恢复扫描。"
        else:
            return JSONResponse({"ok": False, "error": f"未知动作 {action}"},
                                status_code=400)
        return JSONResponse({"ok": True, "message": msg,
                             "run_state": STATE.snapshot(),
                             "config": CONFIG.mutable_dict()})

    @app.post("/api/edge-report")
    def api_edge_report() -> JSONResponse:
        from backtest.edge_report import run_edge_report
        rep = run_edge_report(store)
        return JSONResponse({
            "summary": rep.summary(),
            "bets": rep.bets,
            "win_rate": rep.win_rate,
            "mean_return": rep.mean_return,
            "significant": rep.edge_significantly_positive,
        })

    @app.post("/api/go-live")
    def api_go_live() -> JSONResponse:
        """真钱总闸：自动检查所有门槛，全过才原子切到真实自动交易。

        任一门槛不满足直接拒绝并列出原因——不再依赖用户自觉。
        """
        blockers = []

        # 门槛1：edge 必须经验证显著为正
        from backtest.edge_report import run_edge_report
        rep = run_edge_report(store)
        if not rep.edge_significantly_positive:
            blockers.append(
                f"edge 未通过验证（下注 {rep.bets} 笔，需≥30 且置信区间下界>0）。"
                "请让系统多跑、积累结算结果后再试。")

        # 门槛2：.env 凭证齐全
        from data.credentials import load_credentials
        creds = load_credentials()
        if creds is None:
            blockers.append(
                ".env 凭证不全（需 POLYGON_PRIVATE_KEY + CLOB_API_KEY/SECRET/PASSPHRASE）。")

        if blockers:
            return JSONResponse({"ok": False, "blockers": blockers,
                                 "config": CONFIG.mutable_dict()}, status_code=200)

        # 全过：原子切换到真钱自动交易
        CONFIG.apply({"executor_mode": "auto", "dry_run": False,
                      "edge_verified": True})
        STATE.reset()  # 确保不是急停态
        return JSONResponse({"ok": True,
                             "message": "已启用真钱自动交易。系统将在下一轮自动下单。",
                             "config": CONFIG.mutable_dict()})

    @app.post("/api/go-safe")
    def api_go_safe() -> JSONResponse:
        """一键退回安全态：manual + 干跑保护，立即停止一切真实下单。"""
        CONFIG.apply({"executor_mode": "manual", "dry_run": True})
        return JSONResponse({"ok": True, "message": "已回到安全态（只提示、不下单）。",
                             "config": CONFIG.mutable_dict()})

    return app
