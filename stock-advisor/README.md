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
6. **止盈/止损策略**：「🎯 止盈策略」→ 创建策略模板（与股票无关）→「我的持仓 → 记一笔」
   引用到买入笔。内置 6 种常见量化退出策略：

   | 类型 | 规则 | 参数 |
   |---|---|---|
   | 固定止盈 | 现价 ≥ 成本×(1+涨幅%) | 涨幅% |
   | 回撤止盈 | 盈利超激活线后跟踪峰值，离峰值回撤超阈值触发 | 激活线%、回撤% |
   | 移动止盈 | 买入即跟踪峰值，回撤超阈值触发（无激活线） | 回撤% |
   | 止损 | 现价 ≤ 成本×(1-跌幅%) | 跌幅% |
   | 分批止盈 | 涨幅逐档到档通知（如 +5% 卖半仓、+10% 再卖三成），最后一档终结 | 档位数组 |
   | 时间止盈 | 持有满 N 个交易日通知复盘 | 交易日数 |

   交易时段（工作日 9:15–15:05）每 60s 扫描，触发即微信/邮件通知；峰值价与
   到档进度存在交易行上，重启不丢。`POST /api/strategies/check` 手动触发扫描。
7. **板块轮动**：「🔄 板块轮动」→ 看市场情绪（指数涨跌、全市场宽度、涨停/连板/炸板）
   和全市场板块的轮动评分榜，点板块行展开成分股。详见下节。

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

数据存云库 `sa_news` 表（URL 去重），关联股票存 `sa_news_related` 表（一条新闻命中
多只自选股时，通过关联表挂到所有相关股票；按股筛选时会同时匹配主关联和交叉关联）。
网页新闻列表里每条新闻标题旁会显示关联股票徽标，点击可跳转筛选。

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

**分析报告仍是本地 Markdown**：`reports/<日期>/*.md`（由 Claude 定时任务生成）。

## 成本

¥0。行情用腾讯公开接口，新闻用 Claude 会话内置搜索，存储用本地 JSON 文件。
