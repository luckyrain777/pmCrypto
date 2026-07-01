# pmCrypto 架构指南

> 面向想读懂、维护或扩展这套系统的人。回答三个问题：**它由哪些部分组成、一轮扫描里数据怎么流动、为什么这样切分能安全扩展。**
>
> 配套文档：`README.md`（做什么/怎么起）、`OPERATIONS.md`（怎么运维/上真钱清单）、`docs/superpowers/specs/2026-06-30-polymarket-quant-design.md`（原始设计）。本文补的是**结构与实现**这一层。

---

## 1. 一句话定位

pmCrypto 是一套常驻运行的 **Polymarket 量化分析 / 自动交易系统**。它把「赌博」改造成「有纪律、可验证、有刹车的自动交易」：

- **阶段 A · 套利** — 检测 Yes/No 互补、互斥概率和的客观偏差，吃无风险差价（不预测未来）。
- **阶段 B · 方向性 edge** — 用统计信号合成「真实概率 p」，与市场报价 q 比较，误定价够大则按自适应 Kelly 复利下注。

两个策略并存、各自开关，**共用同一套数据 / 执行 / 风控 / 存储 / 面板**。当前默认运行在 **manual 模式（只分析、只提示、不下单）**,任何真钱路径都被多重代码门槛物理拦住。

---

## 2. 分层总览

系统是一个**单进程 Python 应用**,由 `main.py` 的常驻主循环驱动,内部分为六层。每层只依赖它下方的层,通过标准数据模型 (`strategy/models.py`) 解耦。

```
                        ┌──────────────────────────────┐
                        │  main.py  常驻主循环 (每45s)   │
                        │  抓 → 存 → 检测 → 风控 → 执行  │
                        └──────────────┬───────────────┘
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
 ┌────────────┐ ┌────────────┐ ┌──────────────┐ ┌───────────┐ ┌────────────┐
 │  数据层     │ │  策略层     │ │   风控层      │ │  执行层    │ │   支撑层    │
 │  data/     │ │ strategy/  │ │  risk/       │ │execution/ │ │core/ notify│
 │            │ │            │ │  core/state  │ │           │ │ web/ config│
 │ client     │ │ detector   │ │  guard       │ │ base(接口) │ │ state(急停)│
 │ store      │ │ edge_detec │ │  (assess /   │ │ manual    │ │ console    │
 │ crypto_pri │ │ signals/   │ │   assess_edge│ │ auto      │ │ server(面板)│
 │ resolution │ │ pricing    │ │   )          │ │           │ │ CONFIG(单例)│
 │ portfolio  │ │ kelly      │ │              │ │           │ │            │
 │ credential │ │ models     │ │              │ │           │ │            │
 └────────────┘ └────────────┘ └──────────────┘ └───────────┘ └────────────┘
        │              │                                            │
        └──────────────┴──────────► 标准模型 Market/Opportunity/Signal ◄─┘
                                    (策略只认这些,与数据源/执行器无关)

  回测/验证旁路(离线,不碰实时API):
    backtest/engine.py       套利回测(重放历史快照)
    backtest/edge_report.py  ★方向性edge科学验证(结算结果+95%置信区间)
```

**三条解耦主线**(为什么扩展不动主干):
1. **策略只认标准 `Market` 对象** —— 实盘和回测喂的是同一种对象,同一套 detector 通吃。
2. **执行层认 `Executor` 接口** —— 换一行 `CONFIG.executor_mode` 即在 manual↔auto 间切换。
3. **信号「加一个文件即可」** —— 新增统计信号只需实现约定接口并在 combiner 注册权重。

---

## 3. 主循环:一轮扫描里发生什么

入口 `main.py::run_cycle()`,每 `CONFIG.poll_interval_sec`(默认 45 秒)执行一次:

```
run_cycle()
 1. 前置闸门     若 CONFIG.paused 或 STATE.is_halted → 跳过本轮交易逻辑
 2. 选执行器     按运行时 CONFIG.executor_mode 取 manual / auto(热切换即时生效)
 3. 抓数据       source.fetch_markets(limit=max_markets_per_cycle)
                 └─ 抓取失败 → 警告并跳过本轮(不崩)
 4. 逐市场:
    a. 存快照     store.save_market_snapshot(market)
    b. 阶段A套利  detector.detect(market) → Opportunity
                  → guard.assess(opp) → Signal → executor.execute(signal)
    c. 阶段B edge (若 enable_edge_strategy)
                  history = store.market_history(id, 50)
                  edge_detector.detect_edge(history)  → 收集到 edge_candidates
                  edge_detector.detect_crypto_edge(market, crypto_source) → 收集
 5. 精选         edge_candidates 按 raw_edge 降序,只取前 edge_top_n_per_cycle 个
                 → guard.assess_edge(opp) → Signal → executor.execute(signal)
 6. 收尾         STATE.mark_cycle(...) 记录本轮市场数与时间
```

要点:

- **阶段 A 与阶段 B 的关键差异**:套利只看当前快照即可判定;方向性 edge 需要**历史序列**(`market_history`),因此依赖数据先积累。
- **精选机制**:阶段 B 每轮不是把所有候选都提示出来,而是按 `raw_edge` 排序只提示最强的前 N 个(默认 3),把火力集中在高质量机会上。
- **网页面板**在独立 daemon 线程启动 (`start_web`),失败不影响主循环;`--once` 模式不启面板(避免残留端口占用)。
- **manual 下所有「建议下单」都只是提示**;阶段 B 信号提示前还会额外打一条「未验证 edge·仅提示·切勿据此上真钱」警告。

### 命令行入口 (`main.py::main`)

| 命令 | 行为 |
|---|---|
| `python main.py` | 常驻双策略扫描(只提示)+ 网页面板 |
| `python main.py --once` | 只跑一轮即退出(冒烟测试) |
| `python main.py --backtest` | 对已存快照跑**套利**回测 → `backtest/engine.py` |
| `python main.py --refresh-resolutions` | 拉取已收盘市场的结算结果 → `data/resolutions.py` |
| `python main.py --edge-report` | 验证方向性 edge 是否显著为正 → `backtest/edge_report.py` |

---

## 4. 数据层 (`data/`)

**唯一接触外部世界的层。** 把所有 API / SDK / 网络细节隔离在这里,对上只暴露标准模型。

| 文件 | 职责 |
|---|---|
| `client.py` | **唯一接触 Polymarket SDK 的地方**。`PolymarketSource` 实现 `MarketDataSource` 协议,列市场 + 批量拉盘口,转成标准 `Market`。内置指数退避重试、限频、失败计数,连续失败累计达阈值触发全局急停。换 SDK 只动这个文件。 |
| `store.py` | SQLite 持久化(零外部依赖,线程安全)。四张表:快照 / 机会 / 信号 / 结算。既支持实时写入,又支持按 `snapshot_ts` 升序**回放历史**供回测。 |
| `crypto_price.py` | 加密现价外部源(阶段 B 信号原料)。Coinbase / Binance 双源互备,30 秒缓存,失败降级返 `None` 不拖垮主循环;附年化波动率先验表。 |
| `resolutions.py` | 市场结算判定。**不依赖易缺失的链上/UMA 字段**,而是靠「已收盘市场价格收敛到 ≥0.90 即判为获胜方」这条市场铁律,幂等写入 `resolutions` 表,是 edge 验证的原料。 |
| `portfolio.py` | 只读钱包查询(不需私钥签名)。链上持仓市值 + 现金余额 + 盈亏,30 秒缓存。地址优先取 `.env` 的 `POLYMARKET_ADDRESS`,备选从私钥推导。 |
| `credentials.py` | 凭证加载。**只从 `.env` 读,绝不硬编码 / 写日志 / 打印完整密钥**;任一字段缺失返 `None`,由下游拒绝真实下单。`redacted()` 只露尾 4 位供安全摘要。 |

### 核心数据模型 (`strategy/models.py`)

放在 `strategy/` 下但被全项目共用,是解耦的基石:

- **`OutcomeBook`** — 单个结果的盘口:`outcome`、`token_id`、`best_ask/best_bid`、`ask_size/bid_size`,以及派生的 `ask_notional_usdc`(份额×单价)。
- **`Market`** — 一次市场快照:`market_id`、`question`、`outcomes`(互斥结果元组)、`snapshot_ts`(回测排序用)、`end_ts`、派生 `is_binary`。
- **`Opportunity`** — detector 发现的候选(**尚未扣成本**):`kind`(`YES_NO_COMPLEMENT` / `MUTEX_PROB_SUM` / `EDGE_DIRECTIONAL`)、`raw_edge`、`legs`、`min_leg_notional_usdc`,阶段 B 额外带 `estimated_p` 与 `confidence`。
- **`Signal`** — 经定价+风控后的最终建议:`net_edge`(扣成本净收益)、`suggested_size_usdc`(风控给的下注额)、`reason`。

### SQLite 表结构 (`store.py`)

| 表 | 存什么 | 关键列 |
|---|---|---|
| `market_snapshots` | 每轮每市场的盘口快照(回测原料) | `market_id, question, snapshot_ts, end_ts, outcomes_json` |
| `opportunities` | detector 发现的候选机会 | `market_id, kind, raw_edge, legs_json, snapshot_ts` |
| `signals` | 定价+风控后的最终建议 | `market_id, kind, raw_edge, net_edge, suggested_size_usdc, reason, created_ts` |
| `resolutions` | 市场结算结果(edge 验证用) | `market_id`(主键)`, winning_token_id, resolved_ts` |

**设计要点**:每次操作单独建连接 + `threading.Lock` 保护写入(网页线程写 / 主循环读);`INSERT OR REPLACE` 保证结算等幂等;所有实时数据带 `snapshot_ts`,保证回测可复现。

---

## 5. 策略层 (`strategy/`)

### 阶段 A:套利检测 (`detector.py`)

对当前快照检测两类**客观、当下可判定**的偏差(只看名义偏差是否存在,成本在 `pricing` 阶段才扣):

| 类型 | 条件 | raw_edge |
|---|---|---|
| **Yes/No 互补** | 二元市场,两腿都有卖价,`yes_ask + no_ask < 1` | `1 - (yes_ask + no_ask)` |
| **互斥概率和** | >2 结果的市场,全腿有卖价,`ask_sum < 1` | `1 - ask_sum` |

### 阶段 B:方向性 edge (`edge_detector.py` + `signals/`)

思路:**用统计信号把市场报价 q 修正成一个更接近真实的概率 p,edge = p − q,够大就下注被低估的那一腿。**

```
历史快照序列
   │ 逐结果抽取 (中间价序列, 总深度序列)
   ▼
三个统计信号各给 (delta 修正量, confidence 置信度)
   │
   ▼  combiner.combine(q, [signals])
p = clamp( q + Σ(权重ᵢ × confidenceᵢ × deltaᵢ), [0,1] )
   │
   ▼
edge = p − q ;  若 edge ≥ edge_min_threshold → 产出单腿 Opportunity(买低估结果)
```

统计信号 (`strategy/signals/`),每个实现同一约定接口,**新增信号 = 加一个文件 + 在 combiner 注册权重**:

| 信号 | 算什么 | 输出 |
|---|---|---|
| `momentum.py` | 中间价斜率 + 窗口内同向比例 | `delta = 斜率×scale`;`conf = 一致性×斜率幅度` |
| `book_imbalance.py` | `(bid名义 − ask名义)/total ∈ [−1,1]` | `delta = imbalance×0.1`;`conf = |imbalance|×深度因子` |
| `volume_divergence.py` | 近期 vs 早期深度放大倍数,与价格变化对比 | 深度放大≥1.5倍但价格几乎不动 → 低置信「蓄势」信号,scale=0.05 |
| `crypto_mispricing.py` | 对「BTC 到期前破 $X?」类市场用**对数正态模型** `P(S_T>K)=Φ(d2)` | 客观概率 `p_true`;置信度随到期临近升高(>30天=0.3,≤1天=0.95) |

`combiner.py` 默认权重 `momentum=1.0, book_imbalance=1.0, volume_divergence=0.6`(代理指标压权重);置信度为 0 的信号自动不参与。**权重设计上应由 edge 回测校准,而非拍脑袋。**

### 定价:扣成本 (`pricing.py`)

detector 给的是名义偏差,`pricing` 把它落到**可实操的净收益**:扣手续费 (`fee_rate`)、扣**保守放大**的滑点(估算滑点 × `slippage_safety_factor`,默认 ×1.5)、受最薄腿流动性约束,产出 `net_edge`。只向下修正,强制保守。

### 仓位:自适应 Kelly (`kelly.py`,纯函数)

复利核心。二元赌局的 Kelly 最优比例:

```
f* = (p − q) / (1 − q)   =  edge / (1 − q)
```

三层加码 / 削减:

1. **自适应 Kelly 分数**(信心驱动):`kelly_fraction = kelly_min + (kelly_max − kelly_min) × confidence`。信心越高越接近满 Kelly,信心不足则缩回 —— 侵略性精确跟随证据强度,低信号不会被忽略只是缩小仓位。
2. **三重削减**(安全前提,不可删):`f_final = min(f* × kelly_fraction, max_single_pct)`,再 `stake = min(balance×f_final, 剩余总暴露, 腿流动性)`。
3. **机会稀缺性门槛**:`edge < min_edge` 直接返回 0 不出手,把火力留给少数高 edge 机会(避免被手续费/方差磨死)。

边界保护:`q` 无效(≤0 或 ≥1)时拒绝,避免分母趋 0 时 Kelly 爆炸。**复利闭环**:auto 下单前用真实链上余额刷新 `balance_usdc`,赢了余额变大注变大、输了注变小,全自动。

---

## 6. 风控层 (`risk/guard.py` + `core/state.py`)

### RiskGuard:两条评估通道

| 方法 | 服务对象 | 做什么 |
|---|---|---|
| `assess(opp)` | 阶段 A 套利 | 查急停 → 定建议名义额(单笔上限 / 剩余总暴露 / 深度三者取 min)→ 用该额过 `pricing` 扣完整成本 → 净收益 > 阈值才产 `Signal` |
| `assess_edge(opp)` | 阶段 B 方向性 | 查急停 → 直接调 `kelly.compute_stake()` 按置信度自适应定仓 → Kelly 后仓位 > 0 才产 `Signal` |

辅助:`suggest_size_usdc(opp)` 只给数字不产信号;`reserve(size)`/`release(size)` 登记/释放已建仓名义额,维持总暴露准确。

### 全局急停 (`core/state.py::STATE`)

线程安全单例。四类触发,任一触发即进 HALTED 态,主循环、guard、executor 都会检查 `STATE.is_halted` 拒绝交易:

| 触发 | 条件 | 参数 |
|---|---|---|
| API 连续失败 | 累计失败计数 ≥ 阈值 | `api_failure_threshold`(默认 10) |
| 当日累计亏损 | 当日亏损 ≤ −上限 | `daily_max_loss_pct` / `halt_balance_daily` |
| 连续亏损 | 连亏笔数 ≥ 上限 | `max_consecutive_losses`(默认 5) |
| 手动 | `trip("原因")` | 任意 |

`record_api_success()` 会立即清零失败计数(避免偶发失败误触发);`reset()` 需人工排查后手动解除。**`STATE` 是进程内状态,重启即重置** —— 所以 `OPERATIONS.md` 强调「查清急停原因再重启」。

---

## 7. 执行层 (`execution/`)

`base.py` 定义极简接口:`Executor.execute(signal) -> None`。遵守它,主循环与策略零改动。

- **`ManualExecutor`(默认,阶段 C)** — 只做两件事:存库 + 终端/日志提示。**无任何 API 调用、无私钥接触、零真钱风险**。是「自动分析 + 人工确认」的落点。
- **`AutoExecutor`(真实下单,默认仍被拦)** — 完整实现了下单逻辑,但外面套了**五重门,任一不满足即拒绝真实下单**:

  | 门 | 检查 | 不满足 |
  |---|---|---|
  | 1 急停 | `STATE.is_halted` | 直接返回 |
  | 2 机会类型 | 仅 `EDGE_DIRECTIONAL`(套利多腿需原子成交,暂不自动) | 跳过 |
  | 3 dry-run | `CONFIG.dry_run`(**默认 True**) | 走完逻辑但只打印不发单 |
  | 4 edge 验证 | `CONFIG.edge_verified`(**默认 False**) | 拒绝(须先 edge 报告判 [OK]) |
  | 5 凭证 | `.env` 中私钥 + CLOB 凭证齐全 | 拒绝 |

  过门后:查真实链上余额刷新 guard(复利)→ 下**限价单**(价=信号买入价,防滑点)→ 成交则记 guard 仓位 + `STATE.record_real_trade()` + 存库。

> **切 auto 是一行配置改动,但真钱路径被门 3(dry_run 默认开)和门 4(edge 未验证)双重物理拦死。** 这是「防止误上真钱」的代码兜底,而非仅靠纪律。

---

## 8. 支撑层

- **`config.py::CONFIG`** — 全项目导入的单例。**只放策略/风控/运行参数,绝无密钥**。`_MUTABLE_FIELDS` 白名单定义了网页可运行时热改的字段(带类型 + clamp 校验 + 写锁),防止乱改内部字段。维持 `kelly_min ≤ kelly_max` 等不变式。
- **`notify/console.py`** — 统一通知出口(终端 + 日志),强制 UTF-8 防中文乱码。
- **`web/server.py`** — FastAPI **只读监控 + 受控写入**面板(前端是内联 JS 的单页 `web/static/index.html`,每 5 秒轮询,无框架)。仅绑 `127.0.0.1`,未开 CORS。端点:

  | 端点 | 作用 |
  |---|---|
  | `GET /` | 返回单页面板 |
  | `GET /api/state` | 聚合返回运行状态 / 仪表盘(资金·风险·edge 进度)/ 最近信号 / 机会 / 快照 / 配置 / 持仓(持仓查询失败降级返 None,不影响其余) |
  | `POST /api/config` | 热改配置,走 `CONFIG.apply()` 白名单,拒绝任意字段注入 |
  | `POST /api/control` | 运维动作:`halt`(手动急停)/ `resume`(解除急停)/ `pause`·`unpause`(暂停·恢复扫描) |
  | `POST /api/edge-report` | 即时跑一次 edge 验证并返回文本结论 + 是否显著 |
  | `POST /api/go-live` | **真钱总闸**(见下) |
  | `POST /api/go-safe` | **一键退回安全态**:切 `manual + dry_run=True`,立即停止一切真实下单 |

  **真钱总闸 `/api/go-live`** 是把 `OPERATIONS.md`「门槛 ①」自动化的代码兑现,也是切 auto 最安全的方式:它自动检查两道门槛 —— ① `run_edge_report` 判 `edge_significantly_positive`;② `.env` 凭证齐全 —— **任一不过就拒绝并列出具体原因,不再依赖用户自觉**;全过才**原子**切到 `executor_mode=auto` + `dry_run=False` + `edge_verified=True` 并解除急停。这意味着 §7 里 `AutoExecutor` 的门 3/门 4 通常不是手动改配置打开的,而是由 go-live 校验通过后一次性放行的。

---

## 9. 回测与验证旁路(离线,不碰实时 API)

这是「把赌博变成科学」的代码兑现,也是上真钱前的漏斗。两条路径**刻意分离**,因为套利当下可判定、方向性需事后结算:

### 套利回测 (`backtest/engine.py`)

`store.replay_markets()` 按时间升序回放历史快照 → 对每个快照重跑 `detector.detect` 找机会 → 每个机会用一个**独立新建的 `RiskGuard`**(避免历史回放中仓位错误累积)过 `guard.assess` → 通过风控的记为可下注信号。输出 `BacktestResult`:`markets_replayed`、`opportunities_found`、`signals_generated`、每笔 `net_edge` 列表及其均值/最大值,`summary()` 给可读报告。纯离线复现,不碰实时 API。

### 方向性 edge 科学验证 (`backtest/edge_report.py`) — 整个项目的命门

遍历每个**已结算**市场的历史快照,在递增前缀(模拟「当时能看到的数据」)上跑 `detect_edge`,用真实结算结果结算每笔(赢赚 `1−买入价`,输亏 `买入价`),汇总统计:

- 样本数 `bets`、胜率、平均单笔收益 `mean_return`、样本标准差。
- **95% 置信区间**(正态近似):`mean ± 1.96 × std/√n`。
- **判定规则(代码自动执行,不靠感觉)**:
  ```
  edge_significantly_positive  ⟺  bets ≥ 30  且  CI 下界 > 0
  ```
  → `[OK] edge 显著为正,可进入小额灰度` / `[NO] 证据不足,不可上真钱`。

这个 `[OK]` 正是 `OPERATIONS.md` 里「门槛 ①」的实现,也正是 `AutoExecutor` 门 4 (`edge_verified`) 要求人工确认后才置 True 的依据。**样本 <30 即便全赢也判 [NO]** —— 小样本漂亮数字被明确拦住。

---

## 10. 扩展指南:想加东西时改哪里

| 想做的事 | 只需动 | 不用动 |
|---|---|---|
| 换/升级 Polymarket SDK | `data/client.py`(+ `execution/auto.py` 的下单调用) | 策略、风控、执行接口、面板 |
| 加一个统计信号 | 在 `strategy/signals/` 加文件 + `combiner.py` 注册权重 | detector、kelly、guard |
| 加一类套利偏差 | `strategy/detector.py` | 其余全部 |
| 换执行后端(如新交易所) | 实现 `Executor` 接口 + `main.build_executors` 注册 | 策略、风控 |
| 调参(阈值/Kelly/熔断) | `config.py`(或网页面板热改白名单字段) | 任何逻辑代码 |
| 加数据源(如另一个现价源) | `data/` 下加模块,注入 `edge_detector` | 策略主逻辑 |

---

## 11. 测试 (`tests/`,10 个文件共 87 项)

全部**依赖注入 + 假数据**,离线不碰网络。`conftest.py` 及各文件提供 `make_market`(构造标准 `Market`)、临时 SQLite `store`、`StaticSource`(假数据源)、以及 `reset_state`/`clean_state`(autouse,快照并还原全局 `STATE`/`CONFIG`,防测试间污染)等 fixture,并有 `FakeNotifier`/`FakeStore`/`FakeGuard`/`FakeClient` 模拟外部 I/O。

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| `test_detector.py` | 7 | 阶段 A 套利检测(Yes/No 互补、互斥、流动性) |
| `test_pricing.py` | 6 | 定价:流动性过滤、滑点、费率、安全系数、阈值 |
| `test_kelly.py` | 8 | 自适应 Kelly 分数、置信度缩放、单笔封顶、复利、流动性约束 |
| `test_edge.py` | 6 | 阶段 B edge 检测、Kelly 信号生成、急停阻塞、连亏熔断 |
| `test_signals.py` | 12 | 动量/盘口失衡/深度背离 + combiner 合成 |
| `test_crypto.py` | 15 | 加密市场解析、对数正态定价、edge 检测、非加密跳过 |
| `test_guard.py` | 7 | 风控两通道、仓位/总暴露/流动性约束、急停管辖 |
| `test_state.py` | 6 | 急停三条件(API 失败/当日亏损/连亏)、重置 |
| `test_auto.py` | 10 | **auto 五重门的拒绝逻辑**(dry-run/edge 未验证/凭证缺失时拒绝) |
| `test_web_control.py` | 10 | 配置热改、halt/pause 运维、edge 报告、真钱总闸/安全退回、白名单校验 |

跑:`python -m pytest tests/ -q`(应全绿)。

> 注:`README.md` 里写的「52 项」已过时,当前实际为 87 项 —— 更新 README 时可一并修正。

---

## 12. 安全红线(架构层面)

1. **manual 阶段零密钥** —— 只读市场数据,不需任何私钥/凭证。
2. **切 auto 才需密钥**,且只写本地 `.env`(已被 `.gitignore` 挡死):专用小额 Polygon 钱包私钥(绝非主资产钱包)+ Polymarket CLOB API 凭证(可用 `get_api_creds.py` 一次性派生)。
3. **两道准入门槛,缺一不可**(详见 `OPERATIONS.md`):① `--edge-report` 判 [OK];② 小额灰度真跑校准滑点。这两道在代码里由 `edge_verified` 门 + `dry_run` 门物理保证。

---

## 13. 已知待办 / 文档提醒

- **依赖声明与实际不符**:`requirements.txt` 仍写 `py-clob-client>=0.17.0`,但代码实际 `from polymarket import ...`(`main.py`、`execution/auto.py`)。README 也说明旧的 `py-clob-client` 已于 2026-05 归档失效。**装依赖时以代码实际导入的 `polymarket`(官方统一 SDK)为准,`requirements.txt` 需要更新。**
- **`AutoExecutor` 的真实下单路径尚未经过实盘验证** —— 当前被 dry_run + edge_verified 双门拦住,属「代码就绪但未走过真钱」状态;进入门槛 ② 小额灰度时需实测校准。
- **edge 回测是快照离散抽样**,会高/低估可成交性,结论仅作方向性参考(`edge_report.py` 自己在文档字符串里诚实标注了这一边界)。

---

*本文描述 2026-07 时点的代码结构。改动主循环、模型或执行门槛时,请同步更新本文与 `README.md` 的架构速览。*
