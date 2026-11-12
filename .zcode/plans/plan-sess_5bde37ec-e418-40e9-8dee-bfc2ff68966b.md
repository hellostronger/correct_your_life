# 个股锚定模式（paper_anchor）实现方案

## Context

用户实盘半仓持有智谱（02513，港股），希望系统围绕**这一只票**持续运作：做T降成本、必要时空仓躲暴跌但**必须择机买回**，唯一目标是在这只票上赚钱。现有模拟盘按全自选池（43只）决策，没有「单票锚定」概念；直接清仓换票、或靠现有 TRADER_PROMPT 自觉都无法保证「锚定+必回补」。

**探索结论：大部分零件已存在，本功能主要是编排层。** 可复用：做T引擎 `t_trading.py`（`decide()` 高抛/接回、`stabilizing()` 止跌确认、`big_drop_plan()` 深跌降本、`[做T]` 标记锚价链，双轨制：模拟盘 `scan_once` 成交 / 实盘 `advisory` 只读）、目标注入模板 `paper_goal.constrain_note`、预案模式 `event_risk`（事前冻结分支+事后匹配+落盘去重推送）、实盘成本推导 `app._derive_holdings()`（sa_trades 流水回放，可自动拿到用户实盘 02513 的成本与仓位占比）。

**用户四项裁决未答复，按推荐项落地且全部做成配置可改**：①模拟盘真实执行；②初始仓位镜像实盘（从 sa_trades 自动推导）；③其他票冻结决策但保留止损；④回补用「预案注入+超期告警」，不硬执行。

## 新文件 `stock-advisor/paper_anchor.py`

1. **配置 `paper.anchor.*`**（进 config_schema.py paper 段，保存走 `set_nested_key`）：
   - `enabled`（**默认 false**，改决策口径，由用户在页面开启）
   - `code`（str，空=未锚定；UI 填 02513）
   - `seed_mode`: `mirror`（默认，模拟盘无持仓时按实盘仓位占比×模拟盘总资产镜像建仓，成本价用实盘 avg_cost）/ `keep`（沿用模拟盘现有持仓）/ `fresh`（空仓起步由策略自建仓）
   - `max_refill_wait_days`（默认 5 个交易日）：空仓超过此天数未回补 → 推送告警
   - `inject.max_chars`（默认 1600）

2. **锚定状态表 `sa_paper_anchor_state`**（自带幂等 DDL，参照 t_trading.py:731 套路）：code, status(`anchored`/`empty`), empty_since, seed_done, mission_note, updated_at。空仓起始日首见时写入，回补成交后翻回 anchored。

3. **镜像建仓 `seed_mirror(deps, conf)`**（一次性、幂等）：
   - `app._derive_holdings()` 取锚定票实盘 `avg_cost` 与 `net_shares`，实盘仓位占比 = 该票市值/实盘总市值；模拟盘按此占比×总资产、100 股取整、成本价按实盘 avg_cost，直接 INSERT `sa_paper_trades` 一行 buy（status='open'，reasoning 带 `[锚定镜像]` 前缀）+ 事务内扣 `sa_paper_account` 现金
   - sa_trades 无该票记录 → 打印说明并降级 `fresh`，**不编造成本**
   - 已 seed 过不重复（state.seed_done）

4. **注入块 `build_injection(cur, code, conf)`**（复刻 institution_signals 签名与 try/except-返回"" 套路，调用点 `paper_trading._decide_one` with 块内、追加在 strategy 块之后）：
   - 只对锚定票输出，**其他票返回 ""（上下文逐字节不变）**；enabled=false 同样不变
   - 内容：使命声明（唯一目标=该票累计赚钱；卖出的票必须择机回补，躲暴跌不是止盈离场）+ 当前进度（初始成本→当前成本、已实现盈亏合计、做T次数、当前空仓天数）+ 回补预案（若空仓：写明回补参考条件——止跌确认+阈值，复用 t_trading 的 `stabilizing`/`_refill_threshold` 语义，及超期倒计时）

5. **回补监控 `refill_watch(deps, conf)`**：挂进 `_paper_run_cycle` 尾部（止损/决策/做T之后）：空仓时更新 state、超过 `max_refill_wait_days` 个交易日经 `notify_fn` 推送（去重，参照 event_risk 落盘去重键模式）

## 接线改动（现有文件）

| 文件 | 改动 |
|---|---|
| `paper_trading.py` | ①`run_decisions` 加可选参数 `anchor_code`（默认 None→行为逐字节不变）：锚定票**永远进候选**（不被「持仓满剔未持仓」逻辑挤掉，:1232-1240 处豁免）；②`_execute_decision` 的 buy 分支：锚定票买入**豁免持仓上限**闸门（否则空仓回补会被其他持仓占满卡死），豁免仅当调用方显式传 anchor 标记 |
| `app.py` | ①`_paper_run_cycle`：anchor.enabled 时 `run_decisions(only=[code], anchor_code=code)`，其余票不进 LLM 决策（`check_stop_loss` 照常全持仓生效，不动）；②t_trading scan_once 照常（做T引擎自己遍历持仓，锚定票空仓时自然无做T动作）；③新端点 `GET /api/paper/anchor`（状态+绩效）、`POST /api/paper/anchor/seed`（手动触发镜像建仓，幂等）；④`refill_watch` 调用 |
| `config_schema.py` | paper 段加 `paper.anchor.*` 嵌套键（t/d/lo/hi/label/help 格式） |
| `static/index.html` | 新 tab「🎯 锚定」：状态卡（锚定票/成本线/已实现盈亏/空仓天数）+ 做T与躲跌事件表 + 超期告警徽标；TAB_LOADERS 注册（注意 index.html:12090 手写清单同步的坑） |

锚定绩效（成本线、累计盈亏、做T记录、躲跌区间 sell→buy 对）全部从 `sa_paper_trades` WHERE code=锚定票 聚合，**不新增交易表**。

## 明确不做 / 不碰

- 不改 `_execute_decision` 其余闸门链（时段、quote 新鲜度、追高闸门、ATR 止损）、不改止损阈值
- 不改 paper_goal、其他注入块、其他票的止损与结算（settle_and_reflect 照常）
- 回补**不由代码硬执行**（满足条件仍由 LLM 结合注入预案决策）——按推荐项④
- `config.yaml` 不提交；只提交 config.example.yaml

## 验证

1. **离线 `test_paper_anchor.py`**（Fake cursor，不联网不烧 LLM）：注入块非锚定票返回 ""（逐字节断言）、空仓预案文本含超期倒计时、候选池收窄（锚定票空仓仍在候选/其他票剔除）、镜像建仓幂等（seed_done 不重复）与无实盘记录降级 fresh、超期告警触发与去重、`_execute_decision` 非锚定路径行为不变、静态接线（app.py 调用点/TAB_LOADERS/schema 默认值）
2. **回归**：现有 paper 相关测试套件全过（注入块追加不应影响既有块顺序 evo→inst→strategy→anchor→mp）
3. **端到端**：重启本地 app（127.0.0.1:8686）→ `/api/paper/anchor` 200；anchor 关闭时跑一轮决策确认上下文逐字节不变；用户在页面开启并填入 02513 后验证镜像建仓与下一轮 only=[02513]
4. 汇报时明确告知：模拟盘 ≠ 实盘收益，镜像只是同口径起步