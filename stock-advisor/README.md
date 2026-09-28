# Stock Advisor · 股票跟踪分析助手

本地零成本方案：网页管理自选股和持仓 → Claude 定时搜新闻写分析报告 → 网页看报告。
另支持定时监听 B站UP主的最新动态+评论（调 MediaCrawler，零成本）。

> ⚠️ 所有报告仅供学习参考，不构成任何投资建议。

## 快速开始

```powershell
cd D:\correct_your_life\stock-advisor
python -m uvicorn app:app --host 127.0.0.1 --port 8686
# 打开 http://127.0.0.1:8686/
```

依赖：`pip install fastapi uvicorn requests`

## 使用流程

1. **添加自选股**：打开网页 → 「自选行情」→ 输入代码 → 添加。行情来自免费接口
   （A股走腾讯 qt.gtimg.cn，港股走新浪 rt_hk —— 腾讯对港股是 15 分钟延迟数据，
   2026-09-08 实测确认后换源），每 30 秒自动刷新，可加备注。
   - A股：6 位数字，如 `600519`
   - 港股：4-5 位数字，如 `02513`（智谱）或 `700`（腾讯控股，自动补齐 00700）
   - 行情列会显示币种（CNY/HKD），港股暂不支持盘后盈亏换算人民币
2. **填写持仓**：「我的持仓」→ 输入代码、股数、成本价 → 添加。自动算市值和盈亏。
3. **看报告**：「分析报告」→ 按日期查看。每份报告五段结构：
   - ① 新闻要点（带来源链接）
   - ② 情绪评分（-5 ~ +5）及理由
   - ③ 操作建议（结合持仓：买入/加仓/持有/减仓/观望 + 置信度）
   - ④ 关键风险
   - ⑤ 免责声明
4. **B站动态**：「📺 B站动态」→ 添加UP主 UID → 首次点「登录B站」扫码 → 「立即检查」。
   监听UP主的全部最新动态（文字/图文/视频帖）和评论，新动态高亮未读，点击UP主名展开评论。
5. **绑定通知**：「🔔 通知」→ 微信扫码登录（腾讯官方 iLink Bot）和/或配置邮箱（SMTP）→ 测试。
   之后 Claude 定时分析跑完会把报告摘要推到微信/邮箱。
6. **止盈/止损策略**：「🎯 止盈策略」→ 创建策略模板（与股票无关）→ 两种挂法：
   - **整仓挂**（推荐）：「我的持仓」→ 点开某只股票 → 「整仓策略」→ 选模板挂上，
     按整仓摊薄成本判定，触发状态（峰值/档位）独立记账；
   - **逐笔挂**：「记一笔」买入时引用模板，每笔单独跟踪、单独触发。

   内置 6 种常见量化退出策略：

   | 类型 | 规则 | 参数 |
   |---|---|---|
   | 固定止盈 | 现价 ≥ 成本×(1+涨幅%) | 涨幅% |
   | 回撤止盈 | 盈利超激活线后跟踪峰值，离峰值回撤超阈值触发 | 激活线%、回撤% |
   | 移动止盈 | 买入即跟踪峰值，回撤超阈值触发（无激活线） | 回撤% |
   | 止损 | 现价 ≤ 成本×(1-跌幅%) | 跌幅% |
   | 分批止盈 | 涨幅逐档到档通知（如 +5% 卖半仓、+10% 再卖三成），最后一档终结 | 档位数组 |
   | 时间止盈 | 持有满 N 个交易日通知复盘 | 交易日数 |

   交易时段（工作日 9:15–15:05）每 60s 扫描，触发即微信/邮件通知；峰值价与
   到档进度存在策略关联行上，重启不丢。`POST /api/strategies/check` 手动触发扫描。
7. **板块轮动**：「🔄 板块轮动」→ 看市场情绪（指数涨跌、全市场宽度、涨停/连板/炸板）
   和全市场板块的轮动评分榜，点板块行展开成分股。详见下节。
8. **提款计划**：「🏧 提款计划」→ 定一个「截止日期 + 目标金额」，系统对比当前持仓
   总市值给出达成路径。详见下节。
9. **财经日历**：「📅 财经日历」→ 未来 3 个月的非农/股指期货交割/四巫日（富时罗素·标普季调）/
   ETF 期权到期/LPR，FOMC·CPI 可手动补录；交易日盘前自动推微信提醒（今明事件一条 +
   重要事件提前 3 天预告，同键不重复推）。
10. **盈亏排序**：「我的持仓」支持「亏损最多在前 / 盈利最多在前」排序；
    总市值与所有盈亏统一人民币口径（港股按实时 HKD/CNY 折算，行内保留原 HKD 市值）。
11. **币圈 ↔ 股票关联**：「自选行情」页币圈卡片里给每个合约挂一只真实股票，自动算
    相关系数/beta/比价偏离。详见下节。
12. **公众号 / X 监控**：「📰 公众号」「🐦 X」页。详见下节。

## 财经日历（econ_calendar.py）

不依赖任何未验证的接口——最影响盘面的事件本身是日历规则，本地纯计算：

| 事件 | 规则 | 级别 |
|---|---|---|
| 美国非农就业 | 每月第 1 个周五 | ⚡ 重要（提前 3 天预告） |
| 四巫日 | 季月（3/6/9/12）第 3 个周五：美股期货期权到期 + **富时罗素/标普季调生效** | ⚡ 重要（提前 3 天预告） |
| 股指期货交割 | 非季月第 3 个周五（A股期现收敛，尾盘易波动） | 关注 |
| ETF 期权到期 | 每月第 4 个周五（2024-12 起由周三改周五） | 一般 |
| LPR 报价 | 每月 20 日（周末顺延）9:15 | 关注 |
| FOMC / 美国 CPI | 日期不固定，UI 手动添加（存 `sa_calendar_events`，也可让 Claude 查好写库） | 关注 |

**提醒**：`_calendar_loop` 交易日早 8–12 点每 5 分钟查一轮（按日期键去重，每天实际只推一次）：
今明两日事件合并推一条，high 级事件提前 3 天单独预告；去重状态存 `data/calendar_state.json`。
`POST /api/calendar/check` 手动跑。config.yaml `calendar.enabled` 可关提醒。

**API**：`GET /api/calendar?months=3`（合并视图）、`POST /api/calendar/events`（手动事件）、
`DELETE /api/calendar/events/{id}`。

## 提款计划（app.py withdrawal 模块）

回答「我在某日期前要提出 X 万，到底怎么办得到」：

- **市值够** → 难度显示「💰 现在就能提」，并按浮盈收益率降序给出**卖出凑钱建议**
  （卖哪只、卖多少股、回笼多少钱，优先兑现赚得多的）
- **市值不够** → 算出缺口、所需总收益率 `(目标/市值-1)`、剩余交易日、
  复合日收益率 `(目标/市值)^(1/T)-1`，按涨幅要求分级：
  🟢 轻松（≤10%）/ 🟡 正常（≤30%）/ 🟠 积极（≤100%）/ 🔴 风险极高（短期翻倍不现实，
  建议降目标、延日期或场外补本金）
- **进度跟踪**：「详情/记提款」记流水，进度条实时更新；提满自动置为已完成
- **每日提醒**：交易日 15:10 起半小时检查，达标/临近截止（剩 10、5、1 个交易日）/
  逾期等里程碑推微信，同类里程碑只推一次（config.yaml `withdrawal.enabled` 可关）

**API**：`GET/POST /api/plans`、`DELETE /api/plans/{id}`、
`POST /api/plans/{id}/withdrawals`（记提款）、`GET /api/plans/{id}/detail`、
`POST /api/plans/check`（手动跑提醒检查）。

**数据存云库**：`sa_withdrawal_plans`（计划）、`sa_withdrawals`（提款流水，级联删除）。

## 整仓级策略关联（sa_position_strategies）

「记一笔」只能逐笔引用策略，对老持仓补挂很繁琐。现在可直接给某只股票的
**当前整仓**挂策略（持仓页点开盘子 → 整仓策略）：按整仓摊薄成本判定，
6 种类型全支持，峰值/档位记在关联行上，触发同样推微信、只推一次。
已清仓的股票暂停判定（列表标「清仓暂停」），回补后自动继续跟踪。

**API**：`GET /api/position-strategies`、`POST /api/holdings/{code}/strategies`（挂）、
`DELETE /api/position-strategies/{id}`（解绑）。`GET /api/holdings` 每行附带
`position_strategies` 数组。

## 板块轮动监控（sector.py）

监控全市场板块（东财口径：行业 496 + 概念 504 + 地域 31），识别资金主线与轮动方向。

**数据源（全部免费公开接口，无需 key）**：
- `push2delay.eastmoney.com` 板块列表快照：涨幅/成交额/换手/主力净流入/涨跌家数/领涨股
- `push2ex.eastmoney.com` 涨停池：连板数、炸板次数、封单额、所属行业
- 板块成分股（`fs=b:BKxxxx`）点击展开时实时拉

**核心设计：自建历史**。东财板块历史 K 线域名（push2his）不可用（2026-09-09 实测连接被重置），
改为每天收盘后自动采集一次全量快照存云库，N 日动量从自己的库算——首个交易日只有当日榜，
积累 3 个交易日后动量/评分信号完整。

**盘中实时监控**（`_intraday_loop`，config.yaml `sector:` 段可配）：
- 交易时段（9:15–15:05，含集合竞价）每 5 分钟采样一次全量板块（内存缓存，不落库）
- 网页板块页「⏱️ 盘中实时榜」交易时段每 60s 自动轮询（涨幅 Top50 + 较上次采样变化箭头）
- 盘中预警推微信：急拉（采样间隔内涨幅跳升 ≥1.5 个点且 ≥3%）/ 板块涨停骤增（+3 家以上），
  同板块 10 分钟冷却防轰炸；`sector.alert_notify: false` 可关推送
- 收盘后另行采集正式快照入云库（供历史动量/评分/轮动预警）

**轮动评分**（0-100，找主线用）：
涨幅分（百分位×40）+ 主力资金分（正流入分位×30）+ 涨停分（板块涨停家数×4 封顶 20）+ 3 日动量分。

**市场情绪**：6 大指数涨跌、全市场宽度（涨/跌/平家数，收盘后快照统计）、涨停总数/最高连板/炸板次数。

**定时**：交易日收盘后（15:10 起）半小时检查一次，当日未采集则自动采（约 1 分钟，含全市场宽度扫描）；
`POST /api/sector/snapshot` 手动触发（可随时跑，整日覆盖式 upsert 幂等）。

**数据存云库**：

| 表 | 内容 |
|---|---|
| `sa_sector_snapshots` | 板块每日快照（按 snap_date+code 主键，覆盖式） |
| `sa_sector_daily` | 每日市场级数据（涨停聚合、宽度、指数） |

**API**：`GET /api/sector/overview`（总览）、`GET /api/sector/board/{BK代码}`（成分股）、
`GET /api/sector/digest`（给 Claude 定时报告引用的 markdown 摘要）。

## 币圈 ↔ 股票 关联统计（crypto_watch.py）

gate.io 上有一批「股票永续」（`TSLA_USDT`、`ZHIPU_USDT`…），它们 24h 连续交易，
比 A 股开盘早。本模块给每个合约挂一只**真实股票**，取两侧日K 按同一日期对齐后算：

| 指标 | 含义 |
|---|---|
| `corr` | 日涨跌相关系数（1 = 合约基本就是这只股票的影子） |
| `beta` | 合约涨 1% 时股票平均涨多少 |
| `vol_crypto` / `vol_stock` | 两侧日波动率，看谁更疯 |
| `ratio` | 比价 = 股票收盘 / 合约收盘 |
| `ratio_dev_pct` | 比价偏离其中位数的百分比 —— **最值得盯的异动信号** |

**2026-09-26 实测**（`link_lookback_days: 45`）：

```
ZHIPU_USDT   ↔ 02513 智谱    相关 0.85  beta 0.79  比价 7.785（中位 7.762，偏离 +0.30%）
MINIMAX_USDT ↔ 00100 MINIMAX  相关 0.88  beta 0.90  比价 7.852（中位 7.815，偏离 +0.47%）
TSLA_USDT    ↔ TSLA 特斯拉   相关 0.99  beta 1.03  比价 0.999（偏离 -0.09%）
```

港股那批的比价稳定在 **7.8 附近 —— 正是 USD/HKD 汇率**，说明永续价格是按汇率
折算后锚定港股股价的，不是「名字像而已」。比价突然偏离 = 合约被溢价/折价，或股票
除权跳空，超过 `link_alert_dev_pct`（默认 5%）会推微信。

**操作**：币圈卡片每行有「关联」列 → 点 `+ 设关联` 填代码 → 自动重算；点绿色
`相关 0.85` 徽标打开弹窗看**双线图**（两侧各自以首日=100 归一化后叠加，量纲差
7 倍不归一化看不出来）。

**日K 数据源**（2026-09-26 在本机逐个实测，按顺序降级）：

| 源 | 覆盖 | 备注 |
|---|---|---|
| 腾讯 `ifzq.gtimg.cn` | A股 / 港股 / 美股 | **首选**，两个镜像。⚠️ 别用 `web.ifzq.gtimg.cn`（被 WAF 拦成 501）；美股必须带交易所后缀（`usAAPL.OQ`），不带只返回 2 根 2011 年的陈旧数据且**不为空**，能静默通过下游守卫算出全错的相关性 |
| 东财 `push2his` | A股 / 港股 / 美股 | 一条 secid 打通，字段最干净；但本机 `*.eastmoney.com` 整域连不上 |
| 新浪 | A股、美股 | **没有港股日K**（`getDayK` 已下线） |
| 搜狐 `hisHq` | A股 | 会 503 限流，放最后 |

全失败时页面的关联列显示「无数据」并把每个源的失败原因挂在 title 上——能区分
「代码填错 / 源挂了 / 这只票停牌」三种情况，不会只留一片空白。

**API**：`GET /api/crypto/links`（表格数据）、`GET /api/crypto/links/{contract}`
（图形序列）、`POST /api/crypto/links/{contract}`（设/解除，`{"stock_code":"02513"}`，
空串解除）、`POST /api/crypto/links/refresh?contracts=A,B`（重算）。

⚠️ 路由顺序：`/api/crypto/links/refresh` 必须注册在 `/api/crypto/links/{contract}`
**之前**，否则 `refresh` 会被 `{contract}` 吃掉当成合约名然后报 422。

**表**：`sa_crypto_link_stats`（每合约每天一行，PK `(stat_date, contract)`，
自动清 90 天前）。

## 微信公众号监控（wechat_mp.py）

微信**没有任何官方接口**能读别人公众号的文章（官方 API 只管自己号），所以现成方案
都是「一个常驻服务负责爬，本项目只订阅它的产物」。本模块刻意**只依赖标准
RSS/Atom/JSON 源**，因此对上游是谁毫不在意。

### 上游选型（2026-09-26 调研）

| 方案 | 结论 |
|---|---|
| **WeRSS**（`rachelos/we-mp-rss`） | ✅ 推荐。MIT、4.7k star、Python 3.13 + SQLite 单容器、2026-09 仍在提交。支持微信读书渠道 |
| wewe-rss（`cooderl/wewe-rss`） | ⚠️ 2026-05-11 已归档只读；部分接口需经作者的中转服务 `weread.111965.xyz` |
| 公众号平台后台 API | ❌ 只能读自己运营的号 |
| 搜狗微信搜索 | ❌ 早已关停 |

WeRSS 的 RSS 路由（读 `apis/rss.py` 确认）**无需鉴权**即可拉：

```
GET /rss                     全部订阅源的 RSS（limit ≤30）
GET /rss/fresh               先让上游抓一轮再返回
GET /rss/{feed_id}           单个源，支持 ?ext=xml|json|md|txt、?limit=≤100
GET /rss/{feed_id}/fresh     单个源强制更新
GET /rss/{feed_id}/api       单个源 JSON
```

### 远程独立部署怎么接

> **服务已拆出去独立部署**：见仓库 [`mp-service/`](../mp-service/)（`Dockerfile` +
> `docker-compose.yml` + `.env.example` + 部署/加固/运维文档）。它要长期持有登录态 +
> Chromium，天生是独立服务端，可以单独扔到远程机器上，与本项目**不共享配置与数据**，
> 只通过 HTTP 拉 RSS。

`config.yaml` 的 `mp:` 段（网页「⚙️ 调度 → 🧩 完整配置」里也能改）：

```yaml
mp:
  base_url: "http://<服务器IP>:8001"   # 远程地址，配一次，网页添加源时自动预填
  auth: ""                            # 远程有反代鉴权时填：Basic <base64> / Bearer <token>
  use_fresh: false                    # 远程共享实例建议关（见下）
  interval_minutes: 30
  notify_new: true
```

- **地址**：`base_url` 配一次，网页「📰 公众号」页添加订阅源时地址栏自动带入。
- **鉴权**：`auth` 原样放进 `Authorization` 请求头，接口只回掩码，留空=不修改。
- **`use_fresh`**：`true` 走 `/rss/{id}/fresh`（先让 WeRSS 去上游抓一轮）；`false`
  走 `/rss/{id}` 吃它自己的缓存。**远程共享实例建议 false** —— 上游明确提示
  「添加订阅频率过高容易被封控」，而本项目默认 30 分钟一轮等于每 30 分钟催它爬一次。
  每个订阅源可单独覆盖（网页逐源勾选），「大多数吃缓存、个别走 fresh」可行。
- **不装 WeRSS 也能用**：订阅源类型选「任意 RSS 地址」，粘贴任何 RSS/Atom/JSON
  地址即可（wewe-rss 的 `/feeds/all.rss` 也走这条路）。

**本模块做的事**：按 `mp.interval_minutes` 拉取 → 解析（自动认 RSS 2.0 / Atom /
RSS 1.0 / 各种 JSON 形态，剥 HTML、实体解码、日期归一）→ 按 `(源, guid)` 去重入库
→ 新文推一条合并微信 → 未读高亮。`keep_days`（默认 120 天）自动清理。

**API**：`GET/POST /api/mp/sources`、`PATCH/DELETE /api/mp/sources/{id}`、
`GET /api/mp/articles`、`POST /api/mp/read`、`POST /api/mp/fetch`、
`GET /api/mp/status`、`GET /api/mp/digest`。

**表**：`sa_mp_sources`（订阅源，含 `use_fresh`/`auth` 逐源覆盖）、
`sa_mp_articles`（文章，`UNIQUE(source_id, guid)`）。

## X(Twitter) 指定用户发言监控（x_monitor.py）

### 上游选型（2026-09-26 调研）

| 方案 | 结论 |
|---|---|
| **twscrape**（`vladkens/twscrape`） | ✅ **唯一可用**。MIT、2.8k star、2026-09-22 仍在提交、PyPI v0.20.1 |
| Nitter | ❌ 2026-08-24 X Corp 发律师函要求永久下架，仓库 2026-09-11 归档只读 |
| snscrape / Twint | ❌ 依赖匿名 guest token，X 已关闭该通道，一并失效 |
| RSSHub `/twitter/user/:id` | ⚠️ 主体还活着，但 Twitter 路由是全项目最不稳的一批，且同样要 cookie |

### 装 & 配

```powershell
pip install "twscrape[curl]"     # [curl] 必须：X 做 TLS 指纹识别，httpx 指纹会被拒
```

网页「🐦 X」页 → 填 cookie。取法：**x.com → F12 → Application → Cookies** →
复制 `auth_token` 与 `ct0`，拼成 `auth_token=xxx; ct0=yyy`。
（上游也推荐 `unjar x.com -f header | twscrape add_cookie my_account` 一键导出。）

带这两个 cookie 的账号「立即激活，不需要 login_accounts 步骤」（上游 README 原话）。
账号 cookie 存云库 `sa_x_accounts`，接口返回时**只给掩码**（`auth_to…3f2a`），不吐原文。

**每个监听的人可单独设**：是否含回复（默认不含，噪音大）、点赞门槛（过滤低价值推文）。
`x.include_retweets` 控制是否收录转发（默认关）。

**API**：`GET /api/x/status`（含 `available`/`reason`，没装依赖时直接告诉你装什么）、
`GET/POST /api/x/accounts`、`POST /api/x/accounts/test`（校验 cookie 有效性）、
`GET/POST /api/x/watch`、`PATCH/DELETE /api/x/watch/{id}`、
`GET /api/x/tweets`、`POST /api/x/read`、`POST /api/x/fetch`。

**表**：`sa_x_accounts`、`sa_x_watch`（含 `user_id` 缓存，省掉每轮一次
`UserByScreenName` 请求）、`sa_x_tweets`。twscrape 自己的账号池在
`data/x_accounts.db`（compose 已把 `data/` 挂成卷）。

## 完整配置（config_schema.py）

**`config.yaml` 里每一个键都能在网页上改**，入口：网页「⚙️ 调度 → 🧩 完整配置」。

之前每个模块的配置是**手写**输入框，漏掉是必然的（`fetch_timeout_minutes`、
`alert_notify`、`paper.max_position_pct`、`alerts.days`、`mp.base_url`… 都曾经只能
手改 yaml）。现在改成：

```
config_schema.py            声明每个键：类型/范围/默认值/说明/是否敏感
   ↓ GET /api/config/all   后端按模式回值（模式默认值 → config.yaml 依次覆盖）
   ↓ 前端                  按模式**动态生成**表单（段折叠、两三列网格、高级项默认折叠）
   ↓ PUT /api/config/all   后端逐项 coerce + 范围校验 → conf_util 行级写回
```

当前 **15 段 83 项**。加新配置项只要在 `SCHEMAS` 里加一行，页面自动出现输入框，
不需要再手写控件 —— 所以不会漏。

| 段 | 项数 | 说明 |
|---|---|---|
| `news` | 12 | 4 个渠道各自的开关/间隔/超时 + 周期/条数/追加词 |
| `bili` / `wb` | 3 / 3 | 开关、周期、单轮爬取超时 |
| `mp` | 9 | 周期、新文通知、**`base_url` 远程地址**、**`auth` 鉴权**、**`use_fresh` 策略**、条数、保留天数、超时 |
| `x` | 8 | 周期、通知、是否收录转发、条数、保留天数、账号池等待、正文截断 |
| `crypto` | 10 | 抓价/告警 + 「币↔股」关联全套 |
| `sector` | 3 | 盘中采样周期、异动推送 |
| `alerts` / `calendar` / `volume` / `withdrawal` | 2/1/3/1 | 开关、提前天数、采样与推送 |
| `paper` | 5 | 模拟交易开关/初始资金/持有天数/单票上限/LLM 间隔 |
| `llm` | 5 | 开关、key、base_url、模型、自动建议 |
| `notify` | 7 | 微信开关 + 邮件 SMTP 全套 |
| `schedule` | 11 | 9 个定时任务的时间点 |

### 三条安全约定

1. **整批校验**：任何一项类型/范围不合法就整批拒绝（400），一项都不写 ——
   避免「写了一半、剩下还是旧的」这种半成品配置。
2. **只写变化的键**：`conf_util.set_nested_key` 是行级手术，兄弟键、注释、其它段
   一律原样保留。（它同时认**块状**与**流式** `baidu: {enabled: true}` 两种写法 ——
   `news.channels` 就是流式的，只认块状会写出重复 key，**整个 yaml 直接读不出来**。）
3. **敏感项不回传真值**：`llm.api_key`、`notify.email.auth_code`、`mp.auth`
   接口只给掩码；回传空串或掩码一律视为「不修改」，绝不会用掩码覆盖真值。

### 覆盖度是诚实的

`GET /api/config/all` 会返回 `unknown` —— `config.yaml` 里有、但模式未覆盖的键。
页面会把它列出来（不删除、只提示），这样「所有项都能改」这句话随时可核验。
当前是**空**（100% 覆盖）。

### 与专用入口并存

通用入口之外仍有几个顺手用的专用入口，写的是同一个 yaml、都走 `conf_util.WRITE_LOCK`：
`/api/schedule`（时间点 + 抓取周期）、`/api/llm/config`、`/api/notify/config`、
`/api/mp/sources`（订阅源）、`/api/crypto/links`（币↔股关联）。

> ⚠️ 抓取周期那栏的「新闻」写的是 `news.fetch_interval_minutes`（抓取循环真正读的
> 键）。原来这里写的是一个**没人读的幽灵键** `news.interval_minutes`，
> 「在调度页改新闻抓取周期」从来没生效过 —— 已修（`INTERVAL_KEY` 映射）。

## 通知模块（微信 iLink Bot + 邮件 SMTP，notifier.py + ilink_client.py）

微信走**腾讯微信团队官方的 iLink Bot API**（openclaw 微信渠道插件
`@tencent-weixin/openclaw-weixin` 同款协议，无需注册任何第三方推送平台）。
官方只发了 Node 插件没有 Python 包，`ilink_client.py` 按其线上协议用纯
Python 实现了登录、收发消息（协议细节见该文件头部注释）。

**微信（iLink Bot，扫码即用）**：

1. 网页「🔔 通知」→ 点「扫码登录微信」→ 手机微信扫码确认（首次可能要输入
   手机上显示的数字验证码）
2. 登录成功后**先在微信里随便给 bot 发一条消息**——iLink 是被动会话制，
   用户先发过消息，bot 之后才能主动推送
3. 点「发送测试消息」验证；启用渠道后 Claude 定时任务的报告自动推送

凭据（bot_token/baseurl 等）存云库 `sa_wx_ilink`，会话用户与 context_token
存 `sa_wx_users`。config.yaml `notify.wx.enabled` 控制是否启用该渠道。

**邮件（SMTP）**：填发送邮箱 + 授权码（QQ/163 在邮箱设置里开 SMTP 后生成，不是登录密码）
+ 接收邮箱。SMTP 服务器/端口留空时按发件邮箱后缀自动推断（qq/163/gmail/outlook 等预设）。

配置存 `config.yaml` 的 `notify:` 段（网页可改，`auth_code` 注意保管）。

**Claude 侧怎么发**（定时分析任务跑完推送报告摘要）：

```
curl -X POST http://127.0.0.1:8686/api/notify/send \
  -H "Content-Type: application/json" \
  -d '{"title": "盘后复盘 2026-09-06", "content": "**今日操作建议**\n..."}'
```

或命令行：`python notifier.py send "标题" "内容"`（按启用渠道广播，wx/email 独立成败）；
测试单渠道：`python notifier.py test wx` / `python notifier.py test email`。

## B站动态监听（MediaCrawler）

由本仓库根目录的 `MediaCrawler/`（uv 管理）提供爬取能力，stock-advisor 以子进程方式
调它的 CLI（`uv run main.py --platform bili --type creator ...`），跑完读 JSONL 结果入库。

**首次使用**：网页「📺 B站动态」→ 添加UP主（UID 或 space.bilibili.com 链接）→ 点「登录B站」，
弹出浏览器后用手机B站扫码，登录态保存在 `MediaCrawler/browser_data/`，之后抓取全自动 headless。

**定时**：`config.yaml` 的 `bili.interval_minutes`（默认 30 分钟，0=关闭），由 stock-advisor
后台线程执行，随服务常驻（与新闻抓取同机制）。

**数据存云库**：

| 表 | 内容 |
|---|---|
| `sa_bili_creators` | 监听的UP主列表 |
| `sa_bili_dynamics` | 动态（标题/正文/类型/发布时间/互动数/未读标记，dynamic_id 去重） |
| `sa_bili_comments` | 动态评论（按 dynamic_id 关联） |

**报告引用**：`GET /api/bili/digest?hours=24` 返回近 N 小时动态的 markdown 摘要，
可纳入盘前/盘后报告的 prompt。

**MediaCrawler 侧补丁**（相对上游，若更新 MediaCrawler 需重打）：
- `store/bilibili/__init__.py`：动态记录补充 `creator_uid`/`aid`/`bvid`/`title` 字段
- `media_platform/bilibili/client.py`：新增 `get_dynamic_comments`（图文/文字动态评论，type=17）
- `media_platform/bilibili/core.py`：`get_dynamics` 后 best-effort 抓每条动态的评论
- `config/bilibili_config.py`：`CREATOR_MODE=False`（走全部动态分支）、动态上限 30
- `config/base_config.py`：`ENABLE_CDP_MODE=False`（标准持久化上下文，headless 可复现）、评论上限 20

## 定时分析（Claude 会话内运行）

定时任务注册在 Claude Code 会话里（`CronCreate`），交易日自动执行：

- **08:23 盘前简报**：隔夜消息面 + 情绪预判
- **15:57 盘后复盘**：当日新闻 + 综合研判 + 操作建议

报告写入 `reports/<日期>/<代码>.md` 和 `reports/<日期>/daily-summary.md`。

**竞品动态专项调研**：自选股配了「自定义搜索词」（见下节）的，定时分析会额外做一轮
竞品调研——竞争对手近期新发布的产品/模型、定价与性价比对比、对标的股基本面的影响，
单独成段写进报告（snapshot 的 `competitor_watch` 字段是调研线索）。

**重要限制**：定时任务只在本 Claude Code 会话存活期间有效（最长 7 天）。
会话重开后，对 Claude 说 **"重启股票定时分析"**，它会读取云库配置自动重建任务。

## 新闻抓取（多渠道免费，可配置）

新闻不依赖任何付费 API，由服务自身按配置抓取（`news_fetcher.py`），网页「📰 新闻」页
可看、可手动抓取、可改设置（也直接改 `config.yaml`）：

| 渠道 | 内容 | 实测表现 |
|---|---|---|
| 东财 eastmoney | 个股公告 + 资讯搜索（纯 JSON） | 最快最稳，主力渠道 |
| 百度 baidu | 百度新闻垂直搜索（时效头条） | 快且准，反爬敏感已控频 |
| 新浪 sina | 财经滚动流按股名过滤 | 备用，命中率一般 |
| DuckDuckGo | DDG Lite 网页搜索 | 兜底，多为新闻聚合页 |

可配置项（`config.yaml` 或网页设置区）：
- `fetch_interval_minutes` 自动抓取周期（分钟，0=关闭，默认 60）
- 各渠道 `enabled` 开关与 `min_interval` 请求间隔（防反爬）
- `items_per_query` 每渠道取条数、`keywords_extra` 追加搜索词

**自定义搜索词（竞品动态监控）**：每只自选股可单独配搜索词
（`sa_watchlist.keywords`，网页「自选行情」添加时填或点「改」编辑）。
抓取时除按股票名搜外，还会按这些词对每个渠道各跑一轮，命中的新闻关联到该股——
典型用法：智谱（02513）配「Kimi,OpenAI,DeepSeek,新模型发布」，竞争对手发新模型、
降价等动态就会出现在智谱的新闻流和每日报告里。

数据存云库 `sa_news` 表（URL 去重），关联股票存 `sa_news_related` 表（一条新闻命中
多只自选股时，通过关联表挂到所有相关股票；按股筛选时会同时匹配主关联和交叉关联）。
网页新闻列表里每条新闻标题旁会显示关联股票徽标，点击可跳转筛选。

## 界面

单文件 `static/index.html`（无构建步骤，改完刷新即可）：

- **设计 token**：`--bg/--card/--text/--up/--down/--accent/--radius/--shadow…`
  全部集中在 `:root`，改一处全局生效
- **暗色模式**：页头 `🌓` 按钮三态循环（跟随系统 / 浅色 / 深色），存 `localStorage`；
  主题在 `<body>` 渲染前由内联脚本定好，不会闪白
- **吸顶导航**：页头 + 标签栏 `position: sticky`，标签可横向滚动，当前页自动滚进可视区
- **统一弹窗**：`openModal/confirmDlg/formDlg` 三个入口替代所有原生
  `confirm()`/`prompt()`。`formDlg` 支持多字段表单（原来改一条自选股要点 4 次
  prompt，输错一个就得从头来），且**校验不通过不关闭弹窗**，保留已输入内容；Esc 关闭
- **提示条**：可叠加（最多 3 条）、带 `ok/err` 语义色、错误停留更久
- **表格**：表头 sticky、数字列 `tabular-nums`（跳动时不会左右抖）、窄屏横向滚动
- **响应式**：≤640px 收紧留白与表格内距

## 数据存在哪里

**自选股和持仓存在云上 PostgreSQL**（复用仓库根目录 `.env` 里的 `DB_*` 配置，
与云上其他业务同一实例，表名用 `sa_` 前缀隔离）：

| 表 | 内容 |
|---|---|
| `sa_watchlist` | 自选股列表（网页增删） |
| `sa_holdings` | 持仓记录（网页增删） |
| `sa_news` | 抓取的新闻（URL 去重，供报告生成做素材） |
| `sa_wx_ilink` | 微信 iLink Bot 凭据（bot_token 等，KV） |
| `sa_wx_users` | 与 bot 建立会话的微信用户 + context_token |
| `sa_crypto_watch` | 币圈白名单（合约 + 关联股票代码） |
| `sa_crypto_link_stats` | 「币↔股」关联统计（每合约每天一行，PK `(stat_date, contract)`） |
| `sa_mp_sources` / `sa_mp_articles` | 公众号订阅源 / 文章（`UNIQUE(source_id, guid)`） |
| `sa_x_accounts` / `sa_x_watch` / `sa_x_tweets` | X 账号 cookie / 监听的人 / 推文 |

全部建表都是幂等 DDL（`CREATE TABLE IF NOT EXISTS` + 显式 `ALTER ... ADD COLUMN IF
NOT EXISTS`），服务启动时自动跑一遍，升级无需手工迁移。

**分析报告仍是本地 Markdown**：`reports/<日期>/*.md`（由后台线程 + Claude 生成）。

## 成本

¥0。行情用免费公开接口，新闻/币圈/公众号/X 全走免费或已开源免费通道，存储用云上
PostgreSQL（与已有业务共用实例）。唯一可能产生费用的是启用 Claude 的
`llm.enabled`（AI 提款建议 / 每日报告 / 模拟交易决策），关掉即零成本。
