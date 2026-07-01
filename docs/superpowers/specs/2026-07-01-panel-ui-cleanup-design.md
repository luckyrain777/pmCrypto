# 控制台面板优化设计

日期：2026-07-01
范围：`web/static/index.html`、`web/server.py`、`data/store.py`、`main.py`

## 背景

用户在使用 pmCrypto 本地控制台时提出 4 个问题：

1. "建议/已下单信号"标题有歧义——系统里没有任何成交记录，实际只有"建议信号"。
2. 市场列（信号/候选机会/最新市场）常显示 `0x…` 截断地址而非市场问题文本，难以查看。
3. "真钱总闸 / 运维控制 / 策略开关"等按钮 UI 粗糙、山寨，需要重新设计。
4. （第 3 点里包含的）"启用真钱自动交易 / 回到安全" 按钮属于这次视觉重设计范围。

经确认的决策：
- 成交数据源：**系统里没有任何成交记录**，手动单在本系统外操作。
- 信号面板：**改名为"建议信号"即可**，不新建成交表、不做持仓对照。
- 市场名称：**信号存问题文本快照**——最彻底方案。
- UI 风格：**专业交易终端风格**。

## 目标与非目标

**目标**
- 消除"建议/已下单"歧义。
- 让 signals / opportunities 列表稳定显示市场问题文本，不再退化成 0x 地址。
- 把面板按钮重做成专业交易终端观感，主/次/危险操作层级清晰。

**非目标**
- 不新建 trades/fills 表，不做成交补录入口。
- 不改动交易/风控/edge 验证逻辑。
- 不改后端 API 契约（除新增 question 快照字段外）。

---

## 改动一：信号面板改名（歧义消除）

**问题**：`signals` 表 schema 无"是否已成交"字段，面板标题"建议/已下单信号"名不副实。

**改动**（纯前端，`index.html`）：
- 卡片标题 `建议/已下单信号` → `建议信号（仅提示）`。
- 保留空态文案"暂无信号（机会稀少属正常）"。

无后端改动，无数据结构改动。

---

## 改动二：信号存问题文本快照（市场名称）

**问题**：前端 `nameOf(id)` 依赖"最新市场快照 `qmap`"来把 `market_id` 翻成 question。当信号引用的市场已被后续轮次的快照分页挤出 `latest_market_snapshots(limit=100)`，`qmap` 查不到，就退回 `short(id)`（0x 截断）。

**方案**：在信号/机会**产生的那一刻**就把当时的市场问题文本存进各自的表。这样即使市场快照轮换掉，信号自身也永远带名字。

**数据层（`data/store.py`）**
- `signals` 表新增列 `question TEXT`；`opportunities` 表新增列 `question TEXT`。
  - 用与现有 `end_ts` 相同的幂等升级手法：`_init_schema` 里 `PRAGMA table_info` 检查列是否存在，缺则 `ALTER TABLE ... ADD COLUMN question TEXT DEFAULT ''`。旧库平滑升级，不需要重建。
- `save_signal(sig, created_ts)` → `save_signal(sig, created_ts, question="")`：INSERT 语句写入 question 列。
- `save_opportunity(opp)` → `save_opportunity(opp, question="")`：同上。
- `recent_signals` / `recent_opportunities` 的 `SELECT` 天然带出新列（用 `SELECT *` 或显式列表，按现有写法补上 question）。

选择在写库函数加**可选参数**、而非给 frozen dataclass `Signal`/`Opportunity` 加字段：
- `Signal`/`Opportunity` 是 `@dataclass(frozen=True)`，加字段要改多个 detector 的构造点（detector.py 2 处、edge_detector.py 2 处），牵动面大。
- 写库时 `market` 对象就在 `main.py` 手边（`market.question` 直接可取），传参最省。
- 默认值 `""` 保证其它调用方（若有）不受影响。

**调用层（`main.py`）**
- `store.save_opportunity(opp)` 三处 → `store.save_opportunity(opp, question=market.question)`。
- 阶段A：`executor.execute(signal)` 前后没有直接 `save_signal` 调用——确认 signal 落库发生在哪。

  > 待实现时核对：`save_signal` 的实际调用点。grep 显示 main.py 只调 `save_opportunity`，signal 落库可能在 `executor.execute` 内部或 guard 里。实现第一步先定位 `save_signal` 的调用者，在那里把 question 传进去（该处能拿到 market_id，需回溯 market.question；若拿不到 market 对象，则退一步用 opportunity 已存的 question，或由调用链透传）。

**前端（`index.html`）**
- `nameOf(id)` 改为**优先用信号自带的 question**，其次才回退 `qmap`，最后才 `short(id)`：
  - `signals` / `opportunities` 的 render 里，`r.question` 若非空直接用 `r.question.slice(0,42)`，否则走原 `nameOf(r.market_id)`。
- "最新市场"表本就直接用 `r.question`，无需改。

**兜底**：升级前产生的旧信号 question 为空，仍走 qmap/short 回退，不报错。

---

## 改动三：按钮 UI 专业交易终端重设计

**问题**：当前按钮是扁平色块 + 纯 emoji（`.btn-live`/`.btn-safe`/`.btn-danger`/`.btn-warn`/`.btn-mut`），缺乏层级与质感。

**设计原则（专业交易终端风格）**
- 克制配色：延续现有深色底 `#0f1115`，按钮不再是大面积高饱和色块。
- 明确操作层级：
  - **主操作 / 危险操作**（启用真钱、全局急停）：实心、边框高亮、更醒目；真钱/急停用红色系但收敛饱和度，加图标 + 文字。
  - **安全 / 正向操作**（回到安全、解除急停）：绿色系，次一级视觉重量。
  - **次要操作**（运行 edge 报告、暂停扫描、保存参数）：描边/幽灵按钮（ghost），低视觉重量。
- 统一视觉语言：一致的圆角（比现有更收敛，6px）、内边距、字重、hover/active 反馈（轻微亮度变化 + 细边框），去掉粗糙的纯色 hover 跳变。
- 图标：保留语义图标但收敛（用统一的字形符号而非彩色 emoji 大面积铺陈），或 emoji 缩小并与文字对齐。
- 真钱/急停这类高危按钮：加更明确的视觉警示（红色描边 + 略强的 hover），呼应"危险操作"语义。

**实现范围**：仅 `index.html` 的 `<style>` 按钮相关规则 + 按钮 class 结构调整。不动 JS 行为、不动 onclick 绑定、不动 API。

**交付方式**：实现时先做 2–3 套按钮视觉对比（截图/静态 HTML 片段）给用户挑，再落地选中方案——避免一次性猜错风格返工。

---

## 数据流

```
main.py 扫描循环
  ├─ save_market_snapshot(market)            # 不变
  ├─ save_opportunity(opp, question=market.question)   # 新增 question
  └─ (save_signal 调用点) 透传 question
                    │
                    ▼
        SQLite: signals.question / opportunities.question  # 新增列，幂等升级
                    │
                    ▼
        /api/state → recent_signals / recent_opportunities（带 question）
                    │
                    ▼
   前端 nameOf: r.question ?? qmap[id] ?? short(id)
```

## 错误处理

- 列升级：`PRAGMA table_info` 缺列才 `ALTER`，重复启动幂等。
- 旧信号 question 为空：前端回退链保证不崩。
- question 传参默认 `""`：任何未透传的调用点不报错，只是回退到旧行为。

## 测试

- **数据层**：新增/沿用 store 测试——写入带 question 的 signal/opportunity，`recent_*` 能读回 question；对无 question 列的"旧库"文件执行 `_init_schema` 后列存在且旧数据 question 为空字符串（幂等升级验证）。
- **前端**：手动核对——制造一个引用了已被挤出快照的市场的信号，确认列表显示 question 而非 0x（可在本地库插一条带 question 的 signal 验证）。
- **UI**：视觉对比 + 用户确认；按钮 onclick 行为回归（点击各按钮仍触发原 API）。
- 全量 `pytest` 通过。

## 风险

- `save_signal` 调用点未在 main.py 直接可见——实现第一步必须先定位，若透传 question 需要跨层，可能小幅调整 guard/executor 签名。已在改动二标注为待核对项。
- 面板是单文件 `index.html`，样式改动集中，回归面小。
