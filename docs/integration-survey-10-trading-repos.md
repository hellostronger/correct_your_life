# 10 个交易类开源项目 × correct_your_life：接入评估与落地路线

- 评估日期：2026-10-25
- 评估对象：Kronos、TradingAgents、day-trader-skill、Vibe-Trading、last30days-skill、daily_stock_analysis、FinceptTerminal、freqtrade、tick-stock-panel、OpenBB
- 评估方法：逐个读 README / LICENSE / GitHub API 元数据；查 PyPI 版本与依赖数；用 `docker manifest inspect` 验证镜像；对照本仓库 `stock-advisor/` 现有实现。
- 边界：本文只做选型与接入设计，**没有把任何项目装进 `stock-advisor` 的运行环境**，也没有改动数据库。文末列出未验证项。

---

## 0. 结论

### 0.1 建议清单

| 优先级 | 项目 | 接入面 | 建议动作 | 理由 | 预估 |
|---|---|---|---|---|---|
| **P0** | **Vibe-Trading** | **REST + MCP sidecar** | 独立进程接入，先用 `local:` 数据桥 + Alpha Zoo 做研究/回测交叉验证 | MIT；A 股规则与数据链完整；478 个 alpha；REST + MCP 齐全；支持自有 Parquet/DuckDB | 3–5 天 |
| **P1（按需）** | **tick-stock-panel** | **REST + MCP sidecar** | 只有在确实需要分钟线 / 68 列指标 / 因子排名时，再起 Docker 只读接入 | MIT；单容器；65 端点 + 12 MCP；但与本项目重合度最高 | 1–2 天 |
| **P1** | **daily_stock_analysis** | **Skill（策略库）** | 把 15 种策略分类与提示词骨架封装成 skill，不部署整套系统 | 15 种策略问股、多市场、推送齐全；但与现有系统约 70% 重合 | 0.5 天 |
| **P1** | **last30days-skill** | **Skill（CLI 封装）** | 封装“近 30 天事件/舆情”skill，输出带链接的简报给 LLM 顾问 | 65k star；Reddit/HN/Polymarket/GitHub 零配置；但英文世界为主 | 0.5 天 |
| **P1** | **day-trader-skill** | **Skill（流程模板）** | 封装 scan→news→entry→exit→replay→risk 检查表，不接券商 | 11 个 Claude skills，结构清晰；但仅美股日内、仓库只有 5 天 | 0.5 天 |
| **P2（实验）** | **Kronos** | **Notebook** | 只在离线 notebook 里试“K 线预测因子”，不进实时链路 | MIT、43.7k star；但最后提交 2026-04、286 个 open issue、需 GPU | 2–3 天 |
| **P2（暂缓）** | **OpenBB** | — | 先不接；若全球宏观/美股基本面成为一等需求再评估 | Apache-2.0；但仓库约 2.4 GB、36 依赖，README 无 A 股覆盖证据 | — |
| **P3（已完成）** | **TradingAgents** | **代码移植（冻结）** | 不安装；只跟踪上游 | 核心三件事已移植进 `paper_trading.py`/`debate.py`，再装 LangGraph 是重复 | — |
| **不接入** | **FinceptTerminal** | — | 拒绝作为依赖；只看交互/功能设计 | AGPL-3.0 + 商业条款：商业或公司内部使用必须付费；C++/Qt 桌面、约 790 MB | — |
| **不接入** | **freqtrade** | — | 现在不接；若做加密再单独容器 | GPL-3.0；只做加密现货/合约，与 A 股主线无关；85 个依赖、需 7×24 进程 | — |

> **P0 状态（2026-10-25）**：应用侧接入与**服务部署都已完成**。sidecar 跑在
> `101.43.25.101`（systemd + nginx `/vibe/` 反代 + Bearer 鉴权，不新开端口），
> 本站通过 `VIBE_BASE_URL` 直连；LLM 复用 101 本机网关。研究范围是**国内 A 股 + 港股**：
> 因子库 `equity_cn` 377 个、`equity_hk` 180 个，bench 只认 `csi300`（上游 0.1.18
> 没有港股基准），模板滤掉 7 个加密/商品/信用/宏观/全球混合项。
> 仍未完成：① 研究数据副本（4.1.2）；② 与 `backtest.py` 的同窗口对账；
> ③ `csi300` bench 需要 Tushare token（尚未提供），所以 bench 仍不可跑。
> 其余 9 个项目维持上表结论（TradingAgents 为既有代码移植，不算新接入）。

### 0.2 一句话架构

不合并代码，只加两类边界：**服务型 sidecar**（REST/MCP）与**流程型 skill**（薄脚本）。现有 PostgreSQL、回测、模拟盘、通知继续做唯一事实源。

```
┌────────────────────────────── correct_your_life ──────────────────────────────┐
│                                                                              │
│  stock-advisor (FastAPI :8686, 256 endpoints, static/index.html)             │
│    ├── 现有：自选/持仓/交易/回测/模拟盘/辩论/通知/公众号/B站/微博/X …          │
│    ├── 新增：integration_api.py ──HTTP──┐                                    │
│    └── 现有：PostgreSQL (sa_*)            │                                   │
│                                          ▼                                   │
│  ┌─────────────────────┐        ┌──────────────────────────┐                 │
│  │ Vibe-Trading sidecar│        │ tick-stock-panel sidecar │  （P1，按需）   │
│  │ :8899 REST / :8900  │        │ :3018 Open API / MCP     │                 │
│  │ MCP, 478 alphas     │        │ Docker + Parquet         │                 │
│  └─────────┬───────────┘        └────────────┬─────────────┘                 │
│            │ local: Parquet/DuckDB           │ read:market (token)           │
│            ▼                                 ▼                               │
│      research-data/  ←── 从 sa_market_kline 导出的“研究副本”（前复权、股）      │
│                                                                              │
│  Skills（薄脚本 + SKILL.md，不引入进程）：last30days / day-trader / 策略库     │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 0.3 三个关键发现（会直接改变接入方式）

1. **Vibe-Trading 支持“自有数据”**：`local` loader 可读 CSV / Parquet / DuckDB，`local:` 前缀的标的**永不回退到网络源**；基准支持 `.SH/.SZ/.BJ`。这意味着可以让它跑**和我们同一份行情**，而不是拿另一家数据源的结果来“对照”。这是本次评估里最有价值的一条。
2. **但我们的库不能直接喂给它**：`jq_api.py` 明确写了本地日线是**不复权**；`bt_data.py` 记录 `volume` 单位是**手**。多 alpha 需要复权价和股数，直接接会引入分红/送转和 100 倍成交量偏差。必须先做“研究副本”。
3. **tick-stock-panel 是完整工作台，不是数据 SDK**：65 个端点、12 个 MCP 工具、68 列指标、策略/回测/监控/因子挖掘都有，和现有系统重合度最高。只有出现“分钟线 / 因子排名 / 指标流水线”这种明确缺口时才值得加第二个 sidecar。

### 0.4 接入原则：接口优先，Skill 封装，不做 vendoring

这些项目都在高频迭代（Vibe 评估当天还有提交），把代码复制进来等于把上游的升级成本转嫁给自己。因此只在一种情况下做代码移植：**这段逻辑已经稳定，且我们必须改造它**——TradingAgents 的三件核心就是这种例外，移植后冻结。其余一律走边界：

| 接入面 | 适合什么 | 本项目用法 | 稳定性手段 |
|---|---|---|---|
| **REST** | 有状态、重计算、要确定性结果 | Vibe Alpha bench、TSP 行情/指标 | 适配器 + OpenAPI 快照 + 契约测试 |
| **MCP** | 给 Agent 动态发现工具 | Vibe 82 工具、TSP 12 工具 | 工具白名单 + 只读 scope + 超时 |
| **Skill** | 文本/流程/多步编排，上游是 CLI 或提示词 | last30days、day-trader、daily 策略库 | `SKILL.md` + 薄脚本 + JSON 输出契约 |
| **Notebook** | 一次性实验、模型训练 | Kronos | 不进入运行时 |
| **代码移植** | 能力已稳定且必须改造 | TradingAgents | 冻结版本、记录来源、不跟踪内部 API |

三条硬规则：

1. 主应用只调 `integrations/*` 适配器或 `skills/*` 脚本，**不直接 import 上游包**；上游 JSON 不直接进数据库或前端。
2. 上游升级 = 换容器/换 CLI 版本 + 跑契约测试；适配器对外 DTO 不变，页面无感。
3. sidecar/skill 挂了，主应用优雅降级（隐藏入口或返回“研究服务不可用”），不阻塞行情、持仓、通知。

---

## 1. 基线：correct_your_life 已经有什么

### 1.1 现有形态

| 维度 | 现状 | 证据 |
|---|---|---|
| 主应用 | 单进程 FastAPI，`app.py` 10,618 行，256 个 `/api/*` 端点 | `grep -c '@app\.' app.py` |
| 前端 | 单文件 `static/index.html`，540 KB，无前端构建链 | `ls -lh static/` |
| 存储 | 云 PostgreSQL，`sa_*` 表；`sa_market_kline` 约 334 万行 | `bt_data.py` 注释 |
| 数据 | 东财/腾讯日 K、板块数据服务（独立 proxy `101.43.25.101:8080`）、akshare 调研 | `data_service/README.md`、`docs/data-source-survey.md` |
| 回测 | 自研：次日开盘成交、T+1、涨跌停买不进、停牌跳过、12 项费率、基准自动选择 | `backtest.py` 文件头 |
| 模拟盘 | LLM 决策 → 收盘成交 → 5 日结算 → 反思沉淀，alpha 判定 | `paper_trading.py` 文件头 |
| 多智能体 | 已移植 TradingAgents 的 trader JSON、反思循环、alpha 记忆；另有 `debate.py` | `paper_trading.py` 注释 |
| 策略 | 聚宽沙箱、策略日报、策略库、参数进化 | `jq_sandbox.py`、`strategy_daily.py`、`/api/strategy-lib` |
| 通知 | iLink 微信、邮件、Telegram 等 16 个 notify 端点 | `/api/notify/*` |
| 内容监听 | B 站、微博、公众号、X、小红书、雪球 | `/api/bili|wb|mp|x|xhs` |
| 研究/分析 | Claude 顾问（fast/standard/deep 三档）、新闻/事件、板块轮动、宏观 | `llm_advisor.py` |

### 1.2 已有能力 vs 缺口

| 能力层 | 已有 | 缺口 | 本次候选 |
|---|---|---|---|
| 行情/基本面 | A 股日 K、估值、板块、资金流、公告/新闻 | 分钟线、逐笔、部分财务口径、全球数据 | TSP / Vibe / OpenBB |
| 因子/Alpha | 策略文章里的“指标”，没有统一因子库 | 可复现的 alpha 库、IC/换手/衰减、批量 bench | **Vibe（478 alphas）**、TSP（因子挖掘） |
| 回测 | A 股规则完整，指标齐全 | 组合级/多因子/分钟级；跨引擎交叉验证 | Vibe / TSP（只做交叉验证） |
| 模拟/执行 | LLM 模拟盘、聚宽沙箱；无真实券商 | 真实下单、组合再平衡 | Vibe（18 家券商）/ freqtrade（加密） |
| 研究/Agent | Claude 顾问 + 辩论 | 工具化研究循环、可引用 run card、近 30 天事件 | **Vibe MCP**、last30days |
| UI | 单页 540 KB | 研究工作台、图表交互 | TSP / Vibe Web（建议外链，不嵌入） |

**结论：现有系统的“数据 → 回测 → 模拟 → 通知”闭环是完整的。接入的价值必须落在“因子/Alpha 研究”和“研究工具化”，而不是再造一套回测或模拟盘。**

---

## 2. 十个项目逐个评估

> Stars / 最近提交为 2026-10-25 通过 GitHub API 抓取，数值会变；“许可”以仓库 LICENSE 与 API 的 `license.spdx_id` 为准，不一致处单独标注。

### 2.1 Vibe-Trading — P0，唯一以服务形态接入的项目

**是什么**：HKUDS 的自然语言金融研究 Agent。Python，FastAPI Web + CLI + REST + MCP，能跑回测、因子分析、Alpha Zoo、模拟盘、组合、期权、定时研究。

**关键证据**

- 36,608 star / 5,913 fork，MIT，创建 2026-04-01，最近提交 2026-10-25；PyPI `vibe-trading-ai 0.1.18`，要求 Python ≥3.11，**112 个依赖**。
- 29 个行情源，A 股回退链：`tencent → eastmoney → baostock → akshare → tushare → gildata → local`；显式 `local:` 不回退。
- **478 个预置 alpha**：Qlib 158 + Kakushadze 101 + GTJA 191 + 学术/基本面。
- **82 个 MCP 工具**；HTTP 传输默认 `http://127.0.0.1:8900/mcp`。
- REST：`/runs`、`/sessions`、`/alpha/list`、`/alpha/bench`、`/scheduled-runs`、`/settings/*` 等。
- 模拟盘：`vibe-trading paper`、REST、Web 三类入口，按已完成日线撮合，不触达券商。
- 5 个可直接定时的 playbook，其中包含 **`a-share-money-flow`**。
- 回测 run card 带哈希执行记录和“指标 → CSV”引用，适合做可追溯研究。

**与现有系统重合**：回测、模拟盘、辩论、通知、数据代理都有。**差异**在于：Alpha Zoo / 因子 bench、Research Autopilot（假设 → 回测 → 证据）、run card 溯源、MCP 工具面、定时研究、期权/多市场。

**接入边界**

- 只做 **sidecar**：独立 venv 或独立容器，`pip install vibe-trading-ai==0.1.18`，绝不装进 `stock-advisor` 的环境（112 个依赖，会污染现有 `fastapi 0.128` / `anthropic` 组合）。
- 只开只读能力：`/alpha/list`、`/alpha/bench`、`/runs/{id}`、`/sessions`；**不给券商凭证**，**不开 shell 工具**，**先不开 scheduler**。
- 端口只绑 `127.0.0.1`，设置 `API_AUTH_KEY`；`VIBE_TRADING_HOME` 挂独立卷。
- 不把它的模拟盘当事实源；现有 `paper_trading.py` 仍是唯一模拟账本。

**风险**

- 上游几乎每天合并 PR，API/字段会漂移 → 锁版本，升级走单独分支 + 契约测试。
- 生成的策略代码会作为本地 Python 子进程运行（README 说明环境已收窄），仍应放在容器里。
- Alpha 的定义和质量需要在 A 股上复核；不能因为“478 个”就默认有效。
- 自有数据桥要做复权与单位换算，否则结果不可比。

### 2.2 tick-stock-panel — P1，按需接入

**是什么**：A 股本地量化工作台。React + FastAPI + Polars/DuckDB/Parquet，单 Docker 容器，覆盖策略、回测、监控、因子挖掘、AI 助手、Open API、MCP。

**关键证据**

- 6,309 star / 1,493 fork，MIT，创建 2026-06-18，最近提交 2026-10-24。
- 单容器：`docker run -d -p 3018:3018 -v ${PWD}/data:/app/data ghcr.io/shy3130/tick-stock-panel:latest`；已用 `docker manifest inspect` 验证镜像存在（amd64/arm64）。
- 65 个 Open API 端点、12 个 MCP 工具、六档 scope（`read:market` / `read:ext` / `write:ext` / `read:analysis` / `run:backtest` / `paper:trade`）；**MCP 刻意不暴露模拟盘交易**。
- 68 列指标/信号，一次扫表落 enriched Parquet；数据源插件化：TickFlow 官方 SDK、fuyao（同花顺 REST，自备 Key）、TdxQuant（通达信量化）、YAML 自定义源。
- `TICKFLOW_API_KEY` 留空 = None 模式，**历史日 K 免费**。
- Open API 示例：`GET /api/kline/daily?symbol=600519.SH&limit=10`。

**与现有系统重合**：自选、持仓提醒、信号、回测、监控、板块轮动、AI 助手、告警——几乎每一项都有对应页面。**差异**：分钟线、68 列指标流水线、因子全市场排名、OpenAPI/MCP 的稳定契约、Parquet 本地数据层。

**接入边界**

- 只在“确实需要分钟线/指标/因子排名”时启用；不要替换现有 PostgreSQL。
- 只发 `read:market` / `read:analysis` / `run:backtest` 三个 scope；**不要**发 `write:ext` 和 `paper:trade`。
- 只在本机/内网调用，不公网暴露；Token 默认仅本机/内网，公网 403。
- 用 `/api/kline/daily` 与 `sa_market_kline` 做逐日对账；不一致时以现有库为准，先查数据源口径。

**风险**

- 项目较年轻（2026-06 创建），README 同时出现“65 端点”和 v0.3.2 的“61 端点契约”两种说法，**以 `/api/openapi.json?tier=a` 的实际快照为准**。
- 数据来自 TickFlow/fuyao/TdxQuant，免费档有边界，商用/再分发要看各数据源条款。
- 容器内置 Codex CLI 并挂载 `~/.codex`（README 的 compose 说明），攻击面比纯 API 服务大；建议只跑 API、不挂宿主机 Codex 目录。
- AI 助手是数据分析工具，明确不提供买卖指令；不要把它接进自动交易链路。

### 2.3 daily_stock_analysis — P1，Skill 化策略库

**是什么**：A/H/美/日/韩/台股 + ETF 的每日智能分析系统，FastAPI/Docker/GitHub Actions，15 种策略问股，推送到企业微信/飞书/Telegram/Discord/Slack/邮箱。

**关键证据**

- 67,028 star / 55,621 fork，MIT，最近提交 2026-10-21。Fork/Star 约 83%，是典型的“fork 后跑 Actions”模板型仓库，不是库。
- 行情源：AkShare、Tushare、Pytdx、Baostock、YFinance、Longbridge；覆盖 A 股、港股、美股、日股、韩股、台股和 ETF。
- Agent 策略问股：均线、缠论、波浪、趋势、热点、事件、成长、预期等 **15 种内置策略**，覆盖 Web/Bot/API。
- 推送：企业微信/飞书/Telegram/Discord/Slack/邮件；支持 GitHub Actions、Docker、本地定时、FastAPI。

**与现有系统重合**：自选、LLM 分析、每日报告、推送、定时任务——约 70%。**差异**：15 种策略的分类与提示词组织、多市场覆盖、决策仪表盘的信息层级、Actions 零服务器部署。

**建议**：封装为 `skills/strategy-taxonomy`，不部署整套系统（会与现有通知/LLM 成本重复）。这个 skill 只装三样东西：

1. 15 种策略的**分类法与提示词骨架**，映射到 `sa_analysis_techniques` / `llm_advisor` 的 deep 档；
2. 决策仪表盘的**信息层级**（结论 → 关键价位 → 风险 → 证据），可用于现有报告的排版；
3. 多市场数据源的**能力边界表**，补充 `docs/data-source-survey.md`。

**风险**：高重合、提示词质量参差、多数据源配额；不要把它的推送和现有 iLink/邮件并列成第二套通知。

### 2.4 last30days-skill — P1，Skill 封装

**是什么**：跨平台“最近 30 天”研究 skill/CLI，按真实互动量打分，输出带引用的简报。

**关键证据**

- 65,372 star / 5,674 fork，MIT，最近提交 2026-10-23。
- 零配置源：Reddit、Hacker News、Polymarket、GitHub；向导解锁 X、YouTube、TikTok、arXiv、Techmeme 等；标的是股票/加密时自动启用 StockTwits。
- 打分依据是 upvotes / likes / transcripts / engagement / 预测市场赔率，而不是单纯搜索排序。

**适配判断**：英文世界强，A 股弱；对“全球主题/美股/加密/宏观事件”有价值，对 A 股个股的直接价值有限。

**建议**：封装为 `skills/last30days-research`，作为 `llm_advisor` 的**可选研究工具**：

- 全球产业主题（AI 资本开支、算力、加密监管）；
- 美股/加密标的的事件与情绪；
- 给报告补充“过去 30 天可点击的证据链接”。

**边界**：只读、只产文本与链接；不写库、不触发交易；输出必须保留来源链接，不能让模型把摘要当事实。

### 2.5 day-trader-skill — P1，Skill 化流程

**是什么**：11 个 Claude Code skills 组成的日内交易工作流：扫图 → 新闻 → 入场 → 出场 → 时间机器复盘 → 预算 → 风控 → 券商。

**关键证据**

- 1,246 star / 283 fork，MIT，创建 2026-10-20（评估时仅 5 天）。
- 纯标准库 Python；数据用 Yahoo Finance charts、Nasdaq Trader 符号表、SEC EDGAR；`/trader-broker` 默认 paper trading，必须人工确认才下单。
- 明确是**美股日内**；没有 A 股 T+1、涨跌停、集合竞价规则。

**建议**：封装为 `skills/trade-checklist`，不直接运行上游；只保留三件事：

1. `scan → news → entry → exit → replay → budget → risk → broker` 的**单向状态机**；
2. “先在历史里回放同一 setup，再决定是否承担风险”的**证据门**；
3. 每个动作都要人工确认的**权限边界**——与现有“通知而不是自动下单”的定位一致。

**风险**：仓库极新，未经时间检验；数据与规则都是美股；不要把它接到真实券商。

### 2.6 Kronos — P2，离线预测实验

**是什么**：K 线基础模型（K-line tokenizer + 自回归预测），提供预训练权重和 Qlib A 股微调/回测流水线。

**关键证据**

- 43,666 star / 7,212 fork，MIT，最后提交 **2026-04-13**，286 个 open issue。
- 预训练模型在 Hugging Face；微调示例用 Qlib 准备 A 股数据并做简单回测；需要 GPU。

**适配判断**：可以作为“预测因子”实验，但不能当实时信号：

- 输入：现有日 K（必须先复权）；
- 输出：未来 1–5 日收益/分位数，落成一张因子表；
- 评估：IC、分层收益、换手、与现有动量/反转因子的相关性；
- 只有 IC 稳定且增量显著，才考虑进策略。

**风险**：半年未更新、issue 多、GPU 成本、过拟合；模型输出不解释，不能直接进报告结论。

### 2.7 OpenBB — P2，暂缓

**是什么**：Open Data Platform（ODP）+ Workspace，把多供应商数据归一化后暴露给 Python、REST、MCP、Excel。

**关键证据**

- 74,669 star / 7,761 fork；LICENSE 明确写 Apache-2.0，但 GitHub API 的 `license.spdx_id` 是 `NOASSERTION`；仓库约 2.4 GB；PyPI `openbb 5.0.0`，36 个依赖。
- README 强调“connect once, consume everywhere”：Python、Workspace、Excel、MCP、REST。
- README 中**没有** A 股/China/Tushare/AkShare 的覆盖说明（这不等于一定没有 provider，但至少不是卖点）。

**适配判断**：现有 `data_service` 已经解决了“多厂商归一化 + 显式降级”的问题；OpenBB 的体积和依赖与收益不匹配。只有当“全球宏观/美股基本面/期权链”成为一等需求时再评估。

**风险**：仓库大、provider 需要各自 Key、许可元数据不一致（使用前必须核对 LICENSE 与实际 provider 条款）。

### 2.8 TradingAgents — P3，已完成移植

**是什么**：LangGraph 多智能体交易研究框架（分析师 → 多空辩论 → 交易员 → 风控）。

**关键证据**

- 113,843 star / 22,049 fork，Apache-2.0，最近提交 2026-10-16。
- 支持 A 股代码：Shanghai `.SS`、Shenzhen `.SZ`（如 `600519.SS`）。
- 本仓库 `paper_trading.py` 文件头明确写“设计移植自 TradingAgents 三样核心”：结构化 trader 决策、反思循环、alpha 判定；`debate.py` 也有多空/风控实现。

**建议**：不安装 LangGraph 版本。只跟踪上游的三类改进：辩论角色约束、风控 veto 逻辑、反思的记忆注入。出现值得抄的改动时，手工移植到现有模块。

**风险**：再装一套 LangGraph 会带来依赖冲突和“两套决策逻辑漂移”；没有必要。

### 2.9 FinceptTerminal — 不接入

**是什么**：C++20/Qt6 + 嵌入式 Python 的桌面金融终端，41 个模块、六大工作台。

**关键证据**

- 33,072 star / 4,712 fork，最近提交 2026-10-13，仓库约 790 MB。
- LICENSE 是 **AGPL-3.0 + 商业许可**双轨：AGPL 仅限个人使用、个人学习、个人学术研究和向本仓库贡献；**任何商业或公司内部使用，无论收入、规模、时长，都必须购买商业许可**；商业用途不适用 AGPL 选项。
- 桌面形态，没有可复用的服务 API 边界。

**结论**：不作为依赖、不复制代码。个人自用虽在 AGPL 范围内，但“通过网络提供服务”会触发源码开放义务；且 C++/Qt 桌面无法嵌进 FastAPI 单体。只参考它的信息架构和模块划分。

### 2.10 freqtrade — 不接入（除非做加密）

**是什么**：加密现货/合约交易机器人，支持 dry-run、回测、hyperopt、FreqAI、WebUI、Telegram。

**关键证据**

- 55,907 star / 11,565 fork，**GPL-3.0**，最近提交 2026-10-25；PyPI `freqtrade 2026.9`，85 个依赖，Python ≥3.11。
- 只做加密货币，不覆盖 A 股。

**结论**：与主线无关。若未来做加密，作为**独立容器**运行，通过 REST/Telegram 交互，绝不把 GPL 代码 import 进 `stock-advisor`。届时它是“另一个交易系统”，不是本项目的一个模块。

---

## 3. 分层判断

| 层 | 现有实现 | 候选能力 | 判断 |
|---|---|---|---|
| 行情/基本面 | 东财/腾讯日 K、估值、板块、资金流、公告 | Vibe 29 源；TSP 65 端点；OpenBB provider | **保留现有为事实源**；sidecar 只读，不双写 |
| 因子/Alpha | 无统一因子库 | Vibe 478 alpha + bench；TSP 因子挖掘 | **最高价值**，优先接 Vibe |
| 回测 | 自研 A 股规则完整 | Vibe/TSP 日线引擎 | **现有引擎做裁判**；Vibe 只做 alpha bench 与交叉验证 |
| 模拟/执行 | LLM 模拟盘、聚宽沙箱；无真实券商 | Vibe paper；TSP paper；freqtrade | **不重复建设**；现有账本唯一 |
| 研究/Agent | Claude 顾问 + 辩论 | Vibe MCP；last30days；day-trader | **加工具，不加第二套决策** |
| UI | 单页 540 KB | TSP React；Vibe Web | **外链/独立页**，不嵌入、不合并前端 |

---

## 4. 落地方案

### 4.0 统一接入层：先定契约，再接项目

```
stock-advisor/
├── integrations/                 # 服务型：REST / MCP 适配器
│   ├── base.py                   # Contract/Capability：health()/call()/probe()/close()
│   ├── vibe.py                   # Vibe-Trading REST 适配（单例 httpx 客户端）
│   ├── tsp.py                    # tick-stock-panel REST 适配（按需，未实现）
│   └── contracts/
│       ├── vibe.json             # 仅记录“我们用到”的能力、必填字段、上游版本
│       └── tsp.json
├── skills/                       # 流程型：Skill 封装
│   ├── _template/                # 新建 skill 的模板
│   ├── selfcheck/                # 契约/隔离自检示例
│   ├── last30days/               # 按需
│   │   ├── SKILL.md
│   │   ├── skill.json            # 上游、版本范围、入口、输出 schema
│   │   └── run.py                # 只做调用 + 归一化，不复制上游逻辑
│   └── …
├── integration_api.py            # /api/integrations/*，只调 integrations/
├── skill_runtime.py / skill_api.py  # /api/skills/* 子进程运行时
└── static/index.html             # 「🧩 Skills」「🔬 研究」两个入口
```

适配器只暴露本项目定义的能力名，不把上游 URL 散落到业务代码里（实际实现）：

```python
class Integration:
    name: str
    contract: Contract          # 从 contracts/<name>.json 载入
    base_url: str

    def health(self) -> dict: ...          # 永不抛异常，返回 ok/error
    def capabilities(self) -> list[dict]: ...
    def call(self, capability: str, **params) -> dict: ...
    def probe(self, only=None) -> dict: ... # 逐个能力真调 + 校字段
```

`contracts/vibe.json` 的实际结构（节选）：

```json
{
  "integration": "vibe",
  "upstream": "HKUDS/Vibe-Trading",
  "tested_version": "0.1.18",
  "health": {
    "path": "/health",
    "required": {"status": "str", "service": "str"}
  },
  "capabilities": {
    "alpha.list": {
      "path": "/alpha/list",
      "query": ["zoo", "theme", "universe", "limit"],
      "required": {"status": "str", "alphas": "list", "total": "int"},
      "probe": {"limit": 3}
    },
    "alpha.bench.start": {
      "method": "POST",
      "path": "/alpha/bench",
      "required": {"status": "str", "job_id": "str"},
      "probe": false
    },
    "runs.list": {"path": "/runs", "query": ["limit"], "type": "list", "required": {}},
    "swarm.presets": {
      "path": "/swarm/presets",
      "type": "list",
      "required": {"0.name": "str", "0.title": "str"}
    }
  }
}
```

类型支持 `str/int/float/bool/list/dict/any`，后缀 `?` 表示可空；`allow_status` 声明
可接受的非 2xx（如 `/ready` 未配 LLM 时 503）；`probe:false` 表示有副作用、契约测试跳过。

升级判定不靠“看起来没坏”，靠**契约测试**：启动 sidecar → 逐个能力调用 → 校验必填字段/类型/状态码。任一失败就拒绝升级，保留旧版本。

### 4.1 方案 A（推荐）：Vibe-Trading 研究 sidecar

#### 4.1.1 进程与配置（2026-10-25 已部署到 101）

**线上形态**：独立 venv + systemd + nginx，不用 Docker（101 上已有 WeRSS、
LLM 网关、data-service、xq-bridge 等多个容器，再叠一层 Docker 只会更难排障）。

| 项 | 实际值 |
|---|---|
| 主机 | `101.43.25.101`（Ubuntu 24.04，4 核 / 3.6 GB / 40 GB 盘） |
| 安装目录 | `/opt/vibe-trading`（venv 约 1.1 GB） |
| 版本 | `vibe-trading-ai==0.1.18`（PyPI，腾讯云镜像，`--no-cache-dir`） |
| 监听 | `127.0.0.1:8899`（只有 nginx 能进） |
| 状态目录 | `/opt/vibe-trading/home`（`VIBE_TRADING_HOME`） |
| 配置 | `/opt/vibe-trading/.env`，`chmod 600` |
| 托管 | `vibe-trading.service`，`Restart=on-failure`、`MemoryHigh=1300M`、`MemoryMax=1800M` |
| 入口 | `http://101.43.25.101/vibe/`（nginx 80 端口，不新开安全组） |
| 鉴权 | 48 位 hex `API_AUTH_KEY`；`/alpha/*` 等业务路由必须 `Authorization: Bearer`，`/health` 公开 |

```bash
# 一次性安装（已在 101 执行）
python3 -m venv /opt/vibe-trading/venv
/opt/vibe-trading/venv/bin/pip install --no-cache-dir vibe-trading-ai==0.1.18

# 日常运维
systemctl status vibe-trading
journalctl -u vibe-trading -f
curl -H "Authorization: Bearer $VIBE_API_KEY" http://101.43.25.101/vibe/health
```

`/opt/vibe-trading/.env` 的关键项（值不写进仓库）：

```dotenv
API_AUTH_KEY=<48 hex>
VIBE_TRADING_HOME=/opt/vibe-trading/home
VIBE_TRADING_DISABLE_HTTP_PROXY=true
LANGCHAIN_PROVIDER=openai
LANGCHAIN_MODEL_NAME=<101 网关上已有的模型>
OPENAI_API_KEY=<复用 config.yaml 里的 llm.api_key>
OPENAI_BASE_URL=http://127.0.0.1:3000/v1
MARKET_DATA_ORDER_A_SHARE=tencent,eastmoney,baostock,akshare,tushare,gildata,local
```

- **LLM**：直接复用 101 上已有的 `:3000` 网关（OpenAI 兼容，实测 chat 200），
  所以 `/ready` 是 `ready`，agent/swarm 有模型可用；密钥不出 101。
- **A 股/港股数据链**：A 股默认顺序本来就是 tencent→eastmoney→baostock→akshare→tushare→
  gildata→local，这里显式钉住，防止上游改默认顺序；港股默认
  tencent→eastmoney→yahoo→futu→akshare→…→local，前两源免 token 已可用，暂不额外覆盖。
- **不装的东西**：不挂券商凭证、不开 shell 工具、不写 `sa_*`、不把 101 的 `.env`
  带进镜像或网站静态目录。

nginx 走 snippet，避免改坏 `sites-enabled/default` 里的旧注释：

```nginx
# /etc/nginx/snippets/vibe-proxy.conf
location /vibe/ {
    proxy_pass http://127.0.0.1:8899/;      # 去掉 /vibe 前缀
    proxy_http_version 1.1;
    proxy_set_header Authorization $http_authorization;  # 透传，由 sidecar 校验
    proxy_read_timeout 300s;
    proxy_buffering off;
}
```

`default` server 里只加一行 `include /etc/nginx/snippets/vibe-proxy.conf;`。
**不要**把 `API_AUTH_KEY` 写进 nginx 配置——鉴权在 sidecar 内完成，nginx 只透传。

本机 `.env`（仓库根，已 gitignore）：

```dotenv
VIBE_BASE_URL=http://101.43.25.101/vibe
VIBE_ALLOW_REMOTE=1
VIBE_API_KEY=<与 101 的 API_AUTH_KEY 相同>
```

`integrations/vibe.py` 启动时会把 `.env` 里的 `VIBE_*` 补进进程环境（只认这个
前缀，不碰 `DB_*`），所以不用改启动脚本；**改了 `.env` 要重启主应用**。

本机仍保留一套 venv 供离线调试，线上流量不走它：

```bash
cd D:\vibe-trading
set VIBE_TRADING_HOME=D:\vibe-trading\home
.venv\Scripts\vibe-trading.exe serve --host 127.0.0.1 --port 8899
```

**Docker 模式的坑**（若以后要容器化）：容器里的 `127.0.0.1` 是容器自己，
宿主绑回环的 sidecar 不会接受 Docker 网关连接。要么进同一 compose 网络，
要么监听容器可达地址并设 `API_AUTH_KEY` + `VIBE_ALLOW_REMOTE=1`。

#### 4.1.2 数据桥（决定成败的一步）

**不要**把 Vibe 直接指向生产表。先做一份“研究副本”：

| 项目 | 生产库现状 | 研究副本要求 |
|---|---|---|
| 价格 | 不复权（`jq_api.py` 明确说明） | 前复权（或后复权），跨分红/送转连续 |
| 成交量 | 手（`bt_data.py` 实测） | 股（×100），并在元数据写明单位 |
| 成交额 | `amount` 大量为 NULL | 用 `volume × 均价` 或源数据补齐 |
| 涨跌幅 | `pct` 大量为 NULL | 由复权收盘价重算 |
| 股票池 | 含退市/ST 需过滤 | 明确 universe（CSI300 / 自选 / 全市场）与 survivorship 口径 |

导出建议：

```text
research-data/vibe/bars/600519.SH.parquet   # DatetimeIndex + open/high/low/close/volume/amount
research-data/vibe/meta.json                # 复权方式、单位、导出时间、universe、缺口统计
```

- 先导 **CSI300 + 自选股、近 5 年**，控制体积（约数百 MB）；
- 用 `local:` 前缀显式指定标的，确保**不回退到网络源**；
- 按 Vibe 0.1.18 的 Data Bridge 文档配置列映射（README 只说明支持 CSV/Parquet/DuckDB，具体键名以该版本 `docs/` 为准）；
- 导完做 **20 个随机 (标的, 日期) 抽样对账** + **一次分红日前后的复权连续性检查** + **成交量单位检查**。

#### 4.1.3 应用接入（REST 适配器）与 Agent 接入（MCP）

**应用侧（2026-10-25 已落地）**。实际实现比示意更保守：契约驱动、默认只连回环、
失败不抛到业务。

| 文件 | 作用 |
|---|---|
| `stock-advisor/integrations/base.py` | 通用适配器：`Contract`/`Capability`、`health()`、`call()`、`probe()`；超时、4 MB 响应上限、`{code,message,detail}` 错误归一化 |
| `stock-advisor/integrations/vibe.py` | Vibe 专用：`VIBE_BASE_URL`（默认 `http://127.0.0.1:8899`）、`VIBE_API_KEY`、`VIBE_ALLOW_REMOTE=1` 才允非本机；进程内复用 httpx 客户端；启动时从仓库根 `.env` 只补 `VIBE_*` |
| `stock-advisor/integrations/contracts/vibe.json` | 只声明用到的 11 个能力 + 必填字段；`probe` 样本参数；`alpha.bench.start` 标记副作用跳过 |
| `stock-advisor/integration_api.py` | `/api/integrations/vibe/*` 门面，响应统一 `{ok,data,meta}`；**A 股/港股范围校验与模板过滤在这一层** |
| `stock-advisor/app.py` | `app.include_router(integration_api.router)` |
| `stock-advisor/static/index.html` | 「🔬 研究」页：引擎状态、LLM 诊断、zoo 计数、A 股/港股行情链、A 股 377 + 港股 180 因子浏览（公式+源码弹窗）、23 个通用模板、契约测试按钮 |

实际端点（读取为主，唯一写操作是启动 bench job，不碰交易）：

| 端点 | 代理上游 | 说明 |
|---|---|---|
| `GET /api/integrations` | — | 接入清单、开关状态、`market_scope=cn_hk`、`alpha_universes` |
| `GET /api/integrations/vibe/summary` | `/health` + `/ready` + `/alpha/readiness` + `/settings/data-sources` + `/swarm/presets` | 首屏聚合 + `scope` 块；health 不通时**短路**，不逐个重试；模板同样按 A 股/港股范围过滤 |
| `GET /api/integrations/vibe/health` | `/health` | 永不抛异常，返回 `ok/error` |
| `GET /api/integrations/vibe/capabilities` | — | 契约声明的能力与版本 |
| `POST /api/integrations/vibe/probe` | 逐个能力 | 真调一遍校验字段；`alpha.bench.start` 跳过 |
| `GET /api/integrations/vibe/alphas` | `/alpha/list` | **只收** `equity_cn`（别名 `csi300`，377 个）与 `equity_hk`（别名 `hk`，180 个）；其它 400 |
| `GET /api/integrations/vibe/alphas/{id}` | `/alpha/{id}` | 元数据 + 公式；**源码在顶层 `source_code`** |
| `GET /api/integrations/vibe/readiness` | `/alpha/readiness` | 只展示 `csi300`；当前 `tushare_token_missing` |
| `POST /api/integrations/vibe/bench` | `/alpha/bench` | **universe 只收 `csi300`**；港股/美股/加密基准直接 400（上游无港股基准域） |
| `GET /api/integrations/vibe/bench/{job_id}` | `/alpha/bench/{id}` | 轮询快照 |
| `GET /api/integrations/vibe/swarm-presets` | `/swarm/presets` | 上游 30 个 → 滤掉 7 个无关项，保留 23 个（A 股/港股共用） |
| `GET /api/integrations/vibe/skills` | `/skills` | 上游 90 个 skill 元数据，只读 |
| `GET /api/integrations/vibe/runs` | `/runs` | agent 未跑过时为空列表 |
| `GET /api/integrations/vibe/data-sources` | `/settings/data-sources` | 各市场 fallback 链与 token 状态 |

关键行为：

- **契约即测试**：升级上游先跑 `POST .../probe`，任一必填字段缺失/类型不符即判 `contract_violation`，不“看起来没坏”就升级。
- **错误分级**：连不上/401 → 503；超时 → 504；缺字段/路径不存在 → 502；参数非法 → 400。前端拿到的是 `{code,message}`，不会是 500 堆栈。
- **只接 A 股与港股**：因子市场 `equity_cn` / `equity_hk`（默认 A 股），bench 固定
  `csi300`（上游 0.1.18 的基准域只有 csi300/sp500/btc-usdt，没有港股基准）；模板拒绝名单是
  `crypto_*`、商品、信用、宏观利率/外汇、全球股票/配置共 7 个。要放开只改
  `integration_api.py` 的常量，不动上游。
- **远端必须显式授权**：非 loopback 必须 `VIBE_ALLOW_REMOTE=1`，且 token 只从
  `VIBE_API_KEY` 读（不回落本站 `API_AUTH_KEY`）。
- **可整体关闭**：`SA_VIBE_DISABLED=1` 时 `summary` 直接 409。
- **UI 不启动 swarm/bench**：只展示模板与因子；运行需要 sidecar 配 LLM 且消耗 token，留给命令行或后续授权。

**Agent 侧（未启用）**：Qoder / Claude Code 这类本身支持 MCP 的客户端可以直接连
`http://127.0.0.1:8900/mcp`，但只开放白名单工具：

- 允许：`alpha_zoo`、`alpha_bench`、`factor_analysis`、`backtest`（只读 run）、`get_market_data`；
- 禁止：券商下单、shell、文件写入、`paper_trade_step`；
- 每个工具设超时与返回大小上限，结果落 run 目录而不是塞进上下文。

现有 `llm_advisor.py` 是 Anthropic SDK 的直接调用，**并不天然具备 MCP 客户端**；
第一版继续走 REST 适配器。REST 与 MCP 共用同一个 sidecar 和同一份数据桥；区别只是
“后端确定性调用”还是“Agent 动态发现”。不要在业务代码里直接拼 MCP 调用，统一走适配器的 `call(capability)`。

#### 4.1.4 验收标准与实际结果

2026-10-25 在 101 实测（sidecar 0.1.18，systemd + nginx `/vibe/`）：

| 验收项 | 结果 |
|---|---|
| `GET /vibe/health`（公网） | 200，`service=Vibe-Trading API` |
| 鉴权 | 无 token 访问 `/vibe/alpha/list` → **401**；带 Bearer → 200 |
| `/ready` | **200 `ready`**（LLM 复用 101 本机网关，实测 chat completions 200） |
| 契约测试 | 9 个能力通过、2 个按设计跳过（bench start 有副作用、bench get 缺 job_id） |
| 因子库 | `equity_cn` **377** 个、`equity_hk` **180** 个；`limit=100` 正常截断 |
| `/alpha/{id}` | 元数据 + 公式 + 源码；源码在**顶层** `source_code` |
| `/alpha/readiness` | 只有 `csi300/sp500/btc-usdt` 三个基准域（无港股）；`csi300` 不可用（`tushare_token_missing`），UI 只展示这一项 |
| `/swarm/presets` | 上游 30 → 接口返回 **23**（`filtered_out=7`），A 股/港股共用 |
| 范围外参数 | `universe=equity_us`、`bench universe=sp500/hsi` → **400**，提示只支持 A 股/港股与 csi300 bench |
| 数据链 | `a_share` = tencent→eastmoney→baostock→akshare→tushare→gildata→local；`hk_equity` = tencent→eastmoney→yahoo→futu→akshare→…→local，前两源均无需 token |
| 港股行情实测 | Tencent loader 取 `00700.HK` 2025-09 共 **22 行** OHLCV；成交量单位是**股**（A 股是**手**，跨市场因子不能混算） |
| 资源占用 | 空闲 RSS 约 **400 MB**；cgroup 上限 1.3–1.8 GB |
| 启动耗时 | 101 上约 **35 秒**到 `/health`（境外源预检失败不阻塞） |
| 前端 | 「🔬 研究」页显示“已连接 / LLM 就绪 / A 股 + 港股 / 23 模板 / csi300 不可用”；市场下拉切港股后 `total=180`；搜索、详情弹窗（公式+源码）正常；无 console 报错 |
| 降级 | 本机改成死端口时 summary 约 2 秒返回未连接，全程无 500（沿用上一轮本机实测） |

**仍未通过的三项**：

1. `bench` 不可跑：`csi300` 需要 Tushare token（当前未配置）；`sp500`/`btc-usdt`
   以及**港股**已从接口层拒绝——上游 0.1.18 根本没有港股基准域，不是配置问题。
2. 与现有 `backtest.py` 的同窗口对账没做——需要先完成 4.1.2 的研究副本，
   否则复权/成交量单位/涨跌停口径不可比。
3. 101 磁盘只剩约 **2.9 GB**（venv 1.1 GB）。后续装研究数据副本前要先清盘或扩容；
   这是当前最紧的资源，不是内存。

行情链本身是通的：Vibe 要求 **`.SH`/`.SZ`/`.HK` 后缀**（`600519.SH`、`00700.HK`
走 Tencent；`600519` 会被错误路由到 Tushare），实测 2025-09 两者各返回 22 行 OHLCV；
注意 A 股成交量是**手**、港股是**股**，跨市场比较前要先统一单位。


#### 4.1.5 成本、风险、回滚

- **成本**：LLM token（研究会话）、CPU（alpha bench）、磁盘（venv 1.1 GB + 研究副本）。
  当前 UI 只读元数据，不产生 token；bench 未在 UI 开放，且 `csi300` 缺 token 跑不了。
- **资源红线**：101 磁盘只剩约 2.9 GB，是比内存更紧的约束；sidecar 空闲约 400 MB，
  已设 `MemoryMax=1800M`，超限只杀 Vibe 自己，不连累 WeRSS / LLM 网关 / data-service。
- **风险**：上游 API 漂移（用 `probe` 拦截）、alpha 在 A 股失效、数据口径错误、
  生成代码执行（bench 在 Vibe 的窄环境子进程里跑，不注入本站密钥）、
  `/vibe/` 若误配成无鉴权就是公开的 CPU 消耗入口（必须同时保留 `API_AUTH_KEY`
  与 Bearer 透传，改完 `nginx -t` 再 reload）。
- **升级**：`pip install --no-cache-dir -U vibe-trading-ai==<新版本>` → 改
  `contracts/vibe.json` 的 `tested_version` → 重启 → 跑 `probe` → 全通过才保留，
  否则回滚 venv 版本。**不要**只因为 `/health` 200 就认为升级成功。
- **回滚**：① `systemctl disable --now vibe-trading`；② 删
  `/etc/nginx/snippets/vibe-proxy.conf` 与 `default` 里那行 include，`nginx -t && systemctl reload nginx`；
  ③ 去掉本机 `.env` 的 `VIBE_*`；④ 代码层去掉
  `app.include_router(integration_api.router)` 一行，删 `integrations/`、
  `integration_api.py` 与「研究」页。数据库无迁移，对 `sa_*` 零写入，回滚成本接近零。
- **旁路开关**：不删代码也能关——`SA_VIBE_DISABLED=1` 后 `/api/integrations/vibe/*`
  统一返回 409，页面显示“已关闭”。

### 4.2 方案 B（按需）：tick-stock-panel 数据/指标 sidecar

**触发条件**（满足任意一条再上）：

- 需要分钟线/五档/盘中异动，而现有 DB 没有；
- 需要 68 列指标/信号或全市场因子排名；
- 需要一个可交互的本地量化工作台给非开发使用。

**接入方式**

```bash
docker run -d --name tsp -p 127.0.0.1:3018:3018 \
  -v "$PWD/tsp-data:/app/data" \
  ghcr.io/shy3130/tick-stock-panel:latest
```

- 在页面“设置 → 开放接口”创建 Token，只勾 `read:market`、`read:analysis`、`run:backtest`；
- 用 `/api/kline/daily` 与 `sa_market_kline` 做逐日对账；
- 不启用 `write:ext`、不启用 `paper:trade`、不公网暴露；
- 不要挂载宿主机的 `~/.codex`，除非确实要用它的 AI 助手。

**验收**：`600519.SH` 任取 5 个交易日，OHLCV 与现有库一致（成交量单位换算后）；不一致时先查 TickFlow/fuyao 口径，不直接改现有库。

### 4.3 方案 C：Skill 封装（流程型）

Skill 不是“复制提示词”，而是一个**可执行的薄封装**：`SKILL.md` 说明何时用，`skill.json` 声明上游与版本，`run.py` 调上游 CLI/API 并把结果归一化成固定 JSON。上游本体由使用者单独安装（pipx / uv tool / 插件市场），skill 目录里**不放上游源码**。

| 上游 | Skill 名 | 形态 | 做什么 | 不做什么 |
|---|---|---|---|---|
| `last30days-skill` | `last30days-research` | CLI 封装 | 调上游 CLI，输出近 30 天带链接的事件简报 | 不用于 A 股个股结论；不把摘要当事实 |
| `day-trader-skill` | `trade-checklist` | 流程模板 | 把 scan→news→entry→exit→replay→risk 变成 A 股可用的检查表 | 不跑美股日内、不接券商、不自动下单 |
| `daily_stock_analysis` | `strategy-taxonomy` | 知识资产 | 15 种策略的分类、提示词骨架与报告信息层级 | 不部署整套系统、不接它的推送 |

**`skill.json`（机器读，运行时唯一事实来源）**

```json
{
  "name": "last30days-research",
  "version": "0.1.0",
  "description": "检索最近 30 天的跨平台事件与舆情，返回带链接的证据简报",
  "entrypoint": "run.py",
  "enabled": true,
  "read_only": true,
  "timeout_seconds": 180,
  "max_output_bytes": 65536,
  "capabilities": ["research.last30days"],
  "env_allow": ["LAST30DAYS_API_KEY"],
  "requires": {
    "commands": ["npx"],
    "env": []
  },
  "upstream": {
    "name": "mvanhorn/last30days-skill",
    "version": "0.1.0",
    "url": "https://github.com/mvanhorn/last30days-skill",
    "commit": "<评估时的 commit>"
  },
  "params": {
    "topic": {"type": "string", "required": true, "description": "检索主题"}
  }
}
```

`env_allow` 是**逐个键**的明确授权，不支持 `*`；`DB_*` / `PG*` / `DATABASE_URL` 永远不传入。`entrypoint` 必须是 skill 目录内的 `.py`，目录名必须与 `name` 一致。

**`SKILL.md`（人和 Agent 读）**

```yaml
---
name: last30days-research
version: 0.1.0
description: 检索最近 30 天的跨平台事件与舆情，返回带链接的证据简报
---
```

**输出契约（固定字段，主应用只认这个）**

```json
{
  "ok": true,
  "upstream_version": "…",
  "data": {
    "summary": "…",
    "items": [
      {"title": "…", "url": "…", "source": "reddit", "engagement": 123}
    ]
  },
  "citations": ["https://…"],
  "warnings": []
}
```

**Skill 的五条硬规则**

1. 只读：不写 `sa_*`、不触发交易、不改配置。
2. Fail closed：上游缺失或版本不匹配时返回 `ok=false` + 安装提示，不偷偷降级成模型记忆。
3. 每条结论必须有 URL；没有链接的条目直接丢弃，不让 LLM 补。
4. 限制条数与输出大小（如 30 条 / 50 KB），防止把上下文塞爆。
5. 密钥只从环境变量读；skill 目录里不出现 token、cookie、券商凭证。

**已落地（2026-10-25）**

| 位置 | 作用 |
|---|---|
| `stock-advisor/skill_runtime.py` | manifest 严格校验、子进程隔离、超时/进程树终止、环境变量白名单、输出上限、JSON 契约校验、JSONL 审计 |
| `stock-advisor/skill_api.py` | `GET /api/skills`、`GET /api/skills/runs`、`POST /api/skills/reload`、`GET /api/skills/{name}`、`POST /api/skills/{name}/check`、`POST /api/skills/{name}/run` |
| `stock-advisor/skills/_template/` | 新 skill 的复制起点（`_` 开头不参与扫描） |
| `stock-advisor/skills/selfcheck/` | 可运行示例：验证参数、cwd、环境隔离、超时 |
| `static/index.html` → 🧩 Skills | 列表、运行、静态检查、说明、审计记录、结果/引用/警告展示 |
| `stock-advisor/data/skill_runs.jsonl` | 审计日志：只记元数据（名称/版本/耗时/错误码），不记参数值和输出；2 MB 轮转 |

运行时开关：`SA_SKILLS_DISABLED=1` 关闭全部执行；`SA_SKILLS_DIR=/path` 指向外部 skill 目录（便于把 skill 仓库单独 clone）。子进程不经过 shell，`cwd` 固定为 skill 目录，stdout 只接受 UTF-8 JSON。

### 4.4 明确不做

- **不做 vendoring**：Vibe（112 依赖）与 TSP（Docker/React）都通过 HTTP 交互；只有 TradingAgents 这种“已稳定且必须改造”的逻辑才保留代码移植。
- **不替换现有回测/模拟盘**：它们包含 A 股 T+1、涨跌停、停牌、12 项费用等本系统最宝贵的规则资产。
- **不引入第二个 UI**：现有单页 540 KB；研究页优先外链。
- **不接 Fincept / freqtrade**：许可与市场不匹配（见 2.9 / 2.10）。
- **不给 Agent 券商权限**：MCP 只开研究工具；真实下单永远不进自动链路。
- **不让 sidecar 写生产库**：研究结果只落独立目录，人工确认后再决定是否入库。

---

## 5. 路线图

| 阶段 | 交付物 | 验收门 | 预估 | 状态 |
|---|---|---|---|---|
| G0 决策与隔离 | 101 上独立 venv `/opt/vibe-trading` + systemd 单元 + 内存上限；不改动主环境依赖 | 独立进程能起；其余服务不受影响 | 0.5 天 | ✅ |
| G1 数据桥 | `research-data/vibe/` 的 Parquet + 元数据 | 20 行抽样对账、复权连续、成交量单位正确 | 1 天 | ⏳ |
| G2 Vibe 起服务 | 101 上 `:8899` REST（只回环）+ nginx `/vibe/` 反代；`/alpha/list` | Bearer 401/200 正确；A 股 377 + 港股 180 因子可见 | 0.5 天 | ✅ |
| G3 因子 bench | 10 只 A 股自选、3 年窗口的 `csi300` bench + run card | 与本地回测差异可归因 | 1–2 天 | ⏳（缺 Tushare token） |
| G4 接入 | `integrations/vibe.py` + `integration_api.py`（14 个端点，写操作仅本地 bench）+ 网页「🔬 研究」外链 | 零写库；错误可观测；超时/重试明确 | 1 天 | ✅ |
| G5 可选 TSP | 单容器 + 只读 Token + kline 对账 | OHLCV 一致；无公网暴露 | 1–2 天 | 按需 |
| G6 可选 Skill | `skills/last30days`、`skills/trade-checklist`、`skills/strategy-taxonomy` | 输出契约稳定；无链接条目被丢弃；fail closed | 0.5 天 | 按需 |
| G7 升级演练 | 契约测试脚本 + 一次“升一个版本再回滚” | 旧版本可回滚；页面无感；不污染主环境 | 0.5 天 | ⏳ |
| G8 因子桥 | `tools/factor_worker.py` + `factor_zoo.py` + `sa_hk_kline` 同步 + `/api/factors/*`；快照注入策略结论/日报/模拟盘 | A 股 357 算子、港股 162 算子各在自市场日线上可算；方向由样本内 IC 决定；两个面板不混算 | 1.5 天 | ✅ 2026-10-26 |

**P0 总计约 3–5 天**；TSP 与 Skill 按需追加。G0/G2/G4 已于 2026-10-25 在 101 上线（systemd `vibe-trading` + nginx `/vibe/`），本地应用经 `VIBE_BASE_URL=http://101.43.25.101/vibe` 接入，细节见 4.1。

---

## 6. 风险与验收口径

### 6.1 技术风险

| 风险 | 触发点 | 缓解 |
|---|---|---|
| 版本漂移 | Vibe 每日合并 PR | 锁 `0.1.18`；升级走分支；对 4 个端点做契约测试 |
| 依赖冲突 | Vibe 112 依赖 | 独立 venv/容器；绝不 `pip install` 进主环境 |
| 数据口径 | 不复权、手、`amount` NULL | 研究副本统一前复权/股/补齐成交额；写进 `meta.json` |
| 结果不可比 | 两套回测规则不同 | 现有引擎做裁判；差异逐项归因，不设“看起来差不多”的容忍 |
| 安全 | 生成代码子进程、上传、MCP | 容器隔离；无券商/LLM 密钥；shell 工具关闭；回环绑定 + Bearer |
| 成本 | LLM token、bench CPU、磁盘 | 先只跑 alpha list/bench；设预算与 run 清理策略 |
| 维护 | 两个 sidecar | 先只上 P0；TSP 必须由明确缺口触发 |

### 6.2 许可与合规

| 项目 | 许可结论 | 对本项目的影响 |
|---|---|---|
| Vibe / TSP / daily / last30days / day-trader / Kronos | MIT | 可用于个人项目；保留版权声明 |
| TradingAgents / OpenBB | Apache-2.0 | 可用；OpenBB 的 API 元数据为 NOASSERTION，使用前核对 LICENSE 与实际 provider 条款 |
| freqtrade | GPL-3.0 | 不 import；只能独立进程交互 |
| Fincept | AGPL-3.0 + 商业许可 | 商业/公司内部使用需付费；不作为依赖 |
| 数据源 | 各厂商条款 | TickFlow/fuyao/Tushare 等免费档有边界；不做数据再分发 |

> 本文不是法律意见；涉及对外分发或商业使用时，应由实际使用方核对许可证原文。

### 6.3 验收口径（建议写进后续 PR 描述）

- **可追溯**：每个研究结论能指到 run card / CSV / 原始数据行。
- **可复现**：同数据、同窗口、同参数，重跑结果一致；随机种子/版本固定。
- **可归因**：与现有回测的差异必须落到规则或数据，不接受“引擎不同”作为解释。
- **只读**：sidecar 对生产库零写入；所有写操作留在现有应用内。
- **可回滚**：移除 sidecar 不影响现有 256 个端点、定时任务与通知。

### 6.4 上游升级策略（让“开源持续更新”真正变成收益）

| 环节 | 做法 |
|---|---|
| **锁定基线** | Vibe `==0.1.18`；TSP 用 **image digest** 而不是 `latest`；Skill 记录 `tested_commit` |
| **订阅变更** | 关注 GitHub Releases / CHANGELOG / security advisory；不追每个 commit |
| **升级节奏** | 常规每月一次；安全修复 24 小时内评估；大版本单独开分支 |
| **升级流程** | bump 版本 → 跑契约测试 → smoke（alpha list / bench / kline 对账）→ 灰度一周 → 合并 |
| **失败处理** | 保留旧镜像/digest；契约测试失败即拒绝升级；无数据库迁移，回滚只换回 digest |
| **观测** | 适配器记录 `upstream_version`、延迟、错误率；页面显示版本徽标 |
| **禁止** | 生产环境不用 `latest`、不自动 pull、不在主环境 `pip install -U` 上游包 |

这样上游更新只在**适配器 + 契约测试**这一层产生工作量，主应用的业务代码和页面不需要跟着改；某个项目停止维护时，也只是少一个能力，不会拖垮主系统。

---

## 7. 证据附录

### 7.1 仓库元数据（2026-10-25，GitHub API）

| 项目 | Stars | Forks | 最近提交 | 语言 | 许可（API） | 仓库大小 |
|---|---:|---:|---|---|---|---:|
| Kronos | 43,666 | 7,212 | 2026-04-13 | Python | MIT | 9.5 MB |
| TradingAgents | 113,843 | 22,049 | 2026-10-16 | Python | Apache-2.0 | 6.4 MB |
| day-trader-skill | 1,246 | 283 | 2026-10-20 | Python | MIT | 414 KB |
| Vibe-Trading | 36,608 | 5,913 | 2026-10-25 | Python | MIT | 90.8 MB |
| last30days-skill | 65,372 | 5,674 | 2026-10-23 | Python | MIT | 53.9 MB |
| daily_stock_analysis | 67,028 | 55,621 | 2026-10-21 | Python | MIT | 119.7 MB |
| FinceptTerminal | 33,072 | 4,712 | 2026-10-13 | C++ | NOASSERTION（LICENSE 为 AGPL+商业） | 790 MB |
| freqtrade | 55,907 | 11,565 | 2026-10-25 | Python | GPL-3.0 | 831 MB |
| tick-stock-panel | 6,309 | 1,493 | 2026-10-24 | Python | MIT | 28.2 MB |
| OpenBB | 74,669 | 7,761 | 2026-10-24 | Python | NOASSERTION（LICENSE 为 Apache-2.0） | 2.4 GB |

### 7.2 包与镜像

| 对象 | 版本 / 结果 | 备注 |
|---|---|---|
| `vibe-trading-ai`（PyPI） | 0.1.18，Python ≥3.11，112 依赖 | 必须独立环境 |
| `openbb`（PyPI） | 5.0.0，Python ≥3.10，36 依赖 | 暂缓 |
| `freqtrade`（PyPI） | 2026.9，Python ≥3.11，85 依赖，GPLv3 | 不接 |
| `ghcr.io/shy3130/tick-stock-panel:latest` | `docker manifest inspect` 成功，含 amd64/arm64 | 可用 |

### 7.3 原文引用

- Vibe-Trading 的 `local` loader：`your own CSV / Parquet / DuckDB via local: prefix`；`An explicit local: symbol never silently falls back to a network source.`（[README](https://github.com/HKUDS/Vibe-Trading)）
- Vibe-Trading 的 A 股回退链：`tencent · eastmoney · baostock · akshare · tushare · gildata · local`。
- Vibe-Trading 的 alpha 与工具面：`478 pre-built alphas (Qlib 158 + Kakushadze 101 + GTJA 191 + academic + fundamental)`；`MCP tools exposed (82)`。
- Vibe-Trading 的 MCP 地址：`vibe-trading-mcp --transport http` → `http://127.0.0.1:8900/mcp`。
- TSP 的开放能力：`65 端点契约`、`12 个精选工具`、`paper:trade`；`TICKFLOW_API_KEY` 留空 = None 模式（历史日 K 免费）。
- daily_stock_analysis：`15 种内置策略`；行情源 `AkShare、Tushare、Pytdx、Baostock、YFinance、Longbridge`；推送 `企业微信/飞书/Telegram/Discord/Slack/邮箱`。
- last30days：`Reddit, HN, Polymarket, and GitHub work immediately`；`StockTwits` 在标的是股票/加密时自动启用。
- day-trader-skill：`11 Claude skills for day trading`；数据为 `Yahoo Finance charts, Nasdaq Trader's symbol lists, SEC EDGAR`；`paper trading by default`。
- TradingAgents：`China A-shares: Shanghai .SS, Shenzhen .SZ (e.g. 600519.SS for Kweichow Moutai)`。
- Fincept LICENSE：`AGPL-3.0 ... Free for personal use ...`；`Fincept Commercial License - REQUIRED for any business or internal company use, regardless of revenue, size, or duration.`
- OpenBB README：`ODP operates as the "connect once, consume everywhere" infrastructure layer ... Python ... OpenBB Workspace and Excel ... MCP servers ... REST APIs`。

### 7.4 未验证项（不要当成已证实）

1. 没有实际安装/运行 Vibe、TSP、OpenBB、Kronos；本文的接入结论基于 README、LICENSE、API 元数据、PyPI 元数据和 Docker manifest。
2. 没有跑过 Vibe 的 Alpha Zoo 在 A 股上的实际 IC/收益；478 个 alpha 的“数量”不等于“有效”。
3. 没有验证 Vibe `local:` 的完整配置键名与字段映射；落地时以 0.1.18 的 `docs/` 和实际 OpenAPI 为准。
4. 没有对 TSP 的 65/61 端点差异做源码核对；以 `/api/openapi.json?tier=a` 为准。
5. 没有验证 OpenBB 是否存在 A 股 provider；README 未提及，不等于一定没有。
6. 没有做法律审查；许可以 LICENSE 原文为准。
7. Stars/Forks 为抓取时点数据，会变化。

---

## 8. 落地补充（2026-10-26）：已验证算子接入日常决策（因子桥）

### 8.1 为什么要做

原先把 Vibe 的 alpha zoo 只当“研究目录”看（G2/G4），用户明确否决：**爬聚宽策略、接 Vibe 的目的就是拿到经过验证的算子，必须真的参与日常决策**。因此新增因子桥，把 registry 里**当前数据能算**的算子逐日算到本站日线上，而不是停在“能列出 377 个 ID”。随后用户明确“港股我也是必须的”，所以港股不再是“查不到就标 missing”：新增 `sa_hk_kline` 与 100 只流动性样本，A 股/港股各自成面板、各自定向，再合并进策略、报告与模拟盘。

### 8.2 结构与数据流

```
策略每日结论 / 盘前盘后报告 / 网页「🔬 研究」/ 模拟盘决策
        │  (deps 注入，失败只降级，不拖垮报告)
        ▼
factor_zoo.snapshot_all()        主应用（不 import 上游）；A 股/港股并行各一轮
  ├─ A 股：sa_market_kline → 600 只成交额池 ∪ 关注票 → COPY 成临时 CSV
  ├─ 港股：_sync_hk() 经 101 /kline 增量写入 sa_hk_kline（100 只流动性样本
  │        ∪ 自选/持仓/策略里的港股）→ 100 只面板 → COPY 成临时 CSV
  ├─ subprocess: D:\vibe-trading\.venv\python tools\factor_worker.py job.json
  │     └─ src.factors.registry.get_default_registry() 计算 + 样本内 IC
  │        （两个市场各起一个子进程，z/IC 绝不跨市场混算）
  ├─ _save()         sa_factor_run / sa_factor_alpha / sa_factor_code（JSONB）
  └─ attach_to_suggestions() / brief_lines() 合并两市场供策略与日报使用
```

- 主应用只读 `integrations/factor_catalog.json`（由 `tools/gen_factor_catalog.py` 从 registry 生成），**不 import 上游**，符合“锁版本 + 契约测试 + 不 vendoring”。
- worker 的输入/输出契约写在 `integrations/contracts/factors.json`；子进程失败、超时、JSON 缺字段都会返回 `{ok:false}`，日报与策略继续跑。
- 表：`sa_factor_run`（轮次，带 `universe`）、`sa_factor_alpha`（算子 IC）、`sa_factor_code`（逐票合成与 top 算子）；保留 `keep_days=90` 天。
- 港股日线**单独一张表 `sa_hk_kline`**，绝不混进 `sa_market_kline`：港股 `volume` 单位是**股**、金额是 HKD，混表后 A 股回测/因子池会把 5 位代码当 A 股标的选进来。
- 港股样本 `integrations/hk_universe.json`（100 只，恒指/恒科权重 + 本系统关注票，手工维护，不是官方成分股）；自选/持仓里的港股即使不在样本里也会被 `_sync_hk(extra_codes=...)` 同步。

### 8.3 实测数字（0.1.18，2026-10-26）

| 项 | 实测 |
|---|---|
| registry | A 股 `equity_cn` 377 个；可计算 **357** 个（20 个因 fundamental/valuation/sector 或列缺失跳过） |
| A 股面板 | 600 只 × 309 个交易日（2025-06-27 ~ 2026-09-30），206,218 行；最长 warmup 253 根（关注票多时池会扩到 670） |
| A 股定向 | 356 个算子样本内 IC 达标（\|IC\|≥0.015、\|IR\|≥0.15、≥120 天） |
| 耗时 | 双市场并行：A 股 worker 62–70s、港股 worker 3–4s；港股日线增量同步 100 只 / 24,299 行 / **26.1s** |
| 港股面板 | **100 只 × 244 个交易日**（2025-10-27 ~ 2026-10-23），`sa_hk_kline` 24,299 行 |
| 港股定向 | 162 个算子可算、162 个 IC 达标；置信按「参与/可算」比例（20% 强、10% 中），因为港股可算算子只有 A 股一半 |
| 样例·A 股 | 600519 合成 z **+0.56**（强，107/344 个算子）；反转 +0.95、情绪 -0.93、量能 +0.67 |
| 样例·港股 | 02513 合成 z **-1.90**（中，23/162）；质量 -3.00、动量 -2.24、微观结构 -1.40。195 根日线（2026-01-08 起） |
| 港股对照 | 00700 +0.11（中）、09988 -0.11（中）、01810 -0.24（中），同用 100 只样本 |

方向口径：每个算子先算“t 日因子值 vs t+1 日收益”的**横截面 rank IC**，按 IC 符号定向；合成 z 是定向后 z 的截断（±3）均值。**这是样本内统计，不是明日预测**，页面、日报、通知都保留这句话。

### 8.4 接入点

- 策略每日结论：`strategy_daily.run_all` 每轮只算一次 `snapshot_all`，`run_one` 给每条建议写 `factor = {status: aligned|against|neutral|missing, composite, confidence, n_used, market, market_label, as_of}`；`data_quality.factor` 存本轮汇总（`by_market` 分开统计 A 股/港股）。
- 日报：`daily_reports._build_context` 注入 `【因子暴露…】` 段；有港股时先列各市场面板/阈值，再按市场给逐票结论。prompt 明确要求“只描述风格位置 + 样本内 IC 方向，不得写成明日确定性预测”。
- 通知：有建议的轮次追加“因子同向 / 相悖 / 缺数据”统计与逐条标记；两市场都有时追加“A股 同向x/相悖y，港股 同向x/相悖y”。
- 模拟盘：`paper_trading.run_decisions` 每轮开始时算一次合并快照，传给每条决策的 LLM 上下文（`_factor_block`），块头会写“A股/港股数据截至 …”，所以真正决定买不买的那一步也看到了对应市场的因子位置。默认 `paper.factor_inject: true`；快照失败或单票缺数据时该段为空，决策照常进行。块里明确写“横截面风格位置，不是明日涨跌预测；相悖时必须说明理由，不能仅凭它改变仓位”。
- API：`/api/factors/status|catalog|latest|code/{code}|run|run/status`。`/latest` 不带 code 时合并两个市场（`markets` / `runs` / `codes`）；带 code 时按长度分流（5 位→港股，6 位→A 股）。`/code/02513` 只读港股快照，`/code/600519` 只读 A 股。
- 网页：「🔬 研究」顶部“因子每日快照”卡片分 **A股 / 港股两块**展示（各自面板、as_of、run_id、关注票、极性两端）；单票查询结果带市场标签；策略库明细的因子列显示 `z ±x · 港股`，汇总按市场分行。
- 实测（真快照 `fx-20261026022444-3343` + `fxh-20261026022420-9955`）：600519 渲染为“A股 +0.56（强，107/344）｜反转 +0.95、情绪 -0.93、量能 +0.67”；02513 渲染为“港股 -1.90（中，23/162）｜质量 -3.00、动量 -2.24、微观结构 -1.40”。UI 查 02513 返回 200；`/code/99999` 返回 404 并提示“最新 港股 快照里没有这只票”。快照失败、无该票、`missing` 三种降级路径均返回空串且不抛异常。

### 8.5 已知限制（不要当成已解决）

1. **A 股行情停更**：A 股快照 `as_of=2026-09-30`，页面显示“行情滞后 N 天”；港股快照 `as_of=2026-10-23`（101 同步到最近交易日）。因子方向随数据更新才会滚动，不是实时。
2. **港股覆盖是样本制**：只同步 `hk_universe.json` 的 100 只 + 自选/持仓/策略里的港股；窗口内少于 120 根日线的新股不进面板（02513 有 195 根，勉强达标）。不在样本且没人关注的港股查不到，不会硬凑。
3. **样本内**：IC 用的是同一段面板的历史，未做滚动样本外/交易成本检验；合成因子也还没有独立回测。现在它已作为策略与模拟盘决策的**第二意见**进入上下文，但“是否真的改善胜率”只能靠后续模拟盘结果检验，当前没有结论。
4. **上游拒绝的算子**：如 `gtja191_138` 被 registry 以“>95% NaN”拒绝，桥接层如实跳过，不伪造数值。
5. **成交量单位**：A 股 `volume` 是“手”，worker 按 `close×volume×100` 补 `amount`；港股 `volume` 已是股、`amount` 是 HKD，乘数固定 1。两个面板的 z 不可相加/排序。
6. **港股涨跌停/停牌口径**与 A 股不同，IC 定向只在各自市场内比较；不要拿港股 z 去解释 A 股建议。

### 8.6 对 7.4 的更新

- 第 2 条“没有跑过 Alpha Zoo 在 A 股上的实际 IC”**已部分推翻**：357 个算子已在 670 只 × 309 天上算出样本内 IC，并落库；但“有效”仍只指 IC 达标，不等于扣费后可交易。
- 第 3 条 `local:` 字段映射仍未验证；本桥接不使用 Vibe 的 loader，而是自己 COPY 面板，绕开了该未验证项。

---

## 9. 十项目实际运行与统一接入（2026-10-26）

不是“README 调研”，而是本机真实克隆、装依赖、起服务后的状态。统一探针：
`GET /api/projects`（30s 缓存，`?refresh=true` 强制重探）、`GET /api/projects/{id}`；
网页「🔬 研究」顶部「🧭 十项目接入矩阵」。探针只读，子进程只拿到
`PATH/SYSTEMROOT/TEMP/TMP/USERPROFILE/...`，**不传 `DB_*` 与任何 API key**。

### 9.1 运行矩阵（实测）

| 项目 | 接入方式 | 本机运行方式 | 状态 | 说明 |
|---|---|---|---|---|
| Vibe-Trading | REST sidecar | 101 systemd + nginx `/vibe/` | **live** | 0.1.18，`/health` 62ms；A 股 377 / 港股 180 因子；因子桥已接入策略/日报/模拟盘 |
| tick-stock-panel | REST sidecar | Docker 容器 `tsp` → `:3018` | **live** | 0.3.16；`/api/openapi.json`、`/api/kline/daily?symbol=600519.SH` 实测有数据；新契约 `integrations/contracts/tsp.json` |
| daily_stock_analysis | 本地 Web/API | venv → `:8000` | **live** | 前端已 `npm.cmd ci && npm.cmd run build` 预构建；启动必须 `WEBUI_AUTO_BUILD=false`（否则 `shutil.which("npm")` 在 Windows 返回无扩展名脚本，报 WinError 193） |
| OpenBB | REST / Python | venv `openbb-api` → `:6900` | **live** | `/` 与 `/openapi.json` 200；全球数据只读 |
| last30days-skill | Skill / CLI | 系统 Python | **ready** | `--preflight` 通过（Reddit/HN/Polymarket/GitHub） |
| day-trader-skill | Skill / CLI | 系统 Python | **ready** | 11 个 skill 的 scanner CLI 可调用；美股日内，仅研究 |
| Kronos | Notebook / 离线模型 | `venvs/kronos`（torch 2.14.1+cpu） | **ready** | 依赖装好；权重在 HuggingFace，本机网络不可达（`hf:000`），未下载、未跑推理 |
| freqtrade | 独立进程（不 import） | `venvs/freqtrade` | **ready** | 源码 2026.10-dev-150a10b / CCXT 4.5.90；GPL-3.0，只做加密，不参与 A 股/港股决策 |
| TradingAgents | 代码移植（冻结） | `venvs/tradingagents` + 本项目 `paper_trading/debate` | **ready** | 上游可 import；三件核心已移植进主应用 |
| FinceptTerminal | 桌面应用 | C++/Qt | **blocked** | 无服务 API；AGPL-3.0 且公司/商业使用需商业许可，不做依赖 |

汇总：**connected 9/10**（live 4 + ready 5），blocked 1。`connected = live + ready`，
`live` 是此刻有服务在监听，`ready` 是装好/可调用但不必常驻。

### 9.2 一键启动（本机）

`D:\cyz-integrations\start-all.cmd`（或 `start-all.ps1`）：按端口探测后启动
Docker `tsp`、daily `:8000`、OpenBB `:6900`、主应用 `:8686`；不改配置、不写库。
Vibe 在 101 常驻，不随本机重启。freqtrade/Kronos 不自动启动，避免无意义常驻。

### 9.3 边界（仍然成立）

- **不 vendoring**：除 TradingAgents 的已移植逻辑外，其余全部通过 REST / CLI / 独立进程交互；主应用不 import 上游。
- **不写生产库**：十项目探针零写入；TSP/daily/OpenBB 只读；freqtrade 不接。
- **错误只降级**：单个上游挂掉只让矩阵对应行变灰，行情/持仓/通知/模拟盘不受影响。
- **Kronos 未完成的部分**：权重下载与推理是唯一未闭环项；网络恢复后按 README 的
  `KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")` +
  `Kronos.from_pretrained("NeoQuasar/Kronos-small")` 即可，不需要改主应用。
- **Fincept 不做**：桌面 Qt + AGPL 商业条款，接入收益低于维护/合规成本。

### 9.4 新增接口

| 路径 | 作用 |
|---|---|
| `GET /api/projects` | 十项目矩阵 + `summary`（live/ready/offline/blocked/missing/connected） |
| `GET /api/projects/{id}` | 单项目重探 |
| `GET /api/integrations/tsp/health` | TSP 契约健康 |
| `GET /api/integrations/tsp/capabilities` | TSP 能力清单 |
| `GET /api/integrations/tsp/kline?symbol=600519.SH&limit=60` | 日 K + 指标（只读） |


