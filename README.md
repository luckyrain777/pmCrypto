# pmCrypto — Polymarket 量化交易系统

一套能持续运行的 Polymarket 量化分析 / 自动交易系统。核心原则：**把赌博变成有纪律、可验证、有刹车的自动交易**——不承诺"稳赚"，只保证"若真有 edge，用数学最优方式放大它，且绝不爆仓"。

> ⚠️ **先读这一句**：本系统当前处于 **manual 模式（只分析、只提示、不自动下单）**，零资金风险。任何真实下单都必须先通过 **edge 验证 + 小额灰度** 两道准入门槛（见下文），在此之前 `execution/auto.py` 会**主动拒绝执行**，作为防止误上真钱的物理保险。

---

## 这套系统能做什么

| 策略 | 逻辑 | 状态 |
|---|---|---|
| **阶段 A · 套利** | Yes/No 互补偏差、互斥概率和偏差（不预测未来，吃无风险差价） | 已实现，稀有但客观 |
| **阶段 B · 方向性 edge** | 统计信号合成"真实概率 p" vs 市场报价 q，误定价够大则按自适应 Kelly 复利下注 | 已实现，**edge 待验证** |

两个策略并存、可各自开关。执行、风控、存储、网页面板全部共用。

---

## 快速开始

### 1. 安装依赖

```powershell
cd D:\winsey_code\pmCrypto
pip install -r requirements.txt
```

关键依赖：`polymarket-client`（官方统一 SDK，2026 版；旧的 py-clob-client 已归档失效）、`fastapi`/`uvicorn`（网页面板）、`pytest`（测试）。

### 2. 跑测试（确认环境 OK）

```powershell
python -m pytest tests/ -q
```

应看到全部通过（当前 52 项）。

### 3. 常驻运行（挂机积累数据 + 只提示）

```powershell
python main.py
```

- 每 45 秒轮询一次，抓最多 100 个市场，扫描套利 + 方向性机会。
- 网页面板：浏览器打开 **http://127.0.0.1:8000**（只读监控）。
- **manual 模式**：发现机会只在终端/日志/面板提示，**不自动下单**。
- 数据持续存入 `data/pmcrypto.db`（这是后续 edge 验证的原料）。
- Ctrl+C 停止。

---

## 所有命令

| 命令 | 作用 |
|---|---|
| `python main.py` | 常驻运行：套利 + 方向性双策略扫描（只提示） |
| `python main.py --once` | 只跑一轮即退出（冒烟测试） |
| `python main.py --backtest` | 对已积累快照跑**套利**回测 |
| `python main.py --refresh-resolutions` | 拉取已收盘市场的**结算结果**（edge 验证原料） |
| `python main.py --edge-report` | **验证方向性 edge 是否显著为正**（决定能否上真钱） |

---

## 架构速览（为什么能安全扩展）

```
main.py                  常驻主循环：抓→存→检测→风控→执行
├── data/
│   ├── client.py        唯一接触 Polymarket API 的地方（换 SDK 只动这里）
│   ├── store.py         SQLite：快照/机会/信号/结算
│   └── resolutions.py   记录市场结算结果
├── strategy/
│   ├── detector.py      阶段A：套利偏差检测
│   ├── signals/         阶段B：统计信号（动量/盘口失衡/深度背离）+ 合成器
│   ├── edge_detector.py 阶段B：方向性误定价检测
│   ├── pricing.py       净收益（扣手续费/保守滑点/流动性）
│   └── kelly.py         自适应分数 Kelly 仓位（复利核心）
├── risk/guard.py        风控闸门（套利模式 assess / Kelly 模式 assess_edge）
├── core/state.py        全局状态 + 急停（API失败/亏损/连亏熔断）
├── execution/           可插拔执行器：manual(提示) / auto(真下单，未验证前拒绝执行)
├── backtest/
│   ├── engine.py        套利回测
│   └── edge_report.py   ★ edge 科学验证（胜率/盈亏/95%置信区间）
└── web/                 FastAPI 只读面板
```

**三条解耦让扩展不动主干**：策略只认标准 `Market` 对象（实盘/回测通用）；执行层认 `Executor` 接口（换一行配置切 manual↔auto）；所有信号/策略"加一个文件即可"。

---

## 配置（`config.py`）

关键参数（全部可调，不含任何密钥）：

- `executor_mode`: `manual`（只提示）/ `auto`（真下单，需先过门槛）
- `poll_interval_sec`: 轮询间隔（默认 45s）
- `account_balance_usdc`: 账户基数（默认 100，仅用于风控占比换算）
- `edge_min_threshold`: 方向性机会门槛（edge 低于此不出手）
- `kelly_fraction_min/max`: 自适应 Kelly 分数区间（¼ ~ ½）
- `kelly_max_single_pct`: 单笔硬封顶（20% 余额）
- `halt_balance_daily/total`: 保命熔断线（60 当日停 / 30 彻底停）
- `max_consecutive_losses`: 连亏熔断（5 笔）

---

## 安全红线

1. **manual 阶段零密钥**：只读市场数据，不需要任何私钥/凭证。
2. **切 auto 才需密钥**，且只写入本地 `.env`（已被 `.gitignore` 挡死，绝不进 git/日志）：
   - 专用 Polygon 钱包私钥 —— **绝不能是主资产钱包**，单独建只放小额。
   - Polymarket CLOB API 凭证。
3. **两道准入门槛，缺一不可**（详见 `OPERATIONS.md`）：
   - ① edge 验证：`--edge-report` 判定"显著为正"。
   - ② 小额灰度：极小仓位真跑，校准滑点、对比回测。

---

## 诚实的预期管理

- 阶段 A 套利**极少出单**是正常的（真实市场里干净套利稀少）。
- 阶段 B 方向性策略**大概率结局是小幅波动**；翻倍是小概率好运，亏到熔断是真实尾部。
- **系统的价值不是保证赚钱，而是：在你确实有 edge 时把增长最大化、把爆仓概率压到最低；在你没有 edge 时诚实地拦住你别赌。**
- 详细设计见 `docs/superpowers/specs/2026-06-30-polymarket-quant-design.md`。
