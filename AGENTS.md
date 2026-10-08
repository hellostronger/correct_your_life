# AGENTS.md

本文件的规则对所有会话生效。与用户口头指示冲突时，以用户当次指示为准。

---

## 0. 发现 bug 自行修复，无需逐个确认（2026-10-04 用户授权）

**默认动作是「修」，不是「问」。** 发现明确的 bug 后直接修，不必先征求确认。

但「自行修复」有边界，越界的改动仍然要先问。判断标准是**可逆性 × 影响面**：

### 可以直接修，不用问

| 类别 | 例子 |
|---|---|
| **明确的逻辑错误** | 拼错符号、off-by-one、错误的变量名、null 未处理、异常吞掉 |
| **明确的资源泄漏 / 死锁** | 锁未释放、连接未关、线程未停 |
| **已验证的假失败** | 源其实活着但参数名/日期写错（见第 2 条各类「200 + data:null」） |
| **配置指向已失效的东西** | 模型 EOL、endpoint 404、证书过期（**改动要记进本文件**） |
| **测试/工具脚本的 bug** | 只影响临时脚本，不影响生产路径 |

### 必须先问

| 类别 | 为什么不能自己决定 |
|---|---|
| **删文件 / 删表 / 删数据** | 不可逆 |
| **改数据库 schema** | 影响面超出单次会话，且可能有其他消费者 |
| **改交易/钱的逻辑** | `paper_trading._execute_decision`、`paper_goal`、真实持仓口径 |
| **改风控阈值默认值** | 如 `stop_loss_max_pct`、`atr_stop_k`、`max_chase_pct20` |
| **大规模重构 / 删模块** | 超出「修 bug」范畴 |
| **碰用户 WIP** | `jq_sandbox.py`、`sandbox_runner.py`、`valuation_data.py`、`scripts/*.py`、`_dirty_backup_*.json`、`clade` |
| **加新依赖** | 影响 `requirements.txt` 和构建 |
| **推送 / 提交到远端** | 见下方「推送」一节 |

### 修完必须做的三件事

1. **验证**：写离线断言（可重复跑、不依赖网络/LLM），再跑一次端到端真请求。
   「改完看着对」不算修完 —— 参见第 2 条。
2. **记录**：把「症状 → 真因 → 修法」写进本文件对应章节。**没记录的修复等于没修**，
   下次会重新踩一遍。
3. **报告**：明确告诉用户改了什么、验证结果、以及**顺带发现的其它问题**
   （后者即使不在本次授权范围内，也要报出来让用户决定）。

### 推送

- **不自动 push。** 修完先在本地汇报，等用户明确说「提交 / 推送」再执行。
- 提交范围遵守既有约定：只提交本次相关的文件，不碰用户 WIP。
- `config.yaml` 含明文密钥且已 gitignore，**永远不要提交**；
  涉及配置变更时只提交 `config.example.yaml`。

### 长任务：每步落盘，格式化不许能弄丢数据（2026-10-04 血的教训）

**症状**：15 只持仓的决策（约 90 分钟、90+ 次 LLM 调用）**全部跑完**，
在最后写报告那一步崩了 —— `TypeError: unsupported format string passed to
NoneType.__format__`，成果**全部丢失**。真因是 `f"{r.get('price'):.2f"}`
而 `price` 是 `None`（算完价忘了塞进结果 dict）。

**两条规则**：

1. **超过 ~10 分钟或含外部调用的任务，每完成一个单元就落盘**
   （JSON checkpoint），并支持 `--resume` 复用已完成的部分。
   判据：「重跑一遍要花多少时间 / 多少钱」—— 90 分钟和 90 次 LLM 调用
   就必须能续跑。
2. **展示层（格式化/渲染）绝不该有能力弄丢数据**：
   - 所有数值格式化走一个 `num(v, spec, dash)` 兜底函数，`None`/非数值 → 破折号
   - 取值一律 `.get()` + 默认值，**不要 `d["key"]`**
   - 每条记录的渲染独立 `try/except`，一只票渲染失败只影响它自己

**离线验证渲染层**（不烧 LLM，可重复跑）：直接构造残缺数据喂给渲染函数 ——
「price 缺失 / 几乎全空 / final 缺 action / debate 缺字段 / cost 是 str」。
`%LOCALAPPDATA%\Temp\opencode\test_render.py`（11 项）。

> 这与第 2 条「区分 HTTP 200 和有数据」同源：**都要在「看起来成功」的地方
> 再确认一次**。那次是「源返回 200 但 data 为空」，这次是「LLM 全跑完但报告没写成」。

---

## 1. 优先用 GitHub 开源方案，不要自己造轮子

**这是硬性要求。** 动手写实现之前，先搜 GitHub 有没有现成方案。

具体做法：

1. **先搜，再写。** 接到「实现 X」类任务，第一步是搜 GitHub / PyPI，不是设计模块。
2. **搜索要分两轮**：
   - 第一轮找**库**（library）：`websearch` 搜功能 + 语言，例如
     `GitHub 开源 Python 板块 行业 资金流 akshare`
   - 第二轮找**可直接部署的服务**（含 Dockerfile / docker-compose）：关键词加 `docker 部署`
3. **优先官方出品。** 数据源类的库优先用官方或事实标准，例如 A 股数据
   优先 `akfamily/akshare`（及其 HTTP 化 `akfamily/aktools`），不要自己封装东财/新浪/同花顺的私有接口。
4. **注意 fork / 二级封装。** 例如 `1nchaos/adata` 是多源融合切换的方案，
   `akfamily/aktools` 是 akshare 的官方 HTTP API 封装 —— 这类项目比自己写采集器可靠。
5. **选型要给证据**：star 数、最近提交时间（是否还在维护）、License、是否支持
   Docker。用 `api.github.com` 取这些字段（`webfetch` 直连 github.com 会被拦，走 API）。

### 反例（本项目踩过的）

- ❌ 自己写 `requests` 调 `push2delay.eastmoney.com/api/qt/clist/get` 翻页采集板块
  → 写完当天可用，7 天后因请求量被 WAF 掐死，且没有降级，静默停更 9 天
- ✅ 应该一开始就查 akshare，它有 1000+ 接口，且**多数据源**（东财/同花顺/新浪）
  互为备份 —— 东财被掐时同花顺和新浪仍然可用

### 例外（可以自己写）

- 业务逻辑、评分算法、页面交互 —— 这些开源项目不会有
- 私有/内部系统的胶水代码
- 开源方案确实不满足时，**先说明为什么不能用**，再动手

---

## 2. 结论必须先验证，不要凭印象说

本项目已经因为「没测就下结论」返工过多次。硬性要求：

- 说「X 能用 / X 坏了」之前，**必须实际发过请求**并看到数据。
- **区分 HTTP 200 和有数据。** 已实测的三个例子：
  - 东财 `slist/get` 返回 200 但 `data:null`
  - `stock_zt_pool_em` 不带 `date` 返回 `200 []`，带 `?date=20260930` 才有 52 行
  - akshare `stock_board_concept_summary_ths` 返回 90 行「像行情」的数据，
    但 `日期` 列停在 **2026-07-31** —— 名字像快照，实际是概念新闻流，**静默给过期值**
- **验证「日期参数生效了」。** `stock_zt_pool_em?date=20261001`（假日）
  返回的集合与 `20260930` **完全相同**（52/52 交集）——不校验就会写进错标日期的历史。
- **区分进程内状态和持久状态。** 内存字典重启即空，不能用来判断数据新鲜度。
- **不把探测造成的副作用当结论。** 密集探测会把 IP 打进 WAF 封禁，
  导致「前后测出不同结果」——这时要说明是探测行为导致的变化，而不是当成服务端行为。
- **报错要读全文。** 日志里 `ProxyError('Unable to connect to proxy')`
  指向代理问题，和「IP 被封」是完全不同的根因。
- 说错了就明确说「我之前说错了」并给出更正，不要含糊带过。

---

## 3. 项目事实（省得每次重查）

| 项 | 值 |
|---|---|
| 仓库根 | `D:\correct_your_life` |
| 主应用 | `stock-advisor/`（FastAPI + 单文件原生 JS，无构建步骤） |
| 云库 | PostgreSQL `101.43.25.101:5432/dify`，user `jqeval`；表前缀 `sa_` |
| 连接配置 | 仓库根 `.env`（`DB_HOST/DB_PORT/DB_USERNAME/DB_PASSWORD/DB_DATABASE`） |
| `config.yaml` | **已 gitignore**，含明文密钥，不要提交 |
| 远端服务器 | `101.43.25.101`（腾讯云上海；跑着 WeRSS + LLM 网关 `:3000`） |
| SSH | `python stock-advisor/_remote.py <脚本.sh>`（base64 送脚本，绕开 PowerShell 编码破坏） |
| LLM 网关 | `http://101.43.25.101:3000`，非 Anthropic 官方，`base_url` 一设就走代理分支 |

### 网络环境（每次写网络代码都要注意）

- **本机有代理干扰**，必须设 `NO_PROXY=*` / `no_proxy=*`（`app.py:31` 就是为此）。
  不设的话 pip 和部分数据源会报 `ProxyError` / `Cannot connect to proxy`。
- 本机直连出口：**`59.34.155.130`**（2026-10-03 实测更正；AGENTS.md 原记的
  `121.32.254.148` 已失效，疑似运营商动态分配）。**每次做「换 IP 是否影响限流」
  这类实验前先实测出口**，别用文档里的旧值。无系统代理、无 VPN
  （注册表 `ProxyEnable=0`，常用代理端口无监听）。
- **测试脚本跑 akshare / pip 前也要设**，否则结果全是假失败。

### A 股数据源：11 个开源方案实测结论（2026-10-01，**优先用这个别重测**）

| 库 | 结论 | 决定性理由 |
|---|---|---|
| **akshare** 1.18.88 | ✅ **采用** | 同花顺族覆盖全：行业90(8字段)+概念387+资金流+指数历史。22.8k star，今天还在提交 |
| **aktools** 0.0.91 | ✅ **采用** | akshare 官方 HTTP 化，1083 接口，响应体统一 `list[dict]`，自带 Dockerfile |
| **adata** 2.9.5 | ⚠️ **仅取成分股** | 板块列表/资金流打的就是死掉的 `push2/clist`；无涨停池。但 `concept_constituent_ths(index_code=)` 可用 |
| **baostock** 0.9.4 | ⚠️ **仅当日K兜底** | 板块只有 84 个证监会行业，概念/地域服务端已废弃。日K 14 字段含 `turn` |
| efinance 0.5.9 | ❌ | 无板块列表 API；100% 东财；且硬编码 https |
| jqdatasdk 1.9.8 | ❌ | **根本没有板块行情 API**；token 鉴权服务端已废弃，`auth()` 要手机号 |
| tushare 1.4.29 | ❌ | 无 token；`moneyflow_ind_ths` 要 5000 积分 |
| easyquotation 0.7.7 | ❌ | 8 个源里无任何板块功能；日K 返回空数组 |
| qstock / pytdx | 见 RESULT 文件 | 通达信协议 |
| Ashare | 见 RESULT 文件 | |

**分工（实测验证过）**：

| 需求 | 用谁 | 备注 |
|---|---|---|
| 行业行情 8 字段 | `ak.stock_board_industry_summary_ths` | 3 请求 / 0.6s，涨跌幅+资金流+涨跌家数+领涨股全有 |
| 概念行情 | `ak.stock_fund_flow_concept` | 9 请求 / 1.1s，**缺成交额和涨跌家数拆分** |
| 行业成分股 | `adata.stock.info.concept_constituent_ths(index_code=<881xxx>)` | akshare 行业 code 直喂 adata，**6/6 通** |
| 概念成分股 | 同上但要 **886xxx** | akshare 概念 code 是 3xxxxx，**直喂 0/6 通**；桥要靠 `adata.stock.info.get_concept_ths(stock_code=)` 探针建 name→886xxx 映射 |
| 涨停池 | 项目自带 `sector.fetch_zt_pool()` | `push2ex` 仍可用，实测 52 家 |
| 板块动量 | `ak.stock_board_industry_index_ths` | 源直接给历史日线，**不用再攒 5 天快照** |
| 日K | `market_data` 腾讯兜底（已通） | 可再加 baostock 做第二兜底 |

**已验证能跑通**：用上述数据完整复现 `sector.py` 的轮动评分（涨幅分+资金分+涨停分+动量分四分量全有数据），
90 个行业涨跌幅/资金流/code 均 90/90。

**踩过的坑**：
- 同花顺三个子域 `q.` / `data.` / `d.` **同属一家**，轮询别只压一个。
- `stock_board_concept_summary_ths` 名字像行情快照，实际是**概念新闻流**，`日期` 停在 2026-07-31。当实时数据用会静默拿到过期值。
- `stock_zt_pool_em` **必须显式传 `date`**；且假日传 date 会返回上一交易日的集合（不校验就写进错标日期的历史）。
- `concept_constituent_ths(concept_code=...)` 参数名写错会**静默返回 0 行**，正确是 `index_code=`。
- `push2/ulist.np` 在 **http** 上稳定、**https** 上被 RST —— 用 http/https 结论会不同。
- adata 的 `all_concept_code_ths()` 的 `index_code` 列全是 NaN，别指望它给 886xxx。

### A 股数据源可用性（2026-09-30/10-01 实测，会变）

| 源 | 状态 | 备注 |
|---|---|---|
| `push2*.eastmoney.com`（clist/kline/ulist.np） | ❌ **全封** | 覆盖 `push2` `push2delay` `push2his` `17.push2` 等全部子域 |
| `push2ex.eastmoney.com` | ✅ **可用**（我一度误判为已封） | 涨停池通。**必须用 akshare 的 `stock_zt_pool_em`**，见下方「假失败」 |
| `datacenter-web.eastmoney.com` | ⚠️ **不可靠** | 实测 5 个函数只成 1 个，别当可靠源 |
| `q.10jqka.com.cn` / `data.` / `d.` | ✅ 可用 | 同花顺三子域，**同属一家，轮询别只压一个** |
| `vip.stock.finance.sina.com.cn` | ✅ 可用 | **有资金流**，见下方「新浪板块资金流」 |
| `proxy.finance.qq.com` / `qt.gtimg.cn` | ✅ 可用 | K 线 + 实时行情兜底源 |

**已封的根因（不是 IP 信誉问题）**：`_sector_intraday_loop` 交易时段每 5 分钟一轮，
每轮翻全量 1031 板块 = **12 个 clist 请求**，即 576 请求/日打同一个 host。
被封后**不会停**，继续每 5 分钟捶 12 次，永久续期 —— 所以它永远不会自己恢复。

### ⚠️ 最阴的一个「假失败」：HTTP 200 + 参数名写错 = 看起来像被封

2026-10-01 我误判「push2ex 已封」，写进了 AGENTS.md 和提交信息，**是错的**。
真因是我手写 requests 时**参数名写错了**：

```
我传的:    ?d=20261001&ut=fa5fd1943c7b386f172d6893dbfba10b
正确的:    ?date=20261001&ut=7eea3edcaed734bea9cbfc24409ed989
                    ^^^^^^          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                    是 date 不是 d    这是 zt 池专用 token，不是 push2 clist 的
```

用 `d=` 会命中一个**仍然存在但语义不同的旧接口**，返回
**HTTP 200 + `{"rc":102,"rt":1,"data":null}`** —— 状态码正常、有 JSON、
`data` 为空。任何「检查状态码」「检查返回非空 JSON」的逻辑都会认为
「源活着但今天没数据」→ 误判成被封。

**教训**：
1. 私有接口不要手写封装。`ak.stock_zt_pool_em` 的参数是现成的、已验证的。
2. 「某源被封」这个结论**必须有 `ConnectionError`/`403`/`timeout` 之类
   明确的失败证据**。`200 + data:null` 只能说明「参数或日期不对」，
   因为同期 `ak.stock_zt_pool_em(date="20260930")` 同一个 host 返回 52 行。

### ⚠️ 交易日/休市：三处必查（A股长假会让数据整体错位）

2026-10-01 国庆实测，三个坑叠在一起：

1. **push2ex 忽略 `date` 参数，固定返回最新交易日**
   ```
   date=20261001 -> 52 条, qdate=20260930
   date=20260930 -> 52 条, qdate=20260930   （与上行 52/52 完全相同）
   date=20260929 -> 57 条, qdate=20260930   （传 0929 却给 0930 的数据！）
   ```
   → **只有响应里的 `qdate` 可信**，自己传进去的日期一律不信。
   akshare 会把 `qdate` 丢掉（只保留重命名后的业务列），需要就读原始接口。

2. **levistock 休市日返回 0**
   `sector_ranking_kph(date="2026-10-01")` 三个类别全 0，
   `date="2026-09-30"` 有 405 条。必须回溯，否则误判成源挂。

3. **入库日期必须用「真实数据日」**
   休市日采集到的是上一交易日数据，用 `date.today()` 入库会在
   `sa_sector_snapshots` 写进**错标日期**的行（AGENTS.md 记过这个坑）。
   `data_service` 的 `/boards` 和 `/health` 都返回
   `is_trading_day` + `data_date`，调用方按 `data_date` 入库。
   交易日历用 `ak.tool_trade_date_hist_sina()`；
   **日历不可用时 `is_trading_day` 返回 `None` 而非 `False`** ——
   分不清「休市」和「日历挂了」时绝不能当休市，否则交易时段会跳过采集。

### ✅ 日历源不可用时会真实成交 —— 已修（2026-10-04）

`is_trading()` 把 `None` 折叠成「周一~周五」（设计意图：采集路径宁可跑
不可静默跳过，见上）。但**对交易路径的后果比采集严重**：`_paper_loop`
的假日闸门只有 `is_trading()` 这一层，`market_session_state()` 只判时段
不判假日。日历源（gov.cn 公告抓取）间歇性失败时，折叠把假日变成交易日
→ **真实成交**。

**实证**：`sa_paper_trades` 里 4 笔买入的 `trade_date = 2026-09-25`（中秋，
`is_trading_day(09-25)=False`，5 个独立进程复核一致）——
003035/513980/600121/600406 在假日以（可能 stale 的）价格成交。
`sa_paper_cycles` 还有 `2026-10-01 15:35 catchup`（国庆）一轮，
`planned=18, acted=0`（`can_trade` 的行情闸门挡住了成交，但轮次照跑）。

**修法**：`paper_trading.calendar_gate(td, allow_when_unknown)` 三态闸门 ——
交易轮次**仅 True 放行**（未知=不安全，绝不下单）；结算/快照 **True/None
都放行**（幂等、只读为主）。`_paper_loop` 外层闸门用 `is_trading_day()`
三态 + `calendar_gate(..., True)`，两个交易轮次各加
`calendar_gate(..., False)`。采集路径（data_service 等）的 `is_trading()`
折叠行为**不变** —— 那里最多用到陈旧数据，状态字段会标出来。

**同源修复（全部交易/信号路径）**：
- `_paper_loop`（app.py:5097）—— 盘中轮次 + 兜底补跑，真实成交
- `_strategy_loop`（app.py:2127）—— 真实策略止盈监控，标记
  `strategy_triggered_at` + 发通知（不执行成交，但假日 stale 价触发信号
  会误导后续真实卖出）
- `/api/paper/run` 手动触发（app.py:3799）—— 用户手动一轮决策

三处统一改法：交易/信号路径要求 `is_trading_day() is True`（明确交易日），
`None`（日历未知）时跳过本轮 —— 守护线程 60s/5min 内自然补上，
手动触发直接拒绝并说明原因。

**未改（设计意图覆盖）**：
- `_paper_strategy_loop`（影子卖出扫描）—— docstring 明确「策略探索不该
  依赖交易时段」，且 `_record` 只写 `sa_paper_strategy_exits`，不碰
  `sa_paper_trades`、不动持仓、不改现金
- 通知/采集类循环（提款、财经日历、公告、报告、宏观）—— 维持
  「宁可跑不可静默跳过」，stale 数据会标状态字段
- `in_trading_session`（app.py:1100）—— 无调用者的死代码，不动

测试：`%LOCALAPPDATA%\Temp\opencode\test_cal_gate.py`（17 项，含
「None 时交易停、结算继续」「is_trading 折叠逻辑未改」回归）。

### 卖出结算必崩：`_settle_buy_rows` 拿 tuple 当 dict 用（2026-10-08 修）

**症状**：`sa_paper_trades` 的**卖出成交永远不落库**。日志反复：

```
File "paper_trading.py", line 1671, in _execute_decision
    _settle_buy_rows(cur, code, shares, price, today, ...)
File "paper_trading.py", line 1808, in _settle_buy_rows
    bought = int(r["shares"] or 0)
TypeError: tuple indices must be integers or slices, not str
```

路径是 `check_stop_loss` → `_execute_decision` → `_settle_buy_rows`。

**真因：游标类型和取字段的方式对不上。**

| 位置 | 写法 | 行是什么 |
|---|---|---|
| `_execute_decision:1500` | `cur = conn.cursor()` | **tuple** |
| `_settle_buy_rows:1808` | `r["shares"]` | 当 dict 用 → **TypeError** |

同一文件里 `_account_row` / `_derive_paper_positions` 都写了
`isinstance(row, dict)` 分支来兼容两种游标，**唯独 `_settle_buy_rows` 漏了**。

**影响面**：只要该股存在 `status='open'` 的买入行，`rows` 非空就进循环、
必抛异常 → 事务回滚 → **止损卖单、决策卖单全部落不了库**。
（`rows` 为空时循环不进反而不炸，所以「有时能卖出」纯属运气。）

**修法**：加 `_SETTLE_BUY_COLS` 模块常量 + 循环开头一行归一化，
照抄本文件已有的兼容写法：

```python
if not isinstance(r, dict):
    r = dict(zip(_SETTLE_BUY_COLS, r))
```

**并把 SELECT 改成由 `_SETTLE_BUY_COLS` 生成**：
`"SELECT " + ", ".join(_SETTLE_BUY_COLS) + " FROM ..."`。
这样**列名和列顺序不可能再漂移** —— 漂移的后果是**静默算错份额和收益率**
（tuple 按位置取，不报错），比崩溃危险得多。

**未改 `_execute_decision` 的游标**（另一个方案是把它换成 `RealDictCursor`）：
`position_limit_status` / `sellable_shares` / `_alpha_for_round` 三个 helper
还没审过，风险面更大。改动只在读行，不碰任何金额/份额/手续费口径。

**验证**：`%LOCALAPPDATA%\Temp\opencode\test_settle_cursor.py`（**14 项**，全
FakeCursor、**不碰生产库**），最强的一条是**parity**：
tuple 模式与 dict 模式必须产出**逐字节相同的 SQL 与参数**，
否则「修好了」也可能悄悄改了金额。
回归：`test_debate_persist` 54 / `test_trunc` / `test_cal_gate` / `test_rc_veto`
**全部通过，零回归**。

> 写断言时我错了两次，都是**断言写错不是代码有洞**：
> ① 以为部分平仓 `closed` 返回 0 —— 实际 line 1849 两个分支都 `closed += 1`，
>    它统计的是**被结算的行数**不是「整笔核销数」；
> ② 参数下标取错，`shares` 是 `[5]`、`value` 才是 `[6]`，我拿 `[6]` 断言 shares
>    得到 4000.0，差点以为代码算错了。
> → 断言的预期必须**独立于被测代码**推导，别照抄实现（下标就是照抄的）。

### ⚠️ 部分平仓永远不可能成功：remainder 行撞唯一索引（2026-10-08 查明，未修）

**上一条的姊妹 bug，修好 TypeError 之后才暴露出来。**

`sa_paper_trades` 上有唯一索引：

```
sa_paper_trades_trade_date_slot_code_side_key
  UNIQUE (trade_date, slot, code, side)
```

`_settle_buy_rows` 的**部分平仓**分支把 `trade_date`/`slot` **原样复制**：

```sql
INSERT INTO sa_paper_trades (code, name, trade_date, slot, side, shares, ...)
SELECT code, name, trade_date, slot, 'buy', %s, price, %s, ...
  FROM sa_paper_trades WHERE id=%s
```

→ remainder 行与被拆的那行 **(trade_date, slot, code, side) 完全相同**，
且 INSERT 发生在把原行改成 `resolved` 的 UPDATE **之前**（line 1832 vs 1841），
所以**必然** `UniqueViolation`。实测：

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
  "sa_paper_trades_trade_date_slot_code_side_key"
DETAIL: Key (trade_date, slot, code, side)=(2026-09-25, , 600406, buy) already exists.
```

**与游标 bug 的关系**：修之前部分平仓报 TypeError，修之后报 UniqueViolation ——
**两个都得修才能走通**。整笔平仓只 UPDATE 不 INSERT，**不受影响**（已实测通过）。

**为什么不能自己修**：要让 remainder 行不撞索引，就得改 `slot`（或置 NULL ——
Postgres 唯一索引里 NULL 之间不冲突，而**空串 `''` 会冲突**，库里 buy 行有
**9 行 `slot=''`、0 行 NULL**，600406 的 id=118 就是 `slot=''`）。
但 `slot` 同时是 `_execute_decision` 的**幂等守卫**字段：

```sql
WHERE trade_date=%s AND slot=%s AND code=%s AND status<>'skipped'
```

改动会影响「本轮是否已决策过」的判定和按轮次分组的报表口径
（`sa_paper_cycles` / rounds）。**这属于交易口径，必须先问用户**，没动。

**验证脚本**：`test_settle_realdb.py`（6 项，真连生产库、**自己 rollback
并回查计数**）。它先试 1 股 → 撞 UniqueViolation；改成卖出**恰好等于最老
未平仓行**的股数（整笔平仓）→ 通过，且 `18→18 行 / 600→600 股 / 3→3 open`
证明回滚真的生效（AGENTS.md：回滚了不等于没写进去）。

### 启动本地服务：只有一种起法能活下来（2026-10-08 实测）

用户自己的 `run_local.py`（`%LOCALAPPDATA%\Temp\opencode\run_local.py`，
UTF-8 重定向日志 + `NO_PROXY=*` + 固定 cwd + 剔掉会遮蔽真 `click` 包的
`sys.path[0]`）是对的，**别改**。但「怎么把它启动起来」有坑：

| 方式 | 结果 |
|---|---|
| harness `background:true` + `python -m uvicorn` | 跑 43 分钟后 `Exited with code 1`，**harness 只报 exit code，进程已死** |
| `Start-Process -WindowStyle Hidden` | 瞬死，**连 `=== launcher start ===` 都没写出来** |
| `Invoke-CimMethod Win32_Process Create`（WMI） | 同上，瞬死 |
| `schtasks /Create` + `/Run` | 同上，瞬死（任务本身创建成功，但进程一样没起来） |
| **`Start-Process -RedirectStandardOutput/-Error -NoNewWindow` + 同一条命令里轮询端口** | ✅ **活着，端口在听** |

**判据仍然是 AGENTS.md 那条**：光看「8686 在监听」不够，要核对
**监听 PID 的 CreationDate 晚于被改文件的 mtime**。

⚠️ 我因此犯了个错：为了上修复**先 kill 了用户 15:02:48 起的进程**，
结果四种起法全失败、**服务停了 20 分钟**才找到能活的那一种。
**正确顺序是先把能存活的起法验证通过，再动旧进程。**

### 🔴 新闻时效：抓取时间 ≠ 发布时间（2026-10-08 用户定规矩，已修）

**症状**：10-08 的每日总结里，**莲花控股 600186** 写着
「每经/证券时报揭露投资标的阶跃星辰大额亏损 | -4 基本面造假嫌疑 | 观望80%」，
用户指出**那是 6 月的新闻**。

**真因（三层，缺一不可）**：

**① 下游拿 `fetched_at` 顶替缺失的 `publish_time`。**
`app.py` 三处查询都是 `COALESCE(n.publish_time, n.fetched_at) > now() - interval 'N days'`。
`fetched_at` 是「**我们**什么时候抓到的」，不是「新闻什么时候发的」。
百度会持续把 6 月的旧百家号页返在搜索结果里 → 10-01 抓到 →
**10-08 的报告里就成了「近 7 日新闻」**。这是最关键的一条。

**② 入库层主动放行无时间的条目。** `save_to_db` 原来写着
「publish_time 为 None 的（如 SearXNG/bing）不过滤 —— 宁可保留
（**下游可以按 fetched_at 过滤**）」。那句注释就是根因的说明书。
同一段还有个洞：`except (ValueError, TypeError): pass  # 时间解析失败不过滤`。

**③ 时间解析器只认到「天」。** `_cn_relative_to_iso` 支持
`分钟前/小时前/天前`，**不认 `周前/月前/年前`**。一篇 6 月的旧闻，
百度标「4个月前」→ 解析返回 `None` → 落成 NULL → 走 ①。
实测该缺口 6 种表述全部返回 None（`test_time_parse.py` 修前 `GAP_CONFIRMED`）。

**顺带查出两个独立的真 bug（都是「安静地错」类型）**：

| bug | 后果 |
|---|---|
| `ORDER BY n.url, COALESCE(...) DESC LIMIT 6` + `DISTINCT ON (n.url)` | `DISTINCT ON` 强制 ORDER BY 以 url 打头，于是 **LIMIT 取的是 url 序最靠前的 N 条，不是最新的 N 条**。实测莲花控股**最新的 6 条全被丢弃**，报告展示的是最旧的一批 |
| 时效过滤用 `COALESCE` | 见 ① |

**修法**：

1. **查询层**：三处（`_paper_news_rows` / `_plan_recent_news_titles` / `/api/news`）
   改成 `n.publish_time IS NOT NULL AND n.publish_time > now() - interval ...`，
   **彻底删掉 `COALESCE(..., fetched_at)`**。
   去重下沉到子查询（`DISTINCT ON` 留在内层、外层 `ORDER BY publish_time DESC LIMIT`），
   顺便修掉排序 bug。
2. **入库层**：无 `publish_time` 直接丢弃；`时间解析失败` 也丢弃。
   丢弃**必须打印**（`[news] 入库过滤：新增 N，丢弃 无时间/不可解析/超期`）——
   静默丢一半会让人误以为「今天没新闻」。
3. **解析器**：补 `周前/月(个)前/年(个)前/半?小时前`（月按 30 天、年按 365 天折算，
   误差方向安全）。修后 `test_time_parse.py` → `NO_GAP`。
4. **三级降级抽时间**（用户要求：先试 LLM，再搜关联报道，迭代 3 次，仍不行就放弃）：
   `news_fetcher.resolve_publish_time()`
   - **tier1 `pattern`**：`extract_publish_time(html)` — JSON-LD `datePublished`、
     meta(`article:published_time`/`og:published_time`/`publishdate`/`apub:time`)、
     `<time datetime>`、东财 `publishTime` JS 变量、13 位毫秒、正文「年月日」。
     **拒绝未来时间**（时钟错/预发布会让新闻永远留在近 7 日窗口，比没时间更坏）。
   - **tier2 `llm`**：让 LLM 读正文给日期，**但必须在正文里交叉验证到同一日期字符串**
     才认 —— 模型爱在没找到时答「今天」。
   - **tier3 `related`**：摘正文 300 字去搜关联报道，读**它们**的时间，最多迭代 3 轮。
     取**最早**那个（方向保守：宁可当旧的丢掉）。关联报道时间跨度 > 90 天
     说明是常青话题、无法断代 → 放弃。
     ⚠️ 搜索只取 URL，**绝不用搜索引擎给的 `pubDate`**（那是索引时间，见 SearXNG 那节）。
   - 全失败 → 丢弃。

**验证**：

| 脚本 | 项 | 结果 |
|---|---|---|
| `test_pub_extract.py` | 26 | 全过（1 级 8 种页面形态 + 未来/裸年份/无标签日期拒绝）|
| `test_news_time_gate.py` | 9 | 全过（3 条 SQL 真跑、零 NULL、按时间倒序）|
| `test_pubtime_chain.py` | 真网络 | **那 3 篇骗人的文章实测：2 篇 tier3 解析出 `2026-05-27`（133 天前）→ 被 7 天窗口正确丢弃；1 篇放弃丢弃** |
| `test_news_api_live.py` | 6 | 全过（`/api/news` 零缺时间、倒序、**阶跃星辰已不出现**）|

回归：`test_debate_persist` 54 / `test_trunc` / `test_cal_gate` / `test_settle_cursor` 14
**全过**。`test_time_parse` 修前 6 缺口 → 修后 `NO_GAP`。

**代价（明说，别让人以为是数据丢了）**：`sa_news` 15602 行里
**6761 行（43%）没有发布时间**，现在全部不再参与决策；
可用（近 7 天且有真实发布时间）**1549 行**，另有 **7292 行有时间但已超期**（本就超期）。
按来源看 NULL 率：`baidu 75%`、`bing 100%`、`duckduckgo 100%`、`searxng 17%`，
而 **`eastmoney` / `sina` / `ak_em` / `wx_channels` 全是 0%**（可信渠道）。

### 关掉 bing 与 duckduckgo（2026-10-08，用户「没用的就关掉吧」）

**先实测再关**（AGENTS.md 第 2 条），不是照着聚合数字下结论：

| 渠道 | 实测（600186 莲花控股） | 库里 total / 无时间 / 近30天 |
|---|---|---|
| **bing** | 返回 5 条、**带发布时间 0 条**、0.5s。标题是「公司简介-浙江大学…」「悟空洁身露增长51%」「2026中秋佳品礼盒」—— **跟莲花控股毫无关系** | 422 / 422 / **0** |
| **duckduckgo** | 返回 **0 条**、白等 **16.1s** | 668 / 668 / **0** |
| eastmoney | 20 条、20 条带时间、1.2s | 5699 / 0 / 4862 |
| searxng | 7 条、7 条带时间、1.7s | 1079 / 181 / 898 |
| baidu | 1 条带时间（稀疏但有效） | 7273 / 5490 / 1783 |
| sina | 本轮 0 条（偶发），但库里有 289 条近 30 天 → **保留** | 304 / 0 / 289 |

→ 只关 `bing` + `duckduckgo`，**`sina` 保留**（单轮为空是偶发，不能凭一次就判死）。
剩下的 `eastmoney / ak_em / baidu / sina / searxng` 一条没动。
实测关掉后仍能取到 **40/40 条带发布时间**的条目 —— 不是「全关了就没新闻了」。

**为什么不需要重启**：`load_config()` 每轮都重读 `config.yaml`
（`fetch_watchlist` / `fetch_topics` 都是 `conf = conf or load_config()`），
下一轮抓取自动生效。开关判断是
`if not ch_conf.get("enabled", False): continue` —— **缺 key 时默认关**（fail-safe），
而 `load_config` 是**逐个渠道 merge** 到 `DEFAULT_CONF` 之上，
所以 config.yaml 里的 `false` 不会被默认值覆盖。

⚠️ **`config.yaml` 含明文密钥且已 gitignore，不要提交**；
本次只同步改了 `config.example.yaml`（可提交那份）。

> 写测试时我又错了两次，都是**断言写错不是代码有洞**：
> ① 月份按 30 天折算，我却按日历月写期望（4 个月前应得 06-10 而非 06-08）；
> ② weibo 那条 fixture 我漏写了 `name=`，属性选择器匹配不到。
> 另外真正的代码 bug 是测试抓出来的：`_normalize_pub_dt(m.group(0))`
> 传了**含「发布时间：」前缀**的整串，而它内部用 `re.match` 锚定开头 → 永远解析失败。
> 第三处：探针里我把渠道配置读成 `conf["news"]["channels"]`，
> 而 `news_fetcher` 的 conf 里 channels 在**顶层** → 一开始打印 `[]`，
> 差点误判成「配置没生效」。

### 「这是不是新闻」不能靠关键词黑名单（2026-10-08 用户指出，已改成通用方案）

**用户的原话**：「不是写代码写死吧，下次不是这个关键字怎么办，应该是通用的呀」。

**背景**：莲花控股那 7 天窗口里混着 `分时-莲花控股`、`千股千问ROE数据`、
`股东户数(33户)`、`融资融券明细,2025年三季度`、`东方财富网_莲花控股_公司`。
我第一反应是「再加几个词到 `_NOT_NEWS_HOSTS` / `_NOT_NEWS_TITLE`」——
**那是错的**，那个黑名单已经有洞了（这些全是从正常新闻站 host 出来的），
再加词只是把洞挪个位置，下次换个词又漏。

**改法**：判断「这是不是一篇新闻」是**语义问题**，交给已有的
`_llm_filter_news_items()`，把 prompt 从「按关键词/按 2 天内」改成
**按内容类型**判断，并明确写了「不要用具体词去匹配，因为你不可能穷举下一次的关键词」。
代码只负责提供**结构化事实**（标题、来源、发布时间、URL），判断交给 LLM。

prompt 的三条判据（都不是关键词）：
1. **内容类型**：新闻/公告/解读 = 有一件事、有叙事主体；行情页、报价页、
   分时图页、数据统计表页、百科词条、目录页、聚合页 = 不是新闻
2. **有明确发布时间且在窗口内**；`publish_time` 为空一律剔除（与新规一致）
3. 与该股或其行业直接相关；同一件事的转载只留一条

**实测（真调 LLM，喂 600186 真实 14 条）**：
- **漏网的行情/资料页 = 0**
- 被剔除的包含**任何关键词表都不会列的**东西：YouTube 的 ASMR 视频、
  `EBOD-633 太阳望远镜选购`（百科词条）、`百科的进一步说明:225-236`、
  `商品ASMR混剪`、`龙虎榜:涨幅偏离值达9%`、两条融资融券明细
  → **证明它是语义判断，不是词表**
- 保留的 2 条都是真新闻（持有浙大智能制造创新中心股权、商誉减值约 7.20 亿元）

**顺带修掉一个真 bug**：`fetch_for_stock` 里
`uniq = _llm_filter_news_items(code, name, uniq)` **写了两遍** ——
同一个列表跑两次 LLM，既浪费又可能二次过滤。

**时间抽取那部分本来就是通用的**（顺带自证）：新增代码里唯一的中文字面量
只有**日期格式标记**（`年/月/日`、`发布时间/发表于/时间/来源/更新于/日期`）
和 LLM prompt，**没有任何股票名、公司名、题材关键词**。
1 级靠的是标准属性名（`article:published_time` / `datePublished` /
`<time datetime>`）+ 日期正则；2 级靠 LLM；3 级靠搜索关联报道。

> 编辑工具在这个文件上反复失配（**行尾混合**：242 个裸 LF + 部分 CRLF）。
> 最后用**按行号定位**的脚本改（`patch_llm_prompt.py`）才成功 ——
> AGENTS.md 早先记过这个坑，这里再确认一次。

### 🔴 止损失败会静默回滚 —— 3 只持仓跌破止损线却没卖（2026-10-08 修）

**症状**：用户问「科创50ETF华夏为什么没按计划严格止损」。查下来不是判据问题，
是**止损触发了但成交被回滚**：

| 代码 | 名称 | 成本 | 止损 | 止损价 | 10-08 最低 | 卖出记录 |
|---|---|---|---|---|---|---|
| 588000 | 科创50ETF华夏 | 1.6440 | -4% | 1.5782 | **1.5270** | **0** |
| 513980 | 港股科技ETF景顺 | 0.5840 | -5% | 0.5548 | **0.5510** | **0** |
| 01024 | 快手-W | 30.62 | -5% | 29.089 | **28.52** | **0** |

**真因**：`_execute_decision` 用 `with get_conn() as conn:`，
`_settle_buy_rows` 里抛的 TypeError（见上一条，游标 tuple/dict 不匹配）
会让**整个事务回滚** —— 刚 INSERT 的卖单一起没了。

**为什么没人发现**：`check_stop_loss` 把异常 `except` 掉，只
`traceback.print_exc()` + 塞进返回值的 `skipped`，**既不发通知也不计数上报**。
于是现象只是「今天没止损」，日志里躺着 traceback 但没人第一时间看见。
→ 与 AGENTS.md「静默降级比报错危险」同源：**风控静默失效最糟**。

**修法**（`check_stop_loss`）：
1. 异常分支给 skipped 项打 `FAILED: True`
2. 扫出 `failed` 列表 → 打印 `[paper] 🔴 止损失败 N 只` + `notify_fn(...)`
3. **只报 `FAILED`，不报 T+1 锁定 / 行情不可得**这类正常跳过 ——
   那是天天发生的，报了就是噪音，**噪音等于没报警**
4. 去重：`_STOPLOSS_ALERTED` 记 `(交易日, code, 原因)`，同一故障当天只报一次
   （盘中每 N 分钟一轮，不去重一天能发几十条同样的告警）
5. 通知本身包 try/except —— 通知挂了绝不能反过来打断止损循环
6. 新事件 `paper_stoploss_failed` 登记进 `notify_events.EVENTS`
   （`default: wx+email`，属于「必须看到」的一类）

⚠️ `event_default()` 对**未登记**事件是**全开**（fail-open，故意如此，
好让 `POST /api/notify/send` 手工发的消息不被静默丢掉）——
所以漏登记不会丢告警，但会没有 UI 标签、也没法单独配渠道。

**修复时机的尴尬**：`settle_buy_rows` 修好是 **15:54**，A 股 **15:00 已收盘**，
所以修复后没有任何交易时段可用。**下一个交易时段（10-09）才会真正成交。**

**仍未解决、需用户定口径**：止损判据是「**现价**跌破」，
所以 10-09 若反弹回止损线上方就**不会**卖 —— 今天那次破位不会被记住。
要不要改成「盘中触及即锁死」，属策略口径变更，没动。

**验证**：`test_stoploss_alert.py`（**18 项**，全 Fake deps，不碰库不联网）——
失败必发通知且带事件名/代码、去重生效（第二轮不重发但仍记 `failed`）、
T+1 跳过**不发**通知、通知炸了止损循环照常返回。
回归 `test_settle_cursor` 14 / `test_settle_realdb` 6 / `test_cal_gate` /
`test_trunc` / `test_debate_persist` 54 **全过**。
实机：`/api/notify/events` 里 `paper_stoploss_failed` 的
`effective = {wx: true, email: true}`。

> 写断言时我错了一次：以为「未登记事件会被静默丢掉」，
> 实测 `event_default` 是 **fail-open 全开**。断言写错不是代码有洞。
> 另外 `_execute_decision` 里的异常路径必须**整段包 try/except** ——
> 它是风控的最后一道闸门，闸门自己报错时不能连带把止损循环带走。

### 新浪板块资金流（2026-10-01 采纳为第三数据源）

> ⚠️ 我之前在本文件写过「新浪板块只有 49 行业，无资金流」——**那是错的**，
> 错因是只试了 `newSinaHy.php`（列表接口）。资金流在 `ssl_bkzj_bk` 里，一直可用。

```
http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk
  ?page=1&num=1000&sort=netamount&asc=0&fenlei={0|1|2}
```

- `fenlei` 0=行业(48) 1=概念(181) 2=证监会行业(154)，共 **383**，**单请求 ~140ms，无翻页**
- 15 列：`avg_changeratio` `turnover` `inamount` `outamount` `netamount`
  `ratioamount` `ts_name`(领涨股) `ts_changeratio` 等
- 只能当**辅助源**，实测三个硬限制：
  1. `avg_changeratio` 是**比率**且只保留 2 位小数 → 涨跌幅精度 **±0.5pp**，
     不足以做主源（实测 医疗器械 新浪 0.02 vs 同花顺 1.15）
  2. 申万口径，与同花顺 90 个行业只重叠 **4 个**
  3. **没有绝对「主力净流入」**：`ratioamount` 是「净流入/成交额」比率不是金额，
     且净额 ≠ 流入-流出（实测融资融券 流入930.30 + 流出798.45 ≠ 净131.85，
     差 1596.90 亿 —— 说明两侧不是同一集合）
  - 但 `netamount`（净流入绝对额）可靠，可用来补同花顺概念缺的资金流
- 同花顺 vs 开盘红涨跌幅中位差 **0.029pp**、90/90 在 1pp 内 → 可做交叉校验；
  **资金流方向不能**（同号率仅 65.6%，是主力大单 vs 全主动买卖的**定义差异**）

### 板块数据服务 `stock-advisor/data_service/`（2026-10-01 已部署）

替代上面那些「逐个手写 requests 调 push2」的采集器。三家互不相关的厂商聚合：

| 源 | 通道 | 板块数 | 定位 |
|---|---|---|---|
| 同花顺 | 经 akshare | 477（行业90+概念387） | **主源**，涨跌幅精度最高 |
| 开盘红/财联社 | 经 levistock | 405（行业104+题材259+地域42） | 辅助，多 `turnover_rate` 和地域/题材 |
| 新浪 | `ssl_bkzj_bk` | 383（行业48+概念181+证监会154） | 辅助，只补空不覆盖 |

- 部署在 **101.43.25.101**：`/opt/sa-data-service`，容器 `sa-data-service`，
  绑 **`0.0.0.0:8080`**（`docker ps` 显示 `0.0.0.0:8080->8080/tcp`，
  `ss -ltn` 是 `LISTEN 0.0.0.0:8080 users:(("docker-proxy",...))`）。
  **2026-10-08 更正**：本文件早先写「只绑 `127.0.0.1:8080`（不暴露公网），外部走
  SSH 隧道」——**已过期，是错的**。实测从本机（出口 `59.34.155.130`）直连
  `http://101.43.25.101:8080/health` 返回 200、`/boards` 返回 200/826 板块，
  **不需要隧道**。`stock-advisor/start_tunnel.ps1` 转发的是 **8001/WeRSS**，
  从来不包含 8080 —— 不要拿它当 8080 的前提。
- 容器内 akshare 是 **1.18.97**（`/usr/local/lib/python3.12/site-packages/akshare`，
  解释器 `/usr/local/bin/python3`）。**本地是 1.18.88** —— 版本已漂移，
  「本机复现」不等于「容器行为」，且本节记的 ths 实测结论都是 1.18.88 时代的。
  ⚠️ 容器解释器**不是** `/app/env_x86_64/bin/python3`（那是 WeRSS 容器的路径，
  在 sa-data-service 里会 `no such file or directory`）。
- ths 源会**间歇性**抛 `AttributeError: 'NoneType' object has no attribute 'text'`
  （2026-10-08 观测：连续 4 天 `last_ok=2026-10-04T05:10:53`、12 次调用全败，
  紧接着下一次 `calls=13` 就成功、`consec_fail=0`）。机制上唯一吻合的落点是
  akshare 里的 `soup.find(name="span", attrs={"class":"page_info"}).text` ——
  同花顺返回反爬页/空页时 `find()` 返回 `None`，`.text` 就炸。
  **本机 6 轮有界复现全部成功（90 行/轮），没能复现 → 属推断不属结论**。
  它表现为**单轮真降级**（`degraded=True`、ths 缺席、板块数 969→697），
  下一轮自愈，不要据此判定「ths 源已挂」。
- 已实测：容器内 `/boards` 返回 **969 板块**，`degraded=False`，四个源全部参与。
  涨停池走档 1（push2ex）实测 **52 家**、最高连板 7、炸板 34、32 个板块有涨停家数。
- **交易日感知**：`/boards` 和 `/health` 都返回 `is_trading_day` + `data_date`。
  休市日 `data_date` 是最近交易日，**调用方入库要用 `data_date` 而不是今天**。
- 接入：`sector.py` 的 `fetch_all_boards()` 优先走服务，失败回落东财（解封即双保险）
  —— 配置项 `config.yaml` 的 `sector.data_service_url`，留空则保持原行为
- 测试：`python -m data_service.selftest`（离线 116）
  + `data_service.tests.{test_budget,test_pipeline,test_sector_integration,test_service_api}`
  = **194 断言全过**
- 详细调研见 `docs/data-source-survey.md`（16 个开源方案逐项实测），服务说明见
  `stock-advisor/data_service/README.md`

**别把 200 请求/日的预算改大来「提高频率」**：一次完整采集约 55 个请求
（概念名表就 41 个），200 的预算只够 3 轮。预算耗尽会自动降级到新浪，
而不是像原实现那样无限打同花顺直到被永久封禁。

### 板块 code 是三套体系，混着用会变成「假 502」（2026-10-08 修）

**症状**：日志反复出现
`[sector] 成分股 BK1722: HTTP 502 {"detail":"成分股接口异常: substring not found"}`，
点板块行拿不到成分股。看起来像 data_service 或 adata 挂了。

**真因**：`substring not found` 是 Python `str.index()` 的 `ValueError`，
由 **adata 内部**抛出 —— 它拿到不认识的 code 后在响应里找不到预期片段。
调用方传的是**东财 BK 体系**，而服务端只认同花顺体系：

| 体系 | 样例 | 谁在用 | 能否查成分股 |
|---|---|---|---|
| 同花顺行业 | `881142` `881281` | data_service `/boards` 的 `code` | ✅ adata 直通 |
| 同花顺概念 | `886xxx` | adata 专用 | ✅（但 akshare 给的是 `3xxxxx`，**缺映射桥**）|
| akshare 概念 | `308941` | ths 概念名表 | ❌ 直喂 0/6 全败 |
| **东财板块** | **`BK1722` `BK0437`** | **2026-09-21 前的快照** | ❌ 完全不适用 |

最后一行为什么会出现：`sa_sector_snapshots` 里 **9279 行全是 BK 码**，
`snap_date` 停在 **2026-09-21**（东财被 WAF 掐死的那天）。前端行的
`data-bk` 取自这些陈旧快照 → 点行必然发 BK 码 → 必然踩 502。
东财回落路径也已死（实测 `push2delay` 直接 reset），所以拿不到成分股。

**修法（两侧都拦）**：
1. `data_service/api.py` 加 `THS_BOARD_CODE = re.compile(r"(?:881|886)\d{3}")`，
   在打 adata **之前**拦掉非 ths 体系 → 回可读 4xx，不再是 502。
   ⚠️ 「adata 真抛异常」仍回 502 —— 不能把上游故障也吞成 4xx。
2. `sector.py` 加同一条 `_THS_BOARD_CODE`，非 ths 体系**根本不发请求**
   （省一次白跑往返），日志直说原因。
   ⚠️ 两边正则必须一致（测试里断言了），否则各认各的、守卫形同虚设。

**验证**：`%LOCALAPPDATA%\Temp\opencode\test_fix_board_code.py`（**40 项**，含
「非 ths 体系一个 HTTP 请求都没发」「adata 没被调用」「上游真异常仍回 502」）；
端到端 `GET /api/sector/board/BK1722` 由 502 变 404 且日志不再出现 502，
`881142`/`881281` 正向 200 各 30 只成分股带真实涨跌幅。

**顺带查清但没动的事**：
- **日快照会自愈**。`sa_sector_daily` 只有 9 行、最后一天 2026-09-21。
  但链路 `_sector_auto_loop`(app.py:8622) → `collect_once` → `save_snapshot`
  是通的，且实测 `fetch_all_boards()` 现在能拿 **968~972 板块、
  `degraded=False`、四源齐全**，所以下一个 15:10 就会补上。
  ⚠️ **不要在盘中手动 `POST /api/sector/snapshot`** —— `_is_after_close`
  就是为了防止把半天的数据当全天写进历史表，那会污染动量/评分。
- **`save_snapshot` 今晚不会炸**：主键是 `(snap_date, code)`，
  而 `execute_values(..., ON CONFLICT DO UPDATE)` **同批出现重复 code 会直接报**
  `ON CONFLICT DO UPDATE command cannot affect row a second time`。
  实测这批 972 个里 `duplicated_codes=0`、code 全是 6 位，安全。
- **305/972 个板块没有 code**（新浪源不给），`save_snapshot` 会丢弃并计数上报。
  这是设计如此（主键需要 code），不是 bug。
- **概念成分股基本查不到**：akshare 概念码是 `3xxxxx`、adata 要 `886xxx`，
  中间的 name→886xxx 映射桥**服务端没做**。所以 `886xxx` 之外的
  `3xxxxx` 只会回可读 404。要补得先建那张映射表（AGENTS.md 早前记过方案）。

### ths 瞬时失败重试一次（2026-10-08 修，与上面同一次排查）

`ThsSource.fetch()` 拆成 `fetch()`（记账 + 重试）+ `_fetch_once()`（纯取数）。
依据是上面那条观测：ths 连续 12 次全败、第 13 次立刻成功，属瞬时故障。

**两条纪律，写错了就静默失效**：

1. **`SourceError` 不重试**。预算耗尽再打一次等于白烧配额（整轮约 53 请求，
   `budget_per_host` 只有 200）。
2. **只有最终失败才 `_fail()`**。中间那次重试失败若也记账，`consec_fail`
   虚增一倍，`/health` 的 `healthy=false` 就不再反映真实状态。

⚠️ **踩到的坑**：原来 `_fetch_once` 结尾那句
`if not out: raise SourceError("预算不足")` 会让新加的重试**完全失效** ——
`SourceError` 走的是不重试分支，「上游返回空页」被误判成「预算耗尽」。
已拆成两种：预算真被拒 → `SourceError`（不重试）；
上游给空 → 新增的 `TransientSourceError`（重试）。
`TransientSourceError` **刻意不继承 `SourceError`**，否则又回到不重试。

测试：`%LOCALAPPDATA%\Temp\opencode\test_fix_ths_retry.py`（**19 项**，含
「中间失败不记账」「SourceError 只打 1 次且不 sleep」「失败只记 1 次不是 2 次」）。
回归：`selftest` 116 + `test_budget` 15 + `test_pipeline` 25
+ `test_sector_integration` 24 = **180 项零回归**。
（`test_service_api` 打的是远端 101:8080，跑它验不了本地改动、还烧配额。）

### 部署 data_service 到 101（2026-10-08 实做，磁盘只剩 2.0G 时的正确姿势）

**部署形态**：`/opt/sa-data-service` 是**平铺拷贝**（`api.py` `sources.py`
`aggregate.py` `normalize.py` `__init__.py` `selftest.py` `tests/` + Dockerfile +
compose，**没有 `.git`**），所以改代码 = `scp` 覆盖文件，不需要推 git。
构建上下文就是该目录（compose `build: context: .`）。

**不能全量重建**：磁盘只剩 **2.0G / 95%**。`docker builder prune -f` 释放
**0B** —— build cache 本来就是空的。⚠️ `docker builder du` 报
「Reclaimable 5.542GB」是**共享层的重复计数，不可信**，别拿它做决策依据。
全量重建要重装 pandas/numpy/akshare/levistock/adata（成品镜像 407MB，
构建期临时层翻倍），有撑爆磁盘的风险 —— 和 WeRSS 那次一样的场景。

**做法：叠层构建**（对 WeRSS 用过的同一手法）

```dockerfile
FROM sa-data-service:1.0.0
COPY --chown=svc:svc api.py     /app/data_service/api.py
COPY --chown=svc:svc sources.py /app/data_service/sources.py
```

`docker build -f Dockerfile.overlay -t sa-data-service:1.0.1 .` 耗时 ~0.3s、
新层几十 KB、磁盘纹丝不动。⚠️ `--chown=svc:svc` 要写：基础镜像 `USER svc`
(uid 10001)，不指定 owner 会 COPY 出 root 拥有的文件。

**切镜像的四道校验（缺一道都可能「看着部署成功了」）**：

1. `scp` 后**逐字节 sha256 比对** + 远端 `ast.parse` —— 传坏了要在这一步炸
2. `docker run --rm --entrypoint sha256sum <新镜像> /app/data_service/*.py`
   —— 证明**镜像内部**是新代码，不只是构建目录里改了
3. `docker inspect sa-data-service --format '{{.Image}}'` 在 rebuild 前后取值，
   **ID 必须变**。ID 没变 = 没真重建（AGENTS.md 记过 we-mp-rss 的同款教训）
4. `docker compose up -d` **不要加 `--build`** —— compose 有 `build:` 段，
   加了会触发全量重建，磁盘不够

compose 改标签**必须用通配**并 `grep` 复核（写死版本号会静默空操作）：
`sed -i 's|image: sa-data-service:.*|image: sa-data-service:1.0.1|'` +
`grep -n "image:" docker-compose.yml` + 断言失败就中止。

**回滚**：`api.py.bak.<TS>` / `sources.py.bak.<TS>` /
`docker-compose.yml.bak.<TS>` 都在 `/opt/sa-data-service/`；
旧镜像 `sa-data-service:1.0.0`（`916f1de380a6`）也还在，
改回 compose 的 `image:` 再 `up -d` 即可。

**部署后验收（外部真请求，不是看容器在不在跑）**：

| 检查 | 结果 |
|---|---|
| `BK1722`/`BK0437`/`308941` | 404 + 可读指引（**不再是 502**）|
| `881142` ×10 采样 | **10/10 → 200**，count=56 |
| `881281` | 200，count=107 |
| `/boards` | 970 板块、`degraded=False`、`sources=['ths','sina','kph']` |
| 容器内 selftest | **116/0**（Python 3.12 + akshare **1.18.97** + adata 2.9.5）|
| 仓库 `test_service_api` | **14/0** |
| 本地端到端 | `BK1722`→404、`881142`/`881281`→各 30 只；日志零 502 |

⚠️ 验收时踩到一次**假故障**：`881142` 第一次返 502、随后连打 4 次全 200、
10 次采样零 502 —— 是 adata 自己的间歇故障（与我改的代码无关：
我只加了打 adata **之前**的体系守卫，合法 code 走的
`except Exception -> 502` 那段是原封不动的旧代码）。
**遇到一次 502 不要立刻判定部署失败，要连续采样**。

⚠️ 重启会清空 `/health` 的 `consec_fail`/`last_ok`/`calls`
（都在进程内存里）—— 所以刚重启后 ths 一律显示 `healthy=true`，
**不能据此说「ths 好了」**，要看 `calls` 涨过之后的状态。

### 新闻搜索渠道实测（2026-10-01/02，**别重测**）

| 渠道 | 实测 | 结论 |
|---|---|---|
| `eastmoney` | ✅ 可用 | 公告 + 资讯搜索 JSONP，0.2s，主力 |
| `ak_em`（akshare `stock_news_em`） | ✅ 可用 | **唯一 100% 真实发布时间**的逐股渠道，0.3s |
| `baidu` | ⚠️ 需 iPhone UA | 桌面 UA 会 302 到 `wappass.baidu.com` 图形验证码 |
| `sina` | ✅ 可用 | 滚动流按名过滤，3 页 × 2s 间隔，慢但稳 |
| `bing` | ❌ 噪声 | 中文财经词只认「公司」不认「新闻」，一轮入库 230 条全是官网/行情页 |
| `searxng` | ✅ 可用 | 101 上自建，实测 14~31 条/查询，出真新闻 |
| `duckduckgo` | ❌ 已死 | 2026-09-23 起本机与 101 均 TCP 443 超时 |

**关键坑**：
- 百度**必须用移动端 UA**（iPhone），桌面 UA 会被 WAF 拦。反爬判据不能用
  `"百度安全验证" in html`（编码乱码恒 False），要用 host 判断。
- 百度会**分阶段封禁**：36 只票 × 1 次（间隔 5s）后开始跳验证码。
  被拦后由健康度冷却自动跳过整轮，**不要继续捶源延长封禁**。
- 必应 `/news/search?format=rss` 返回 **0 条**（HTML 也只有 15KB 空壳）。
  网页 RSS 有 `pubDate` 但那是**索引时间**不是发稿时间，不写入 `publish_time`。
- akshare `stock_news_em` 首次 `import akshare` 要 **8~9 秒**，之后 0.15s。
  在渠道函数里 import 会把一次性开销算进渠道耗时，看起来像渠道慢。

### SearXNG 部署（101，2026-10-02 接入）

- 位置：`/opt/searxng`，容器 `searxng-core` + `searxng-valkey`
- 绑定：`0.0.0.0:8888`（2026-10-02 从 `127.0.0.1` 改的，安全组已放行）
- ⚠️ **SearXNG 自身无鉴权**，现在等于在公网开了一个搜索代理。
  任何人扫到 8888 都能用 101 的出口 IP 搜东西（滥用风险 + 流量成本）。
  用完建议改回 `127.0.0.1` 绑回环，或加一层带 token 的反代。
- 实测有效引擎：`sogou` / `yandex` / `360search`（14~31 条/查询）
- 已禁用引擎（101 上 TCP 443 不通或 0 条）：
  `duckduckgo` / `brave` / `google` / `mojeek` / `qwant` / `startpage` / `yahoo` /
  `wikidata` / `wikipedia` / `bing` / `baidu` / `ecosia` / `marginalia`
- 配置项：`config.yaml` 的 `news.channels.searxng.url`
  （留空 = 关闭，填 `http://101.43.25.101:8888` 启用）
- 接入：`news_fetcher.py` 的 `fetch_searxng()`，走 JSON API
  （`?q=...&format=json&language=zh-CN`）

### WeRSS 多账号配额 failover + 前端多次扫码（2026-10-04，已部署 101）

**配额是账号级（`wr_vid`）不是 IP 级** —— 已实测：同一个 cookie 从
`101.43.25.101` 和本机 `59.34.155.130` 分别请求，**都是 `499 -2014`**，
一字不差。换 IP 无法绕过，**「把刷新/采集挪到本机」这条路从根上不成立**。

**改的是自己的 fork** `hellostronger/we-mp-rss`（本地 `D:\correct_your_life\we-mp-rss`）：

| 提交 | 内容 |
|---|---|
| `0d498c5` | 多账号 failover：`_weread_accounts` / `_switch_weread_account()` / `_weread_http_get()` |
| `93ceb59` | QR 登录写 `accounts[]` 数组（按 `vid` 去重追加，旧格式自动迁移） |
| `e706e98` | header 构造收敛到基类 `_weread_headers()`，`_request_headers` 留作别名 |

**关键设计点**：

- `wx.lic` 的 `weread_data.accounts: [{cookie, vid, name}, ...]`
  —— **旧单账号 `weread_data.cookie` 格式自动包装成 1 元素数组**，不迁移也不报错
- failover 触发条件：`499` / `-2014` / `-2041` / `-2012` 四个码**都**会切号
  （它们是配额耗尽时的三种伪装，见上文「三码齐出」那条）
- **每个账号最多试一次**，全耗尽抛 `WereadMPAPIError(retriable=False)`，不死循环
- 切号时 `cookie/ticket/vid/name` 四个字段**一起换**（漏一个就会出现
  「用 A 的 cookie 配 B 的 ticket」这种混合态）
- 请求头**只有一处来源** `MpsWeread._weread_headers()`。
  ⚠️ 改造中间态踩过：三个调用点改走基类后，`_request_headers` 变成**死代码**
  （grep 只剩定义），而基类里我又复制了一份 dict —— 以后谁改子类那份都不会生效，
  **且没有任何报错**。这类「副本漂移」改完必须 grep 确认。

**前端多次扫码（用户要的）零前端改动**：`web_ui/src/api/weread.ts` 已有
`/wx/weread/qr/code|status|over`，扫码成功走 `_save_cookies_to_lic` 追加账号。
所以「扫码一次 = 加一个账号」，扫 N 次就有 N 个配额池。

- `jobs/mps.py:74-75` **每次任务都 `MpsWereadMP()` + `_load_weread_auth()`**
  → **扫完码下一轮任务即生效，不需要重启容器**
- `/api/v1/wx/auth/wechat/unbind` 只删公众号 token，**不碰 `weread_data`**，
  不会误清 `accounts[]`

**离线断言**（可重复跑，不烧配额）—— 用 `importlib.util.spec_from_file_location`
按路径加载模块，因为 `core/wx/__init__.py` 会连**真实 DB**（`core.db` 模块级
`create_engine(cfg.get("db"))`），直接 import 会抛
`Could not parse SQLAlchemy URL from string ''`。要 stub 掉
`core.db` / `core.config` / `core.print` / `core.log` / `core.wx.base`：

- `%LOCALAPPDATA%\Temp\opencode\test_multi_account.py` —— **34 项**（failover/切号/终止/header）
- `%LOCALAPPDATA%\Temp\opencode\test_qr_multi_account.py` —— **22 项**（QR 追加/去重/迁移）

### 101 容器运维的三个坑（2026-10-04 都踩了）

1. **`docker exec` 的解释器不是 venv 那个**：系统 `python3` 连
   `yaml` / `requests` 都没有 → `ModuleNotFoundError`，
   必须 `/app/env_x86_64/bin/python3`（AGENTS.md 早先只提过 bcrypt，同样原因）
2. **`git fetch` 会静默失败**，脚本用 `2>&1 | tail -2` 一吞就只剩「后面步骤超时」。
   **判据必须查 `FETCH_HEAD` 的实际 sha，不能看返回码**。
   症状：build 成功、镜像 digest 与上一次**完全相同**（`b699c265`）、
   `git log` 还停在旧 commit → 源码根本没更新，却看起来「部署成功了」。
   同理本机 `git fetch` 也要重试（github 抽风：`early EOF` / `Connection was reset`），
   且**必须设 `NO_PROXY=*`**，否则走代理直接 reset
3. **`sed` 替换 compose 镜像标签要写通配**：我按上一版标签
   （`126993c`）写死 `sed 's|...fork-scratch-126993c|...|'`，但 compose 早被改成
   `0d498c5` → **sed 空操作、静默无效**，容器还在跑旧镜像。
   用 `s|image: we-mp-rss:fork-scratch-.*|image: <新>|` 再 `grep` 复核

**磁盘**：删镜像标签**几乎不释放空间**（分层共享），真正的大头是
**build cache**（当时 2.97GB）。`docker builder prune -f` 之后
96% → 89%（40G 用 34G，剩 4.4G）。镜像内 `/app` 自己就 2.4GB。

### 全市场供给冲击 + 海外市场：解禁潮 / 增发 / 减持 / 美日韩资金抽离（2026-10-06）

新增 `stock-advisor/supply_events.py` + `global_markets.py`，端点
`/api/supply-events/calendar`、`/api/global-markets/snapshot`，守护线程
`sa_supply_market`（工作日 8:30 后一轮，**只在有信号时才推**）。

**与 `alerts.py` 是两个维度不是替代**：`alerts.py` 逐代码查 → 只覆盖自选股；
新模块去掉 `SECURITY_CODE in (...)` 过滤 → **全市场**，按日聚合判「潮」。

### 解禁数据源实测（东财 datacenter-web 可用，别再怀疑）

| 事实 | 值 |
|---|---|
| 解禁 `RPT_LIFT_STAGE` | 全表 **31,617 行**；未来 60 天 293 行 |
| 增发 `RPT_SEO_DETAIL` | 全表 5,900 行；未来 180 天仅 **3 条**（天然稀疏，**不能用分位数判潮**）|
| 减持 `RPT_SHARE_HOLDER_INCREASE` | 全表 **146,773 行**，`DIRECTION` 取中文字符串 |
| 延迟 | 0.1~0.3 秒/请求；`pageSize=300` 可一次拿完 293 行 |

**坑（都是实测）**：

1. `LIFT_MARKET_CAP` 单位是**万元**（不是元），/1e4 才是亿元；
   `CURRENT_FREE_SHARES` 是**万股**。写错差 1e4 倍。
2. **`DIRECTION="减持"` 的中文枚举值必须用双引号**。单引号
   `(DIRECTION='减持')` 实测**静默返回空** —— 看起来像「最近没人减持」。
3. 聚类字段（`COUNT`/`SUM_MARKET_CAP`）**不存在**，报
   `9501 COUNT返回字段不存在` → 按日聚合必须在客户端做。
4. **「空」有两种原因，必须区分**：`success=true` + `result=null` = 真的 0 行；
   `success=false` + `code=9501` = **参数写错**。HTTP 都是 200。
   实测踩过：`PREDICT_DATE` 列不存在时报 `9501`，差点误判成「未来无增发」。
   `_dc_query` 因此返回 `meta`（success/code/message/count）。
5. `LIFT_MARKET_CAP=0` 占 0.9%（都是定向增发机构配售），求和不失真但要单独计数。

### 减持做不出「前瞻日历」——源的结构性限制

`RPT_SHARE_HOLDER_INCREASE` 的 `NOTICE_DATE` / `START_DATE` / `END_DATE` /
`TRADE_DATE` **四个字段在未来窗口全部 0 行**（2026-10-06 实测）——
这张表**只收录已过公告日**的减持。所以：

- `supply_events.py` 里减持是 **`MODE="backward"` 回顾模式**，不做假前瞻
- 真要前瞻只能走逐代码公告检索（`alerts.py` 的 `f_node=7`），那是自选股维度
- 渲染里明写这个限制，不假装有

### 「潮」判定：两套基线，绝不能混用

| 判定 | 对照基线 | 代码 |
|---|---|---|
| **单日**潮度 | 历史「每日」解禁市值分布 | `classify()` |
| **窗口合计**潮度 | 历史「同长度滚动窗口合计」分布 | `classify_window()` |

⚠️ **踩过的方法论错误**：初版拿「30 天合计 3213 亿」去比「单日 p90 = 328 亿」，
结果**永远判「强潮」= 永远报警 = 等于没有报警**。必须用滚动窗口分布
（实测历史同 30 天窗口 p50=1629 / p90=2997 亿 → 3213 亿判强潮才是真结论）。

- 分位数用 **nearest-rank**（`int(n*p)`，不插值）：只用于分级，行为要可复算
- 基线**只用有事件的日历天** —— 那天没解禁不是「压力小」而是「那天没这回事」
- 基线样本 < 10 天 → **不给评级**（不硬编「安全」结论）
- 每类事件**各自**的请求预算（`BUDGET_PER_KIND`）：共用一个池子会被先执行的
  解禁吃光，减持直接采不到，且表现为「最近没人减持」

### 2026-10 真实核实：「10 月解禁上千亿」成立，但低估 3 倍

| 指标 | 值 |
|---|---|
| 解禁市值 | **3,105 亿元**（139 只，124.87 亿股）|
| 12 个月排位 | **第 2**（仅低于 2025-12 的 3,290 亿）|
| 12 个月均值 | 2,305 亿 → 10 月是 **1.3 倍**|
| 最猛两天 | **10-28 990 亿（9 家）/ 10-21 749 亿（5 家）**，合计占全月 56%|

**「解禁市值」≠「新增抛压」**，按类型拆开后：

| 类型 | 市值 | 占比 |
|---|---|---|
| 追加承诺限售股份上市流通 | 1,398 亿 | 45.0% |
| 首发原股东/战略配售 | 1,019 亿 | 32.8% |
| **定向增发机构配售** | **483 亿** | **15.6%** |
| 股权激励（64 条） | 18 亿 | 0.6% |

真正「成本低、减持倾向强」的是**机构配售那 483 亿（15.6%）**。
「追加承诺限售」占 45%、1,398 亿 —— 从字面看是对已解禁股份的自愿延长锁定，
到期多为形式到期；**这条是解读不是核实过的事实**，所以不算进「真压力」。

数据源**单一**（akshare 1.18.88 里 `stock_restricted_release_summary/detail/batch`
三个名字**不存在**，`stock_restricted_release_queue_em` 只返回 4 行近期批次，
无法交叉验证）。

### 美日韩：KOSPI 真的取不到（试过 4 条路径，2026-10-06）

| 源 | 结果 |
|---|---|
| `akshare.index_global_spot_em()` | **push2 系全封**；10-05 偶然返回 56 行，10-06 就 `RemoteDisconnected` → 再次印证「一次成功不代表源可用」|
| `hq.sinajs.cn/list=int_kospi` 等 10 个符号穷举 | 全空 |
| `index_global_name_table()` 有「首尔综合指数/KOSPI」 | 但 `index_global_hist_sina(symbol="KOSPI")` 抛 `KeyError` —— **名字表有代码 ≠ 历史接口支持** |
| 新浪 K 线接口 | `Service not valid` |

→ 模块里**明确把 KOSPI 标为不可用**并写进报告，**不用任何代理指标冒充**。

可用的源（全部实测）：

| 市场 | 源 | 符号 |
|---|---|---|
| 纳指/标普/道指 | 腾讯 `qt.gtimg.cn` | `usIXIC,usINX,usDJI` |
| 恒生/恒生科技/国企 | 腾讯 | `s_hkHSI,s_hkHSTECH,s_hkHSCEI` |
| 上证/沪深300/中证500 | 腾讯 | `s_sh000001,s_sh000300,s_sh000905` |
| 日经225 | 新浪 `hq.sinajs.cn` | `int_nikkei`（**仅现货**）|

**坑**：

1. **腾讯没有日韩符号**：`s_jpNI225`/`s_krKOSPI`/`jpNI225` 等全部
   `v_pv_none_match="1";`
2. **腾讯字段布局按市场前缀不同**（写错不报错、静默给错数）：
   - `us*`：`[3]`现价 `[4]`昨收 `[5]`今开 **`[32]`涨跌幅**
   - `s_*`（港/A）：`[3]`现价 `[4]`涨跌额 **`[5]`涨跌幅**
   把港股的「涨跌额 68.05」当涨跌幅 → 数值离谱但不报错
3. **日K 要去掉 `s_` 前缀**：`s_hkHSI` 返回 0 根，`hkHSI` 返回 30 根
4. **美股/日经无历史源** → 只能算**当日**涨跌，算不了 5日/20日；
   渲染里用 `SPOT_ONLY` 标注「（仅当日）」，不假装有
5. 现货**2 次请求**拿全（腾讯批量 + 新浪批量，逗号分隔）

「资金抽离」信号定义（阈值写死、不预测）：港股当日跑输纳指 ≥1.0pp /
跑输日经 ≥0.8pp / 上证跑输恒生 ≥0.8pp。数据缺失时**不产生信号**
（实测「全 None 不出信号」）。

### 辩论结论落库保全（2026-10-05 修）

**问题**：`paper_trading._decide_one` 的三个分支各自手拼 `decision["debate"]`：

| 分支 | 修复前 | 修复后 |
|---|---|---|
| 正常 | 存全文 | 不变 |
| 交易员第二轮解析失败 | 只存 `rounds`/`llm_calls`/`rating` —— **Bull/Bear/Judge 正文全丢** | 加标记 `trader_round2_parse_failed`，正文照存 |
| 抛异常（`format_debate_for_trader` 或第二轮 LLM 炸）| 只存 `str(exc)` —— **辩论已跑完的钱全白花** | `debate_result` 提到 try 外，异常时照样打包落库 |

- 新增 `_pack_debate()` 统一打包（含 `history` 字段，之前根本没用上）
- `risk_report` 原来硬编码 `[:3000]` 截断且**不带标记**（看的人会当完整原文），
  现在统一 `_cap()`，超长附 `[截断，全文 N 字符]`；每段 8000 字符上限
- **新增 sidecar** `data/paper_decisions_YYYYMMDD.jsonl`：决策与成交是**两阶段**
  （`run_decisions` 先全部决策再排序成交），中间崩溃/被 kill 时 DB 里一条都没有，
  但 LLM 的钱已经花掉了。每只票决策完**立刻**逐行追加（崩溃安全、不依赖事务）

⚠️ sidecar 按 `__file__` 定位 `data/`：**测试必须重定向** `os.path.dirname`，
否则测试数据会写进真实仓库（第一版测试就在 `stock-advisor/data/` 留下过垃圾，
已清理；`data/` 在 .gitignore 里没进版本库）。

测试：`%LOCALAPPDATA%\Temp\opencode\test_debate_persist.py`（54 项）、
`test_supply_global.py`（65 项）、`test_supply_e2e.py`（端到端真发 HTTP）。

### 微信视频号监控（2026-10-06，闭环已跑通，卡在 PC 微信那一端）

新增 `stock-advisor/channels_watch.py`。**视频号没有公开 API**，网页端也拿不到
内容（`wx_channel` 文档原文：「微信视频号内容来自独立客户端，浏览器不能直接访问
内容」），所以必须 MITM 代理 + PC 版微信 —— 这条链路 101（无桌面 Linux）**跑不了**。

开源方案调研（全部 `api.github.com` 核实，别只信搜索摘要）：

| 仓库 | star | License | 最近提交 | 结论 |
|---|---|---|---|---|
| `nobiyou/wx_channel` | 2699 | **MIT** | **2026-10-05** | ✅ **用这个**（v5.7.10，有 API + radar 监控变体）|
| `ltaoo/wx_channels_download` | 9626 | NOASSERTION(Commons Clause) | 2026-09-30 | 上游鼻祖，License 非 OSI |
| `qiye45/wechatVideoDownload` | 5857 | **NONE** | 2026-10-05 | 无 License = 保留所有权利 |
| `will-17173/electron-...downloader` | 66 | MIT | 2025-07-21 | 停滞 1 年 |
| `KingsleyYau/WeChatChannelsDownloader` | 60 | NONE | 2020-09-27 | 6 年前 |
| `crossthere/sph_caiji_wenan`（搜索里的「视频转文字」）| — | — | — | ❌ **404，仓库不存在** |

用到的接口（`wx_channel` API 端口 2026）：`/api/channels/contact/feed/list?username=`
（监控核心）、`/feed/profile`（描述文案）、`/feed/comment/list`（**评论区有**）、
`/contact/search`。参数名是 `object_id`/`nonce_id`（不是 objectId）。

**闭环**（已端到端验证，见下）：
```
订阅 → 轮询 feed/list → 按 object_id 判重 → feed/profile 取描述
  → （可选）下载 mp4 + ASR → 情绪判定 → ① data/channels_items_*.jsonl
  → ② sa_news（url 唯一去重，code=''）→ sa_news_related（多对多关联股票）
```
**不新建表**：`sa_news` 以 `url` 唯一、`code=''` 表示非股票源，正好容得下，
关联股票走 `sa_news_related`。完整正文（描述+ASR）不塞进 `sa_news`
（title 语义是标题），落 jsonl 用 url 关联。

### ⚠️ `save_to_sa_news` 内部 commit → 测试 rollback 变空操作（污染了生产，已清理）

端到端测试第一版把 3 行测试数据写进了**生产 `sa_news`**（14998 行），
而测试结尾那句 `conn.rollback()` 什么都没回滚 —— 因为 `save_to_sa_news`
内部无条件 `conn.commit()`。

**教训（比 bug 本身更重要）**：

1. **凡是需要能被调用方放进事务的写入函数，就不要自己 commit**。
   加 `commit: bool = True` 参数，`commit=False` 时不提交。
2. **测试里「回滚了」不等于「没写进去」** —— 必须**回滚后再查一次计数**，
   拿数字说话。现在 E2E 末尾就是这么断言的：
   `assert left == 0, "回滚没生效！生产表里还有 N 行测试数据"`。
3. 清理脚本先 SELECT 看清要删什么、先删子表再删主表、删完再复查
   （`clean_test_pollution.py`）。清理后 14998 → 14995、关联 6 行清零。

### 视频号 ASR（L2）：两种端点 + 网关是 NVIDIA NIM 而非 SiliconFlow（2026-10-06）

**先纠正本文件早前两处错**：

1. 「`XingChenAGI/XingChenASR-V3.2-Ultra` 不是硅基模型名」——**错**，见下方「我判错的两处」
2. 「在 101 网关加一个 SiliconFlow 渠道就能通」——**不完整**：网关跟 SiliconFlow
   **毫无关系**（见下），加渠道的前提是有一个**充值过的** SiliconFlow key

#### ASR 有两个不同端点，选错必然失败

| 端点 | 适用模型 | 请求体 |
|---|---|---|
| `POST /v1/audio/transcriptions` | SenseVoiceSmall / TeleSpeechASR / **XingChenASR-\*** / Qwen3-ASR-1.7B | multipart，`files={file:...}`，**≤1h / ≤50MB** |
| `POST /v1/chat/completions` | **Qwen3-Omni-30B-A3B-\***（用户最终指定） | JSON，音频是 content part：`{"type":"audio_url","audio_url":{"url":"data:audio/wav;base64,..."}}` |

- **计费：omni 音频 13 tokens/秒**（官方原文：22.5s = 292 tokens）
- 官方文档页的 `enum` **不完整** —— 判据是 `/v1/models` 的真实返回
- `channels_watch.py` 里两个 provider：`SiliconFlowAsr`（transcriptions）
  和 `QwenOmniAsr`（chat），`get_asr()` **按模型名路由端点**，不只看 provider
- 分片下限 `MIN_CHUNK_SECONDS=30`：低于 30 秒会把一次转写拆成几十次请求，
  每次都付音频费 + 冷启动，反而更贵
- 非 WAV（视频号下载是 mp4）需要 ffmpeg 抽音轨，**没有就明确报 `asr_no_ffmpeg`**，
  不静默降级

#### ⚠️ 101 网关是 NVIDIA NIM，不是 SiliconFlow 代理

读 `one-api.db` 的 `channels` 表（**别 SELECT `key` 列**）：

| id | name | base_url | 模型数 | status |
|---|---|---|---|---|
| 2/3/4/5/6/7/9 | 杭威/文启/12/小红书/军强193/军强/海瑞 | `https://integrate.api.nvidia.com` | 14~572 | 1（3 是 2=停用）|
| 10 | gitee | `https://ai.gitee.com` | 5 | 1 |

→ 这解释了之前那些 `404 Function '<uuid>': Not found for account '<uuid>'`
（NIM 的 deployment id），以及为什么模型名长得像 SiliconFlow（NVIDIA 也托管
`nvidia/*`、`z-ai/*`、`moonshotai/*`、`deepseek-ai/*`）。

**8 个渠道没有一个含 `Qwen3-Omni`**，含 `Qwen3-Omni` 的判断是逐条 LIKE 出来的
`no`。渠道 3（文启，停用）的模型列表里**有** `FunAudioLLM/SenseVoiceSmall`，
但那是一份 572 个模型的通用聚合清单、`base_url` 仍指向 NVIDIA。

#### 网关的 nemotron-3-nano-omni **不能**做中文 ASR（采样率全档实测）

`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` 是网关上唯一名字带 omni/audio
的模型，它**确实吃音频**（不是 model_not_found），但转写质量不可用：

| 采样率 | 字符覆盖 | 备注 |
|---|---|---|
| 22050（原始） | 0% | 全 `<unk>`，5200+ tokens 全是未知 token |
| 16000 | **54%** | 真实转写但系统性音近错 |
| 8000 | 39% | |
| 24000 | 43% | 最快（6s） |
| 44100 | 21% | |
| 16000/32000/48000 | — | 两次都撞 `503 ResourceExhausted`，重试仍 503 |

判读门槛 85%（财报错字不可接受）—— **无一档达标**。音近错形态：
`市场→石场`、`信号→新好`、`明显→明天方`、`解禁→基金归`。

**两种故障要分开认**（同一条链路、不同根因）：
- 全 `<unk>` = 音频解成词表外 token（解码失败）
- 有汉字但音近错 = 音频真被听了，但**听错了**（模型/编码器能力问题）

`503 ResourceExhausted: Worker local total request limit reached (16/16)` 在这个
模型上是**常见态**（7 档里 5 档首撞），必须退避重试才能拿到真实结果 ——
一次探测下结论会误杀。

#### 自己踩的坑：`audioop.ratecv` 返回裸 PCM，不是 WAV

采样率扫描第一版整轮作废：`to_rate()` 直接 return `ratecv()` 的返回值
（**只有帧数据**），base64 开头是 `AAAAAAA`（0x00）而不是 `RIFF`。
服务端回 `HTTP 500 Failed to load audio from data:audio/wav;base64,AAAAA...`
—— 这个报错本身是**正确的**。断言 `blob[:4] == b"RIFF"` 现在写进脚本。

判别「模型不支持音频」和「我传的音频是垃圾」：看错误里回显的 base64 **开头**。

#### 现状与唯一阻塞

用户最终指定 `Qwen/Qwen3-Omni-30B-A3B-Instruct`，`config.yaml` /
`config.example.yaml` 已切好（`provider: auto`）。唯一阻塞：
**硅基余额不足** `402 {"code":30001,"message":"Sorry, your account balance is
insufficient"}`。key 有效、端点通、模型有权限（402 而不是 404/401）。

解法只有两条：① 充值个人账户；② 充值后把 key 建成网关的 SiliconFlow 渠道
（这样 ASR 走网关不占个人额度）。离线断言 104 项全过
（`%LOCALAPPDATA%\Temp\opencode\test_channels.py`）。

### 视频号：wx_channel 本机跑起来的实测 + 我判错的两处（2026-10-06 01:20）

**部署**：`nobiyou/wx_channel` v5.7.10 的 `wx_channel_radar.zip`（16.9MB 便携版，
免安装）→ `%LOCALAPPDATA%\Temp\opencode\wx_channel\app\`。
`config.yaml` 从 `config.yaml.example` 拷，把 `radar_enabled` 改 `true`。
启动后 **2025（代理）/ 2026（API）端口在听**，证书流程自动装好了 SunnyNet。

**本机前置条件（都满足）**：

- PC 微信已装：`Weixin.exe`（4.x，6 进程）+ 旧版 `WeChat.exe` 3.9.12.51
- **视频号宿主 `WeChatAppEx.exe` 13 个进程在跑**（工具就是注入它）
- 系统证书里有 `CN=SunnyNet`（工具第二次启动时 `certificate.installed=True`）

**卡住的唯一门槛：注入要管理员权限**

```
connected=false  clients=0  ready_clients=0
injection: {target_process: "WeChatAppEx.exe", started: false,
            last_error: "StartProcess returned false; administrator permission
                          may be required"}
搜索接口 → 503 "No ready WeChat page is available for search"
```

用 `-Verb RunAs` 提权被 UAC 拒绝（「此操作已被取消」），**我无法自行跨过**。
需要用户：① 以管理员身份运行 exe ② 在微信里打开视频号页面。
另外雷达文档明写「如果博主列表为空，**请先在视频号添加博主**」——
所以要监控某博主，得先在微信视频号里关注/添加它。

**「只开系统代理」实测无效（已回滚）**：我怀疑 `ProxyEnable=0` 是直接原因，
于是把系统代理临时指到 `127.0.0.1:2025`（原值 `ProxyEnable=0` /
`ProxyServer=127.0.0.1:6478` 已记录并加了 `127.*;<local>` 旁路），等 25 秒后
`clients` 仍是 0。工具日志给出答案：**「可能需要【管理员权限】才能开启系统代理」**
—— 非提权时它自己都设不了系统代理；且微信大概只在启动时读一次代理。
**已回滚到原值**（别把用户的代理留在指向一个抓不到流量的工具上）。

### ⚠️ `channels_watch.py` 解析层路径全错 —— 这模块**从没跑通过**（2026-10-06 修）

**症状**：E2E 实测博主「多空看财报」，`search_contact` 恒返回 `[]`、
`contact_feed_list` 恒空 → 看起来像「搜不到 / 该博主没视频」，
但**同一时刻直接 curl 同一个 URL 返回 15 条真实数据**。

**真因：当初照 `web/docs/API.md` 的示例写，而 v5.7.10 的真实响应是三层 `data`。**

| 方法 | 代码原来找的 | **真实路径**（2026-10-06 实测）|
|---|---|---|
| `search_contact` | `data.list` | **`data.data.infoList[]`**，账号包在 `.contact` 里 |
| `feed/list` 的 items | `data.list` / `data.feed_list` | **`data.data.object[]`** |
| `feed/list` 分页 | `data.next_marker` | **`data.data.lastBuffer`** |
| `feed_profile` | `data` | **`data.data.object`**（`data` 是 `{data,errCode,errMsg,payload}` 空壳，拿它当 profile 只会得到没有正文的壳）|

字段名同样对不上（`_first` 靠候选名取，一个都命不中）：

| 代码找的 | **真实字段** |
|---|---|
| `desc` / `description` | **`objectDesc.description`** —— `objectDesc` 是**对象不是字符串** |
| `title` / `shortTitle` | **`objectDesc.shortTitle` = `[{'shortTitle': '...'}]`（`list[dict]`！）** |
| `create_time` / `createTime` | **`createtime`**（**小写 t**，`dict.get` 区分大小写）|
| `nonce_id` / `nonceId` | **`objectNonceId`** |
| `object_id` | `id`（这条本来就靠 `id` 兜住了，没暴露）|

**最阴的是 `shortTitle`**：它是 `list[dict]`，`str()` 出来是 Python repr，标题会
原样显示成 `[{'shortTitle': '老腾讯赚钱新腾讯烧钱'}]` —— **数据一直在，
是渲染层把它变成了垃圾**。这与「200 + data:null」「渲染丢数据」是同一类陷阱：
**必须在「看起来有值」的地方再确认一次值的类型**。

**修法**：4 处路径改真实结构（每处都保留旧路径做兜底，见各方法 docstring）+
新增 `_flat_text()` 摊平类型不稳的值（str/dict/list/嵌套全兜，dict 未知键递归一层）
+ `extract_text` 改走 `_desc_text()` 从 `objectDesc` 取。

**验证（三层，都可重复）**：

| 层 | 脚本 | 结果 |
|---|---|---|
| 离线 | `%LOCALAPPDATA%\Temp\opencode\test_channels_offline.py` | **70 项全过**，fixture 是真实响应快照 `%TEMP%\opencode\wx_fixture\*.json`，patch `_get` **不联网** |
| 回归 | `%LOCALAPPDATA%\Temp\opencode\test_channels.py` | **104 项，改完仍 exit=0** |
| E2E | `%LOCALAPPDATA%\Temp\opencode\test_channels_e2e.py` | 真跑通：**15 条视频 + 评论**（`comment_meta ready=True total=6 collected=6`），三轮幂等 10→5→0 |

> 测试里我三次把自己的断言写错（首轮受 `max_new_per_sub=10` 限制只取 10/15、
> 期望 `poll_once` 自己持久化、把另一次请求的 `objectNonceId` uuid 硬编进断言）
> —— **失败的不一定是要修的代码**。判据是「真实结构到底是什么」，
> 不是「断言说什么」。

### 🔑 流量重定向**必须提权**（2026-10-06 03:15 实测，非提权是死路）

判据是 `netstat`，不是猜：

```
非提权实例（能绑 2025/2026、cert.installed=True、lifecycle 正常）：
  微信活动连接 8 个 → 183.61.x / 121.12.x / 101.91.x / 117.89.x **全部直连 :443**
  连到 127.0.0.1:2025 的微信连接数 = 0
  全系统连 2025 的进程数 = 0（连我自己发的探测请求都不算）
  → 一个连接都没被 SunnyFilter2.sys 重定向
```

所以三条链路条件缺一不可，**缺的都是提权那一条**：

| 条件 | 非提权 | 提权 |
|---|---|---|
| 端口 2025/2026 | ✅ 能绑（>1024 不需要管理员）| ✅ |
| 证书 `CN=SunnyNet` | ✅ 装在 `LocalMachine\Root`，**跨实例有效，不用重装** | ✅ |
| `injection.started` | ❌ `StartProcess returned false; administrator permission may be required` | ✅ |
| **微信流量走 2025** | ❌ **0 连接** | ✅（实测通了，见下）|

> **提权成功过一次**（本节上方 01:20 那段写「被 UAC 拒绝、我无法自行跨过」，
> 在**当次会话后来成功了**：`Start-Process -Verb RunAs -PassThru` 通了 →
> `injection started=True` → 点视频号入口后
> **`connected=True ready=1 search_ready=1 lifecycle=healthy`**，
> 搜索立刻返回真实数据）。UAC 是否放行取决于用户当时点没点，
> **别把一次被取消当成永久不可行**，也别把一次成功当成不用再验证。

**运维三个坑（都会表现成「工具莫名其妙不可用」）**：

1. **非交互 shell 里 `Start-Process` 起的进程会被回收** —— 表现为
   「起来了、端口在听、还服务过一次 200，几十秒后突然 `无法连接到远程服务器`」。
   必须用 harness 的 **`background: true`** 常驻跑（`sh_...`），
   不能用 `Start-Process` + `Start-Sleep` 就当它活着。
2. **僵尸实例会占掉「正常实例」的预期位**：提权实例死掉后，剩一个
   `拒绝访问`（= 它是提权的）却**零监听端口**的残留进程 —— 杀不掉、也没用。
   判据用 `netstat -ano | findstr <PID>`，**不要用 `Get-Process` 还在就以为它在服务**。
3. **`Get-NetTCPConnection -OwningProcess` 可能因为权限静默返回空**，
   与「真的没监听」不可区分 —— 用 `netstat -ano -p tcp` 交叉验证。

**当前数据状态（实测留下的）**：
`data/channels_subs.json` 有 1 条订阅（多空看财报，`v2_06...f2b7@finder`）、
`channels_state.json` 已删（所以下一次真实轮询会把 15 条全当新的、重新写 jsonl）。

### 视频号 ASR：改用讯飞 lfasr（2026-10-06，免费额度最大）

**为什么换**：SiliconFlow 402 余额不足；网关的 `nemotron-3-nano-omni`
采样率全档实测中文覆盖只有 21~54%（音近错，见上）。讯飞是**唯一免费额度
够用**的选项。

**三个讯飞产品要选对，别默认用「语音听写」**：

| 产品 | 时长 | 免费额度 | 适配视频号？ |
|---|---|---|---|
| 语音听写（流式）`iat-api.xfyun.cn/v2/iat` | **≤60 秒** | 创建应用后**默认每日 500 次** | ✗ 视频常超 60s |
| 语音听写大模型 V2 `wss://iat.cn-huabei-1.xf-yun.com/v1` | ≤60s | 按产品页 | ✗ 同上 |
| **语音转写 lfasr** `raasr.xfyun.cn/api/*` | **≤5 小时** | 体验包 **5 小时/30 天**（每账户限 1 次）+ 新用户礼包**最高 50 小时/年** | ✅ **本项目用这个** |

lfasr 支持 `wav/flac/opus/m4a/mp3`、8k/16k、单&多声道、≤500M，
并且有 **`pd=finance` 金融垂域** 和 `hotWord` 热词（≤200 个、单个≤16 字）——
对财报口播是实打实的准确率提升。官方还劝「尽量转 5 分钟以上的音频」，
短音频反而容易排队。

**它是异步任务制**（与 SiliconFlow/Qwen 的同步一次调用完全不同）：

```
POST /api/prepare      form  -> data = task_id
POST /api/upload       multipart(10MB 分片) -> 每片一个 slice_id
POST /api/merge
POST /api/getProgress  -> data 是 JSON 字符串，status=9 才算完成
POST /api/getResult    -> data 是 **双层 JSON 字符串**
```

**签名**（文档给了测试向量，可离线验证、不需要账号）：

```
signa = base64(HmacSHA1(MD5(app_id + ts), api_secret))
```

文档向量：`appid=595f23df ts=1512041814 secret=d9f4aa7e…fd5234`
→ `IrrzsJeOFk1NGfJHW6SkHUoN9CU=`（`test_xfyun_signa.py` 逐字验证 ✅）。
四个易踩变体都算错、已写进断言防回归：MD5 漏 secret、漏 MD5、
key/msg 反了、用 SHA256。

**额度按秒扣 → task_id 必须落盘**。`channels_asr_tasks.json` 以**音频内容
sha1** 为键存 `task_id`/`text`。重跑时：已有 `text` 直接复用（**一个请求都不发**）；
有 `task_id` 没结果就**接着轮询**（不重新提交）；只有 `err_no=26602`
（任务不存在）才重新 prepare。

**双层 JSON**：`getResult` 的 `data` 是「被 JSON 字符串包了一层的数组」，
**必须 parse 两次**，少一次就会当成 dict、拿到空结果（HTTP 200 但没数据那一类）。

**没有开源实现**（AGENTS.md 第 1 条已查）：`api.github.com` 搜
`xfyun lfasr python` → total=0；`讯飞 语音转写 python` 只有 4 个仓库，
最好的 `sonicrhino-client`（1★）是**讯飞听见**不是开放平台，
`lfasr_new_python` 0★ 无 License 且停在 2024。官方只有 Java SDK，
文档明说「开发语言任意」+ 附 Python demo → 自己实现（已说明理由）。

#### 🎉 mp4 抽音轨不需要 ffmpeg —— PyAV 已装

`shutil.which('ffmpeg')` 是 None，但 **`av` (PyAV) 14.1.0 已装**，
自带 FFmpeg 库，能编也能解 → **零新依赖**解决了「视频号下载是 mp4」
这个卡点（`asr_no_decoder` 只在既没 av 又没 ffmpeg 时才报）。

> ⚠️ **PyAV 的坑**：`bytes(frame.planes[0])` 取的是**带 SIMD 对齐填充**的
> 整块 plane，会让时长**虚长约 30%**（实测 3.0s → 3.89s，多 28012 字节）。
> 必须 `[: frame.samples * 2]` 截断，或用 `to_ndarray().tobytes()`。
> 解码完还要 `resample(None)` **flush**，否则末尾丢一截。

#### 自己踩的坑（都靠测试逮到，不是靠「看着对」）

1. **`json.dump` 崩掉整个转写**：checkpoint 里存了 `_now()`（返回
   `datetime`）→ `TypeError: Object of type datetime is not JSON serializable`。
   修法：`json.dump(..., default=str)`，且 `except` 要同时接
   `(OSError, TypeError, ValueError)` —— checkpoint 写不了**不该**让转写失败，
   但**写盘时崩溃必须防住**。
2. **测试用例互相污染**：checkpoint 按音频 sha1 索引，同一个 wav 第二个用例
   直接命中缓存、**连请求都不发**，于是 6 个错误码用例全部「没抛异常」。
   修法：每个用例造**内容不同**的音频。**失败的不一定是要修的代码**。
3. **`git checkout --` 连带毁掉未提交的新配置段**：想撤销一次坏补丁，
   却把整个 `channels:` 段还原没了（git HEAD 里只有 `news.channels`）。
   重新写脚本从 `config.yaml` 取模板、清空密钥重建。
4. **同一招失败两次就换手段**：`edit` 工具在这个文件上失配（**行尾混合**：
   242 个裸 LF + 部分 CRLF）→ 改用脚本；脚本又失配 →
   改成**按行定位**而不是多行 anchor。

测试：`%LOCALAPPDATA%\Temp\opencode\test_xfyun_asr.py`（**43 项**，全 patch
`requests.post`，不联网不烧额度）+ `test_xfyun_signa.py`（签名向量）+
`probe_av_padding.py`（PyAV padding 定位）+ 回归 `test_channels.py` 104 /
`test_channels_offline.py` 69，**共 216 项全过**。

**当前阻塞**：需要用户去 xfyun.cn 注册 → 创建应用 → 添加「语音转写」服务
→ 控制台取 `app_id` + `APISecret`（32 位）→ 领免费体验包。配置项已留好
（`channels.asr.app_id` / `api_secret`，也支持环境变量 `XFYUN_API_SECRET`）。

### ⚠️ 我判错的两处（都被真实请求打脸）

**① `XingChenAGI/XingChenASR-V3.2-Ultra` 是真实存在的模型，我说不存在是错的。**

我当时的依据是 SiliconFlow **官方文档页** `/audio/transcriptions` 的 `enum`
（只列 `FunAudioLLM/SenseVoiceSmall` 和 `TeleAI/TeleSpeechASR`）。用真 key 打
`GET /v1/models` 返回 **97 个模型，其中语音类 9 个**：

```
XingChenAGI/XingChenASR-V3.2-Ultra      ← 用户指定的，真实存在
XingChenAGI/XingChenASR-V3.2
XingChenAGI/XingChenASR-Diarize-V3.0
Qwen/Qwen3-ASR-1.7B
FunAudioLLM/SenseVoiceSmall              （有免费额度）
TeleAI/TeleSpeechASR
Qwen/Qwen3-Omni-30B-A3B-Instruct / -Thinking / -Captioner
```

> **判据**：模型是否存在以 `/v1/models` 的真实返回为准，**文档页的 enum 不完整**。
> 这与「`/v1/models` 列表里有 ≠ 能用」（EOL 模型也在列表里）不矛盾 ——
> **存在性**看列表，**可用性**必须真发一次请求。两个问题要分开验。

**② 评论接口的响应结构我全猜错了**，照 GitHub 文档写的 `data.list[]` /
`next_marker` / `create_time` 实际是（读**工具自带的** `web/docs/API.md`
和 `COMMENT_CAPTURE.md` 才发现）：

| 我原来写的 | 实际 |
|---|---|
| `data.list[]` | **`data.data.commentInfo[]`**（两层 `data`）|
| `next_marker` | **`lastBuffer`** |
| `create_time` | **`createtime`**（秒级）|
| — | `nonce_id`（一级评论必需）、`commentId`、`replyCommentId`、|
| — | `likeCount`、`expandCommentCount`（回复数）、**`levelTwoComment[]`（二级回复）**、|
| — | `data.data.countInfo.commentCount` |

> **教训**：优先读**工具包内自带的文档**（`web/docs/*.md`），不是 GitHub 页面上的
> 二手示例 —— 前者是该版本的真实契约。

**更关键的结构性约束**：评论**走页面 DOM/Store 采集**，链路是
`HTTP API → WebSocket Hub → 注入脚本 → 页面内 finderGetCommentList`。
**微信页面必须停在那个视频上**才返回数据。所以 `comment_meta.ready=False`
必须报「客户端未就绪」，**不能报「该视频 0 条评论」** —— 两者混在一起会让监控
看起来在工作、其实什么都没抓到。

### 雷达 API 前缀不同（容易踩）

- 视频数据：`/api/channels/...`（如 `feed/comment/list`、`contact/feed/list`）
- 雷达目标：`/api/v1/radar/targets`（POST 增、DELETE 删、`{id}/logs`、`{id}/status`）
- 本地浏览记录搜索：`/__wx_channels_api/search?q=`（与 `/api/channels/contact/search` 是两回事）

雷达开关 `radar_enabled` **只认 `config.yaml`，控制台是只读展示**，改完要重启。

### 板块资金流只有 3 家（2026-10-01 确认，别再找第四家）

`同花顺`(主) / `开盘红·财联社` / `新浪` —— 就这三家有**板块级资金流**。
东财的 `push2` 系已全封；akshare 里其余板块资金流函数要么走东财要么走新浪。
要「板块级净流入绝对额」时只能轮询这三家，**不要再设计第四路**。

### Bull/Bear 多空辩论（2026-10-04 接入，TradingAgents-astock 的对抗机制）

`TradingAgents-astock/` 是**参考实现，不是运行时依赖**。主项目只借鉴了它的
「多空辩论」思路，自建了 `stock-advisor/debate.py`（约 260 行），
**没有引入 langgraph、没有 import 它的任何模块**。

| 组件 | 位置 | 说明 |
|---|---|---|
| 辩论模块 | `stock-advisor/debate.py` | `run_debate()` 跑 RiskCritic→Bull→Bear→Judge |
| 接入点 | `paper_trading._decide_one()` | 交易员**第一轮之后**插入辩论，再**复判**一次 |
| 配置 | `config.yaml` 的 `paper.paper_debate` | 默认 `enabled: false` |
| 提示词来源 | TradingAgents 的 `bull_researcher.py` / `bear_researcher.py` / `conservative_debator.py` | A股特色论据（政策市/T+1/涨跌停/游资/解禁/北向）已内建 |

**决策链**（辩论关闭时 = 原行为，成本不变）：

```
分析师 → 交易员① → [触发? no→结束 / yes→ RiskCritic?(按需) →Bull→Bear→Judge → 交易员② → [enforce_rating? → 否决]
```

- 每轮辩论 **3 次 LLM 调用**（+1 次仅高风险票）；触发辩论的票整轮 **6~7 次**
- 触发条件：`action ∈ {buy,sell}`（hold 不辩）且 `confidence >= min_confidence`
- 辩论失败**不阻断**决策：捕获异常后沿用第一轮结果，记 `debate_error`

### Risk Critic：补上被漏掉的风险层（2026-10-04）

**起因是实测反常**：`603718 *ST海利` 跑完 Bull/Bear 辩论后，**Judge 给了 Buy**。
一个带 `*ST`（退市风险警示）的亏损股被判看多 —— 说明 Bull/Bear 的框架里
**没有专门讲 A 股结构性风险**，辩手把它当普通亏损股讨论。

TradingAgents 原版把这块放在 `risk_mgmt/conservative_debator.py`
（T+1 锁定、涨跌停无法出场、ST/退市、质押爆仓），我最初移植时
**只带了 researchers/ 里的 Bull/Bear，漏了这一层**。

**修法**：`run_risk_critic()` 专查 7 类结构性风险（ST/退市、T+1、
跌停出场、股权质押、基本面恶化、流动性、估值陷阱），**只输出风险清单、
不给方向判断**。三处接入：

1. **默认只对高风险票跑**（名称含 ST/*ST/退市，或代码 4/8 开头=北交所），
   普通票不额外花钱（`risk_critic_always: false`）
2. **风险清单注入 Bull/Bear/Judge 三方**，且对 Bull 明确要求
   「不得回避，必须正面回应命中的风险」
3. **Judge 加了「结构性风险 = 否决项，不是扣分项」**：清单里命中
   ST/退市/质押/跌停 任一条就**不得**给 Buy/Overweight。
   同时把原来的 `Commit to a clear stance…` 软化 ——
   原措辞在**推着 Judge 远离 Hold**，是 *ST 股被判 Buy 的帮凶之一

### `enforce_rating`：让辩论有约束力（2026-10-04，默认关）

**实测问题**：600406 / 000858 / 002457 三只 Judge 判 Sell / Underweight / Sell，
**但交易员复判后动作全是 hold** —— 辩论沦为「参考意见」。

`apply_rating_veto()` 的三条设计约束（都不是随意选的）：

1. **只拦与评级方向相反的动作**：Judge 判 Sell 而交易员想 buy → 降级 hold。
   Judge 判 Sell 而交易员想 **hold → 不拦**（hold 已是不加仓，
   `_execute_decision` 的资金/T+1/持仓数约束会兜底）
2. **只对 BEARISH 评级否决**（Sell/Underweight → 拦 buy）。
   看多评级**不产生否决** —— 否则 Judge 喊 Buy 就能逼着加仓，
   那不是风控是放大风险。加仓该由 `max_position_pct` 管
3. **必须写在代码里、且在成交之前**。延续 `stop_loss_max_pct` 那条：
   「不要追高」写在提示词里 LLM 可以自己论证「但前景光明」压过去。

**默认 `enforce_rating: false`** —— 先观察辩论质量是否稳定，再决定是否上闸门。

### `parse_judge_rating` 曾在自由文本里做子串匹配（2026-10-04 修）

原实现兜底是 `if "buy" in text` / `if "sell" in text`，而 Judge 的输出是
**英文推理正文**，里面出现 "sell-off"、"buy the dip" 就会误判；
且 `Buy` 分支在前，`Underweight` 有可能被含 "buy" 的句子抢走。

**修法**：① 优先精确取 `Rating:` 那一行（prompt 强制要求输出）；
② 兜底用**词边界** `\bsell\b` 而非子串；③ **长词优先**
（先 `Underweight`/`Overweight` 再 `Buy`/`Sell`）。

### Judge prompt 泄漏 + 缺 `Rating:` 行（2026-10-04 修）

**症状**：600406/603718 的 `judge_decision` 以 "The user wants me to act
as a Research Manager..." 开头（3447 字符），**没有 `Rating:` 行** ——
`parse_judge_rating` 靠兜底猜，不可靠。

**真因**：nemotron 模型把 prompt 指令当输出开头（同「LLM 不遵守
只输出 JSON」那节的行为抖动）。

**修法**：
1. **prompt 加强**：明确要求「第一行必须是 `Rating: X`」，禁止以
   "The user wants"/"As the Research Manager"/"Let me analyze" 开头
2. **`_strip_prompt_leakage()`**：解析前剥离 prompt 泄漏（检测常见泄漏
   开头标记，从 `Rating:` 行或空行后截取），否则泄漏的 prompt 指令会
   污染前 200 字符的兜底匹配
3. 兜底匹配范围从 400→200 字符（剥离泄漏后正文更短）

测试：`%LOCALAPPDATA%\Temp\opencode\test_leak.py`（600406/603718 真实
泄漏样本 + 正常输出 + 中文冒号）。

### 已知局限

- **Bull 和 Bear 用同一个模型**。TradingAgents 的 `role_llms` 支持给多空辩手配
  不同厂商模型来避免「同源模型互相不反驳」，**本项目没实现**。当前两方同源，
  辩论的独立性打了折扣。真要强化，需要在 `debate.py` 里加 per-role 模型配置。
- **Judge 的推理可能输出英文**（Bull/Bear/RiskCritic 是中文）。评级行 `Rating:`
  能被正确提取，但正文语言不稳定。
- 测试：`%LOCALAPPDATA%\Temp\opencode\test_rc_veto.py`（45 项，
  含「正文含 buy/sell 但评级相反」「ETF 不误判为 ST」「否决只拦相反方向」）。

---

### 模拟盘「决策理由」必须带决策时刻（2026-10-08 加）

**症状**：决策流水只显示 `trade_date.slice(5)` = `10-08`，**同一天几轮决策完全
分不清**，也没法判断理由是不是已经过期（行情早变了）。`/api/paper/trades`
连 `slot` 都没取。

**关键判断：不能拿 `slot` 当决策时间。** `slot` 是 `_paper_loop` 决定跑这一轮的
时刻（`app.py:6250` `slot = now.strftime("%H:%M")`），而行是**决策跑完才 INSERT**。
实测（400 行采样，`created_at AT TIME ZONE 'Asia/Shanghai'` 减 `trade_date+slot`）：

```
min=+0.3min   p50=+11.9min   p90=+22.5min   max=+32.6min   （从不为负）
```

所以 slot 系统性偏早最多半小时。真正该显示的是 `created_at`（决策实际产出时刻）。
另有 **74/674 行（11%）slot 为空** → 必须有兜底，不能只靠 slot。

**优先级**：`decided_at`（rounds 的 entry/exit，本轮给 `rounds()` 新增）
> `created_at`（trades 接口早有）> `slot`（轮次标签，仅兜底）> 都没有则显示破折号。

**第二个坑：时区。** `created_at` 是 `TIMESTAMPTZ`，库里是 `+00:00`
（服务器 `TimeZone = Etc/UTC`），而 `slot` 是本地墙钟。**用正则截 ISO 串里的
`HH:MM` 会拿到 UTC 墙钟，对 UTC+8 的用户偏早 8 小时**（实测决策发生在本地
15:54，截出来是 07:54）。必须 `new Date(iso).getHours()` 取本地时区。
⚠️ 这个 bug 是离线渲染测试逮到的，不是「看着对」发现的。

**改动**：`app.py` `/api/paper/trades` 补 `slot`；`paper_trading.rounds()`
SELECT 补 `created_at` 并在 entry/exit 输出 `decided_at`；`static/index.html`
加 `paperDecisionAt/Day/Age` 三个 helper，决策流水时间列与理由单元格、
每笔交易的买入/卖出理由都带上时刻与「多久之前」。理由摘要里也带时刻，
**不展开 `<details>` 就能看到**。

测试：`test_paper_ui.py`（**34 项**，py_mini_racer 真跑 V8，喂残缺数据断言无
`undefined`/`NaN`/`slot` 误用/时区换算/标签转义）+ `test_paper_e2e.py`（**17 项**，
真请求）。回归：`check_full_js.py` 两个 script 块 `node --check` 全过。

**过程中两次「以为好了其实没好」**：
1. 离线测试脚本报 `helpers 抓不全` / `Unexpected end of input` —— 是**抽取正则坏了**
   （`const esc = s =>` 占 2 个物理行、固定 42 行截断了 helpers），不是页面 JS 坏了。
   已改成**按花括号配平抽取**。
2. E2E 报 `entry 没有 decided_at` —— 服务是**用户在 14:52 用 `uvicorn app:app` 起的**，
   我的启动器因 `[Errno 10048]` 端口被占而退出（harness 只报了 `Exited with code 1`），
   我却只看到「8686 在监听」就以为起来了。实际那个进程比 `paper_trading.py`
   的修改时间**早 3 分钟**，跑的是旧代码。
   → **重启后必须核对「监听 8686 的 PID + 其 CreationDate 晚于文件修改时间」**，
   光看端口在监听不够。

### 分析技法学习：后端闭环 + 前端「📚 分析技法」页（2026-10-07 完成）

从 `sa_mp_articles` 萃取「原文参考了什么指标、按什么阈值判读」，落
`sa_analysis_techniques`，注入 `daily_reports` 的 prompt。

| 组件 | 位置 |
|---|---|
| 模块 | `stock-advisor/analysis_learn.py`（DDL / 粗筛 / prompt / 解析 / 注入）|
| 端点 | `/api/analysis/techniques`、`/techniques/review`、`/learn`、`/config`(GET/POST)、`/injection` |
| 守护 | `sa_analysis_learn`，**12 小时一轮**（`loop_interval_minutes` 默认 720）|
| 前端 | `static/index.html` 的 `tab-analysis` + `loadTechniques()` 系列 |

**双模式是同一个字段的两端，不是两个开关**：`status ∈ active/pending/rejected`。
`mode=ai` 时 `score >= activate_score(7.0)` 直通 `active`；`mode=manual` 一律落
`pending` 等人点「生效」。审核动作只有一个 = 改 `status`，所以 AI 学歪时人一条
请求就能止损，不用改代码不用重启。注入按 score 降序取 `limit=12`、
`max_chars=2600`。`/api/analysis/learn` 默认 `dry_run=true`（LLM 调用花钱）。

### ⚠️ 打分维度定义错了 → 注入的 12 条里 8 条是废的（2026-10-07 修 prompt）

**症状**：`GET /api/analysis/injection` 的 12 条里，排第一（score **10.0**）
是「异构双模型交叉验算」，来自《华尔街顶级阵容联手：如何评测金融大模型》。

实测构成：**LLM 评测方法论 5 条** + **迪拜楼市尽调 2 条** + **一级市场 LP 出资
1 条** + 真正有用的 A 股技法 **4 条**（光模块硅光 / 情景估值 / 护城河组件 /
租赁溢价）。**67% 是废的，而且废的分数更高、排更前。**

**真因（两层，都明确）**：
1. `EXTRACT_SYSTEM` 把 `SCORE` 定义成「这条技法的**技术含量**」——
   一套很厉害但用不到 A 股的方法照样 8~10 分。**模型没执行错，是维度定义错。**
2. prompt 开头虽写「A 股个人自用系统」，但**没有一条规则要求丢弃资产类别 /
   市场不符的技法**，于是迪拜楼市尽调被忠实地萃取了（按 prompt 它做得对）。
   这些文章能过 `looks_like_analysis`（≥5 个指标词）—— 它们含 PE/估值/回测/
   回撤 等词，**粗筛拦不住**。

**修法（只改 prompt，不花钱、不动已有数据）**：
- 加**规则 6**：适用范围必须是 A 股/港股个股的二级市场分析，否则整条丢弃，
  并把「海外房产尽调 / 一级市场募资 DPI·TVPI·GP / LLM·ML 评测方法论」三类点名
- `SCORE` 改成「对 A 股/港股个股分析的**可复用性**」，明写
  「打的是对我做 A 股决策有没有用，不是这套方法技术上厉不厉害」

**没做的（要人决定）**：库里已有的 8 条跑题技法仍是 `active`、**仍在注入**，
新 prompt 只管以后抽的，不会自动修正已抽的。两条止损路：
1. 在「📚 分析技法」页逐条点 ❌ 否决（可逆，改回 status 即可）
2. `POST /api/analysis/learn {"dry_run":false,"force":true}` 重抽
   —— **花钱 + 覆盖现有 54 条，没做，等用户点头**

### 前端三处修复（都能「页面看着正常」地藏着）

| 症状 | 真因 | 修法 |
|---|---|---|
| 通用弹窗 ✕ 点了没反应 | `#gen-modal` / `.modal-close` 写 `onclick="closeModal(false)"`，**全文件只有两处调用、没有定义** → ReferenceError。遮罩那处因 `#gen-modal` 上另挂了 `addEventListener` 兜底才看不出坏，**只有 ✕ 暴露** | 补 `function closeModal(v){ Modal.close(v \|\| 0); }` |
| 真跑但筛出 0 篇时显示「抽出 0 条」 | 后端在 `if not picked` 就 `return`，**早于**设 `dry_run`，该路径只有 `reason` 没有标记；汇总函数只认 `skipped`/`dry_run` → 把「根本没跑」说成「跑了没抽到」 | `_analLearnSummary` 加 `if (r.reason)` 分支 |
| evidence 里的换行被渲染成空格 | HTML 默认把 `\n` 压成空格 = **引文被改写** | `.anal-ev { white-space: pre-wrap; overflow-wrap: break-word }` |

### 验证四层（浏览器全程连不上，所以不靠肉眼看）

| 脚本（`%LOCALAPPDATA%\Temp\opencode\`） | 层数 | 断言 |
|---|---|---|
| `check_index_js.py` | JS 语法 | 抽 2 个 script 块 `node --check` |
| `test_anal_ui.py` | 静态装配 | tab 按钮/section/`TABS`/`TAB_LOADERS` 四处对齐；JS 里 `$('id')` 的 id 都存在；**每个 onclick 引用的函数都有定义**；CSS 类有规则 |
| `test_anal_render.py` | **V8 真跑**（`py_mini_racer`） | 46 条真实数据 + 21 种残缺数据喂 `_analRow`/`_analRenderList`，断言无 `undefined`/`NaN`/字段丢失、按钮互斥、空态文案、汇总文案含 2 条**反例** |
| `test_anal_api.py` | 真请求 | 4 个端点 + 页面确实是新文件 + 状态筛选生效 + `evidence` 非空 + `indicators` 可解析 |

**这一轮 4 个失败全是断言写错，不是代码有洞** —— 记下来免得下次被同类假失败带偏：

1. `esc()` 把 `"` 转成 `&quot;` → 拿**原文**去比**转义后**的输出必然假失败
2. 空态写的是 `textContent`，测试读了 `innerHTML` → 读错属性得到空串，
   看着像「空态没渲染」
3. 断言「整页不许出现『✅ 生效』」—— **状态徽标本来就显示它**；
   要判的是按钮（`✅ 生效</button>`）不是文字
4. 徽标文案是 `❌ 已否决`、按钮才是 `❌ 否决`，**两个标签不同** ——
   我拿按钮标签配 `</span>` 去断言徽标
5. `skipped` 只在早退分支返回，正常路径没有 → 把可选键当必选

> 与 §2「区分 200 和有数据」同源：**断言本身也要独立推导**，
> 不能拿被测代码的字面值当预期（第 3、4 条就是照抄了实现）。

### 从 index.html 抽 JS 做离线测试的两个正则坑

- `const esc = s => .*?;` 会在 **`&amp;` 的 HTML 实体分号**处截断
- `.*?\}));` 根本匹配不到（`esc` 结尾是 `}[c]));`）→ 带 `re.S` 一路扫到
  文件别处，把半截文件吃进来 → V8/node 报 `Unexpected end of input`，
  **看起来像页面 JS 坏了，其实是测试的抽取正则坏了**
- 正确：**按物理行**取 `^const esc = s => [^\n]*\n[^\n]*$`（`re.M`）

### 起服务：`import app` 本身就要 81.8 秒（这次卡了很久）

`python -m uvicorn app:app` 起来后端口一直不监听，进程活着但**只有 1 线程、
4 分钟只用 1.6s CPU**，我一度去查锁、查 DB、查 `ilink_client` 模块级网络调用
（`ilink_client` 全是 `def`，没有模块级 I/O）—— 全是白查。

`python -X importtime -c "import app"` 实测 **`81802424` µs = 81.8 秒**，
且 import 期就打印 `[wx] iLink 会话已建立` / `[holiday] 缺 [2027]` ——
**守护线程在 import 末尾才起，所以 `threads=1` = 还在 import 里**。

→ **判据**：`threads=1` + CPU 几乎不动 = 卡在 import 期，不是卡在端口/锁。
先 `-X importtime` 拿到真实耗时，再决定「等多久」；100 秒就去查端口是查早了。
→ 另：后台 shell 会报 `completed/exit` 而**进程其实还活着**，
**不要拿 shell 的退出状态判断服务死活**，用
`Get-CimInstance Win32_Process | Where CommandLine -match 'uvicorn'` + `netstat`。

---

## 新股/打新「提前发现」渠道实测结论（2026-10-01，**别重查**）

起因：打新额度提醒只依赖 `ipo_calendar` 排期，想问「有没有渠道能在排期公布前发现新股」。

**结论：没有。试过 8 个巨潮关键词 × 4 个时间窗 × 加不加 IPO 辅导层，`fresh` 全程为 0。**

`stock-advisor/ipo_discover.py` 里有一轮完整探测（54 候选 / 8.4s）：

| 分类 | 数量 | 含义 |
|---|---|---|
| `listed` | 38 | 已上市（多是「中签率公告」，申购早结束） |
| `scheduled` | 16 | 已在 `ipo_calendar` 排期里 —— 额度提醒已覆盖 |
| `indirect` | 12 | 参股/控股**子公司**上市 —— 要打新的不是母公司 |
| `post` | 38 | 申购后才发的公告，仅归档 |
| **`fresh`** | **0** | 未上市 + 未排期 + 申购前 + 直接信号 ← 想要的 |

### 三层原因（每层都实测过，不是猜的）

1. **公告时间轴和排期表高度重叠**（结构性）。IPO 从受理到申购，交易所/券商
   必然先披露排期，所以「公告能看到的时刻」排期表也能看到 → 零增量。
2. **北交所没有「排期外待打新」的存量**。曾把 `920238 长鹰硬科` 当成
   「还在排队」（07-13 出招股书 → 推断 10-01 未申购），实际
   `sa_stock_roster.list_date=2026-07-24` —— **招股书到上市只隔 11 天**
   （920176 维琪科技 07-27、920079 乔路通 07-22 同理）。
3. **唯一理论上的早期通道（IPO 辅导）返回 4 年前冻结快照**。
   `ak.stock_notice_report()` 实测 56 条：日期全是 `2022-05-11`（1604 天前）；
   未辅导公司的「代码」是**辅导备案号**（`A21479`/`A16087`/`A12031`/`A17225`），
   不是 A 股 6 位代码。不加过滤时它造出 4 个**假 fresh**（备案号查名册必然
   落空 → 判成「未上市未排期」）。加 `_is_a_share_code` + `_age_ok` 后归零。

### 巨潮关键词的早期信号密度（选错会让整个模块白做）

| 关键词 | 总命中 | 过噪 | **早期** | 早期占比 | 结论 |
|---|---|---|---|---|---|
| 招股说明书 | 7825 | 23 | 23 | 100% | ★主源 |
| **首次公开发行股票申请** | 1038 | 27 | **17** | 63% | ★★金矿 |
| 上市委 | 365 | 9 | 9 | 100% | ★ |
| 受理 | 1101 | 5 | 5 | 100% | ★（要严过滤，见下） |
| 过会 | 1624 | 12 | 1 | 8% | ✗ 噪声大 |
| 注册批复 | 2952 | 6 | 1 | 17% | ✗ 29/30 是已上市公司再融资 |
| 提交注册 | 79 | 0 | 0 | 0% | ✗ |
| 网上申购/发行公告/提示公告 | ~2000 | ~80 | ~0 | ~0% | 仅 post 校验用 |

**「首次公开发行股票申请」最初漏了** —— 它是申购前 1-2 个月最集中的早期
信号（受理→问询→过会→注册都在这个短语下）。漏了它 `pre_far` 从 17 掉到 0。

### 「受理」这个关键词要严过滤

它会捞到**药品/医疗器械上市许可受理**（实测海思科「创新药新适应症上市许可
申请受理通知书」、翰宇药业「司美格鲁肽注射液上市申请获得受理」），
与 IPO 无关。白名单必须含「首次公开发行/股票发行/公开发行股票/北交所」。

### 巨潮 API 本身

```
POST https://www.cninfo.com.cn/new/hisAnnouncement/query
必须 header: Referer(巨潮公告页) + X-Requested-With: XMLHttpRequest
seDate="" = 只返回最新一批（pageSize 封顶 30，传 50/200 都只给 30）
seDate 格式 "YYYY-MM-DD~YYYY-MM-DD"，**有效**（实测能拉到 2025-09 的数据）
column=szse 与 column=sse 返回**完全相同**结果（逐条比对一致）→ 不用查两遍
```

### 那 `ipo_discover` 保留下来干什么

1. **交叉验证排期表**：16 个 `scheduled` 是公告层**独立**抓到的 → 排期表没漏。
   反向查漏能找到「排期有但公告层没抓到」的：实测 3 只
   （`920071` 金钛股份 / `920269` 杰锋动力 / `920289` 华汇智能，都是北交所）。
2. **`indirect` 对持仓有意义**：参股/控股子公司上市，母公司通常确认一次性
   投资收益（万润股份、北陆药业、信德新材、汇川技术…）。
3. **`post` 含新股中签率**，可反推打新收益率。

---

## Python/数据库踩坑（都实测过，别重犯）

### 🔥 服务起不来 40 分钟：`init_db()` 的 DDL 被死连接「接力」占锁（2026-10-07 修）

**症状**：反复重启 8686 都起不来 —— 进程活着、CPU 几乎不动
（**8 分钟只用 1.4s**、线程数 2、内存 59MB）、`netstat` 永远无监听。
用 `-X importtime` 定位到 `daily_reports` 之后就停，一度以为是 import 慢/网络挂。

**真因**（`pg_stat_activity` + `pg_locks` 是判据，不是猜）：

```
7 个后端全在等：
  ALTER TABLE sa_watchlist ADD COLUMN IF NOT EXISTS keywords TEXT NOT NULL DEFAULT ''
  wait_event = Lock/relation，granted = FALSE   ← 没有一个拿到锁
而 keywords 列**早就存在** —— 这些 ALTER 全是空操作，却都要 AccessExclusiveLock

持有锁的：pid=1139082  state='idle in transaction'  已挂 47 分钟
          最后语句 `SAVEPOINT sp_mp`（= stock_discovery._rows() 跑 mp 源时设的）
          本地 netstat 反查 client_port=26763 → **无 ESTABLISHED** = 客户端进程已消失
```

**根因链**：`_discover_loop` 开了事务 → `SAVEPOINT sp_mp` → 客户端进程死掉 →
后端永远等 ClientRead → 事务不结束 → 持 `AccessShareLock` on `sa_watchlist` →
挡死 `ALTER`（要 AccessExclusive）→ `init_db()` 阻塞 → **整个服务起不来**。

**最阴的是「接力」**：干掉第一个堵点，第二个等锁者抢到锁、跑完 ALTER，
然后**自己**也卡成 idle-in-transaction 持锁（它的客户端也死了）——
一个接一个，要循环清理才能解完（实测 2 轮清干净）。

**修法**（AGENTS.md 第 0 条「资源泄漏 / 死锁」授权直接修，不必先问）：
循环终止 `state='idle in transaction' AND xact_age > 120s` 的后端，
每轮复查「仍等 ALTER 数 / 仍超时事务数」，双双归零才停（最多 12 轮）。
脚本 `%LOCALAPPDATA%\Temp\opencode\unlock2.py`。

**为什么安全**：终止前先取证 —— 该事务最后语句是 SAVEPOINT（其后无任何语句），
即**没有已执行的写入**，terminate 触发的 ROLLBACK 不丢数据。

**教训**：
1. **排查「服务起不来」第一步查 `pg_stat_activity`，不是猜 import 慢。**
   `wait_event` 是 `Lock/relation` 时，问题 100% 在锁，与你的代码无关。
2. **`ALTER TABLE ... IF NOT EXISTS` 仍是空操作也要抢排他锁** ——
   「列已经有了所以没问题」是错觉，它照样会死锁。
3. **进程还活着 ≠ 连接还活着**。判客户端死活用
   `netstat -ano | findstr <client_port>`，没有 ESTABLISHED 就是死了。
4. 清理要**循环 + 每轮复查**，单次 terminate 只解一个堵点。

### ⚠️ 列名进了参数位 → 端点从没成功返回过一次（2026-10-07 修）

**症状**：`GET /api/paper/adopted-strategies` 恒 500
（`InvalidTextRepresentation: invalid input syntax for type boolean: "s.enabled"`）。

**真因 ①**：`WHERE (%s = '' OR %s)` 配 `("FALSE", "s.enabled")` ——
**列名被塞进参数位**，psycopg2 渲染成带引号的字面量 `'s.enabled'`，
Postgres 拿它当 boolean 解析 → 炸。而且**两个分支都坏**：
`include_rejected=True` 时渲染成 `('TRUE' = '' OR 'TRUE')`，把 `'TRUE'` 也当字符串。

**真因 ②（修完 ① 又犯的）**：写成 `cur.execute(sql, (not include_rejected,))` →
布尔取反了。`include_rejected=True` 渲染成 `WHERE (FALSE OR s.enabled)`
= **只返回已采纳的**，而库里一条都没采纳过 → **不抛异常、返回 200、恒 0 行**。

**最阴的是**：我当时写的测试断言 `eval_params(False) == (True,)` **照常 PASS** ——
断言是照抄代码算出来的值，测试和代码错在同一个方向，**等于没测**。

**修法**：`WHERE (%s OR s.enabled)` + `cur.execute(sql, (include_rejected,))`。
语义钉死（别再靠猜）：`True → TRUE OR s.enabled`（全都要）、
`False → FALSE OR s.enabled`（只要已采纳）。

**教训**：
1. **列名永远不能走参数位**，参数位只放值。
2. **断言必须从语义独立推导**，不能拿被测代码算出的值当预期。
3. **「不炸但恒 0 行」比 500 难发现得多** —— 必须断言「True 分支必须有行」
   并与独立查出的基准行数比对（`count(*) FILTER (WHERE enabled)`）。

测试：`%LOCALAPPDATA%\Temp\opencode\test_adopted.py`（23 个断言点，用 `ast` 抽出
`app.py` 里的**真实** SQL 字面量与参数表达式对真库跑 —— **不 import app**，
因为 import app 会起整套守护线程并卡在网络调用上）。

### ⚠️ 删 `sa_strategy_def` 重复行：外键是 `ON DELETE CASCADE`，顺序反了丢数据（2026-10-07）

**背景**：策略展示区上线后发现库里 12 行 / 4 个策略，其中
`505366328b8be8ce53ef9575f22a65e0` 一个 article 占 9 行（根因是
`jq_sandbox.save_run` 无条件 INSERT，已改成按 `article_id` upsert）。
页面上就是 9 条几乎一样的行。

**坑（探针实测出来的，不是记得 schema）**：

```
sa_backtest_run.strategy_id -> sa_strategy_def.id   ondelete=CASCADE
```

直接 `DELETE ... WHERE id IN (2..9)` 会把挂在这些行上的 **4 条回测记录一起
级联删掉** —— 而 id=1 当时恰好**没有**回测指标（指标全挂在 6~9 上），
等于「清掉重复行」清成「指标全丢」。表现还不报错，只是展示区的
年化/回撤/夏普变成一片 `—`。

**正确顺序（单事务）**：

```sql
UPDATE sa_backtest_run SET strategy_id = 1 WHERE strategy_id IN (2,...,9);  -- 先迁
DELETE FROM sa_strategy_def WHERE id IN (2,...,9) AND article_id = '...';   -- 后删
```

**动手前必须扫一遍还有谁引用**：`information_schema` 查指向该表的外键（含
`delete_rule`），再逐个 `count(*) WHERE col = ANY(待删id)`。本次扫出 4 个
`strategy_id` 列（`sa_paper_strategy_bindings` / `sa_paper_strategy_exits` /
`sa_position_strategies` / `sa_trades`），引用待删 id 的行数**都是 0** 才动手。

> ⚠️ 扫描 SQL 里的字面 `%` 要写 `%%`，否则 `LIKE '%strategy%'` + 空参数 →
> `IndexError: tuple index out of range`（见下方 psycopg2 那节）。

**结果**：`sa_strategy_def` 12 → **4**；`sa_backtest_run` 4 → 4（一条没丢）；
id=1 从「有判定无指标」变成**判定+指标齐**（年化 16.48% / 回撤 -19.56% /
夏普 0.78）；`sa_backtest_daily` 2634 行未受影响；孤儿回测 0。
23 个断言点全过（`%LOCALAPPDATA%\Temp\opencode\dedup_strategy.py`，
含「先 UPDATE 再 DELETE 顺序」与「回测总数不减」）。

### ✅ 「策略引用到模拟盘」的桥 = `sa_watchlist`（2026-10-07 实现）

用户要「策略可以直接引用，在实际盘或者虚拟盘中引用」。**前提先验了再说**：
`paper_trading.py:1095` 的 `SELECT code, name FROM sa_watchlist ORDER BY code`
就是 `run_decisions` 的候选池 —— 所以把策略的股票池写进自选，等于让它参与
模拟盘，**不改 schema、不改交易逻辑**。

新增 `POST /api/paper/adopted-strategies/{sid}/to-watchlist`，三个实现要点：

1. **必须后端批量**：`fetch_quotes` 是批量接口（一次请求带全部 A 股代码，
   `app.py:791`）。前端逐个调 `POST /api/watchlist` 会变成 36 次行情请求。
2. **单个代码不许中断整批**：`universe` 里混一个格式非法/行情取不到的，
   分开报 `added / already / invalid / no_quote`。
3. **`ON CONFLICT DO NOTHING` 而非 `DO UPDATE`**：`POST /api/watchlist` 是
   「编辑单只」语义、会覆盖 `note/keywords`；批量引用绝不能洗掉用户已有的
   备注和关键词。

**诚实边界（写在页面上，没藏）**：`enabled=false` 时照样可加池子，但
`drives_paper=false` —— 写进去的只是策略**声明的股票池**，买不买/何时买卖仍由
`run_decisions` 的 LLM + 那 6 种**卖出**规则决定。聚宽是**选股**逻辑，
`sa_strategies` 只有 sell kind，两个维度接不上，搬的只有股票池、没有择时。

端到端：`%LOCALAPPDATA%\Temp\opencode\test_to_watchlist.py`（23 个断言点，
`ALL PASS`）—— 首次 `added=31 already=5`、**二次 `added=0 already=36`（幂等）**、
`total=67 distinct=67` 无重复、404/400 边界正确、测完按来源标记
`来自策略「...」股票池` 精确回滚并断言「恢复集合 == 快照集合」。

> 写这条时的**测试纪律**：测试会真实写生产自选，所以 快照 → 跑 → 回滚 →
> **再查一次集合相等**。光看「删了 31 行」不够（删错 31 行也看不出来）。

### 港股 K 线静默全挂：`market_data.tx_symbol` 把港股拼成深市代码（2026-10-04 修）

**症状**：`market_data.fetch_kline` 对港股（00136/09696/01024）抛
`两个源都失败 ... tx: 腾讯 K 线失败: 响应里没有 qfqday/day 数组（code=0）`。
看起来像「腾讯源挂了」或「港股没数据」，**实际是符号拼错**。

**真因**（`market_data.py:69` 修复前）：

```python
def tx_symbol(code):
    if code.startswith(("60","68","51","58","11","50","56")): return "sh"+code
    return "sz"+code        # ← 港股 00136 落到这里，变成 sz00136
```

`sz00136` 是一个**不存在的深市代码**。腾讯对它的响应是
`code=0`（HTTP 200、JSON 合法）但 `data` 里没有该 key → `bars` 为空 →
抛「没有 qfqday/day 数组」。**又是「200 但没数据」那一类**，极易误判成源挂了。

**修法**：港股走 `hk` + 5 位。识别口径与 `paper_trading.market_of` /
`app._market_of` 对齐（5 位纯数字，或已带 `hk` 前缀）。

**关键澄清**：`app.py:734` 的 `_tx_symbol` **一直是对的**
（`re.fullmatch(r"\d{5}", code)` → `hk`），所以**线上实时行情路径没受影响**。
坏的只有 `market_data.py` 这份副本，影响的是 `sa_market_kline` 的港股日K落库
—— 即港股历史数据一直在静默缺失。两个同名函数、不同实现，是这次的坑根。

**验证**：`hk00136/hk09696/hk01024/hk00700` + A股/ETF 11 只，实测 15/15 取到
91 行，港股末根 `2026-10-02`、A股末根 `2026-09-30`。
港股**不需要特殊 param**，用 A 股那套 `,day,,,{n},qfq` 就能拿满（实测 120 根）。

> 教训：改这类「符号/参数拼接」函数前，先 grep 全部调用方。
> 同名函数有多份副本时，**逐个比对实现**，别以为改了 A 就是改了 A。

### LLM 模型 EOL：`/v1/models` 列表不能当可用性判据（2026-10-04 修）

**症状**：所有 LLM 功能（提款建议、模拟交易决策、辩论）集体失败：

```
HTTP 410  The model 'nvidia/nemotron-3-super-120b-a12b' has reached its end of
life on 2026-10-03T09:00:00Z and is no longer available.
```

**真因**：网关（`101.43.25.101:3000`）把模型的 EOL 生效了，而 `config.yaml`
的 `llm.model` 还指着它。**所有走 `llm_advisor.ask()` 的功能同时挂掉** ——
这也是判断「一个模型名会影响多少功能」的依据：ask() 是统一出口。

**三个必须知道的点**：

1. **`GET /v1/models` 仍会列出已 EOL 的模型**（实测 92 个里大量已死）。
   拿到列表**不等于**能用。判据只能是**真发一次推理请求**。
2. **失败模式因模型而异，不能一概而论**：
   | 返回 | 含义 |
   |---|---|
   | `410 ... end of life` | 模型 EOL，确定不可用 |
   | `404 Function '<uuid>': Not found for account '<uuid>'` | NIM deployment 被删；**uuid 每次都不同** → 网关后端是多个 NIM 实例，部分已清理 |
   | `503 Service temporarily overloaded` / `ResourceExhausted: Worker local total request limit reached (16/16)` | 临时过载，**重试可能成功** |
   | `Timeout` | 可能是**冷启动**（550B 级模型实测 129~224s），不是不可用 |

   → **超时和 503 必须重试才能区分**，一次探测下结论会误杀可用模型。
3. 本机出口 IP 与是否代理**不影响**这些判定，别往 IP 限流上想。

**实测可用（2026-10-04，网关 `101.43.25.101:3000`）**：

| 模型 | 首字节 | 备注 |
|---|---|---|
| **`nvidia/nemotron-3-ultra-550b-a55b`** | **~1s** | **已配为默认**；550B 但响应最快 |
| `openai/gpt-oss-20b` | ~1s | 可用 |
| `deepseek-ai/deepseek-v4.1-flash` | 224s | 可用但冷启动极慢 |
| `moonshotai/kimi-k3` | 187s | 同上 |
| `z-ai/glm-5.3-flash` | 129s | 同上 |

已死（勿再试）：`nemotron-3-super-120b-a12b`(410)、`deepseek-v4-pro/flash-0731`(410)、
`minimax-m2.7/m3`(410)、`gpt-oss-120b`(410)、`kimi-k2.6`(404-fn)、
`nemotron-4-340b`(404-fn)、`mistral-large-2`(404-fn)、`glm-5.3`(3×240s 超时)。

**顺带修掉的隐患**：`llm_advisor.DEFAULT_LLM_CONF["model"]` 原本硬编码
`"claude-opus-5"`。本机 `base_url` 指向自建网关，一旦 `config.yaml` 的
`llm.model` 被清空（网页「LLM 设置」保存时容易发生），就会拿
`claude-opus-5` 去打网关 → 必然 404，而报错完全指不到真因。已改为**留空**
并在 `ask()` 里显式拦截，报错直指「`llm.model` 为空 + 当前是网关模式」。

### LLM 不遵守「只输出 JSON」+ 静默降级成 hold（2026-10-04）

**症状**：15 只持仓决策里 **5 只**交易员 JSON 解析失败，全部**静默**降级成
`hold`（信心 1）。不报错、不告警，模拟盘看起来只是「今天比较谨慎」。

**真因不是 `max_tokens` 太小** —— 这是我一开始的假设，被实测推翻：

| max_tokens | 输出字符 | 可解析 |
|---|---|---|
| 1500 | 230 | ✅ |
| 3000 | 278 | ✅ |
| 6000 | 234 | ✅ |
| 12000 | 249 | ✅ |

同一个 prompt、同一个 `max_tokens=1500`，**有时直出 230 字符 JSON，
有时先写 4000+ 字符英文思维链**、写到 `Final JSON` 就撞上限被截断
（JSON 一个字都没输出）。**是模型行为抖动，不是配置问题** ——
所以调大 `max_tokens` 解决不了，只能在**解析侧**兜底。

两种截断形态（都要能救）：

- **短截断**（123~293 字符）：JSON 写到 `reasoning` 中途断掉，字段基本完好
- **长截断**（4000+ 字符）：全是思维链，**没有任何字段可救** → 必须返回 None

**修法**：`paper_trading._extract_json()` 加第 4 级容错
`_extract_truncated_json()` —— 用正则逐个捞标量字段。**两条纪律**：

1. **只取标量**（`action`/`confidence`/`target_value_pct`/`stop_loss_pct`）。
   `reasoning` 是自由文本、几乎总是被截断的那部分，捞不到就置空。
   宁可少一个字段，也不要因为最后一条 reasoning 没写完就丢掉整个
   `action=buy/sell`。
2. **必须拿到合法枚举的 `action` 才算成功**，否则返回 None。
   这里踩过一个自己写的 bug：`[^",}]+` 会匹配到**空串**，于是 `{}` 造出
   一个「有 action 的假 dict」。修法是加 `and m.group(1).strip()`。

**实测**：真实失败样本抢救回 2 只（`603366`、`01024`，都正确恢复
`confidence: 8`），另外 3 只本就没有可救内容、正确返回 None。
原有 7 类输入（围栏/裸 JSON/带前言/单引号/非 JSON/空串/双 JSON）**零回归**。
测试：`%LOCALAPPDATA%\Temp\opencode\test_trunc.py`（含「拒绝样本不得瞎猜」）。

> **静默降级比报错危险**：503 会报错你看得见，「解析失败 → hold」看不见。
> 任何 `_validate_decision(...) or {"action":"hold"}` 的兜底都要计数上报，
> 否则「模型没按格式输出」会伪装成「模型很谨慎」。

### `threading.Lock` 非重入 → 静默死锁

`with _lock:` 里再 `with _lock:` = **进程无输出、无异常、永久挂住**。
表现为「脚本跑满超时但什么也没打印」。`ipo_discover.discover()` 就这么
把我骗了半小时 —— 我先后误判成「akshare 上游卡住」「IP 被限流」
「两个数据源抢出口 IP」，全错，**真因就是锁非重入**。用 `RLock`。

> 以后遇到「无输出、无异常、一直超时」，**第一个怀疑锁**，不是怀疑上游。

### psycopg2 的 `IN (%s)` 传 list 会炸

```python
cur.execute("... WHERE code IN (%s)", (codes,))
# IndexError: tuple index out of range   ← psycopg2 索引不了「1 个 list 对多个占位符」
cur.execute("... WHERE code IN %s", (codes,))
# SyntaxError: syntax error at or near "ARRAY"  ← 不会帮你展开成 ARRAY
cur.execute("... WHERE code = ANY(%s)", (codes,))
# ✅ 正确
```
`list_date` 是 `date` 列，比较必须 `list_date::text` 或 `::date`，
传 ISO 字符串报 `operator does not exist: date = text`。

**字面 `%` 必须写 `%%`**（2026-10-02 实测，`IndexError` 很难联想到是转义问题）：

```python
cur.execute("... WHERE url LIKE '%~%'", ())     # IndexError: tuple index out of range
cur.execute("... WHERE url LIKE '%%~%%'", ())   # ✅ 正确
```

同理还有 `_query` **只做 fetchall、finally 里直接 `conn.close()`，没有 commit** ——
拿它写 UPDATE 会静默不生效，写库要自建连接并显式 `conn.commit()`。

### 不要在工具模块里 `from app import get_conn`

它会 import 整个 `app.py`，连带启动所有 daemon（iLink 会话、holiday 日历构建…），
**实测光这一步 46.8 秒**。工具模块用 `ipo_quota._query`（只读 `.env` + psycopg2）。

### 微信 `/s/` 短链的 `~` → `_`：一个字符毁掉 30% 的正文

微信 `mp.weixin.qq.com/s/<token>` 的 token 是 **base64url**（22 字符），字母表里
**没有 `~`**。抓取链路上某处把 `_` 损坏成了 `~`，后果是：

- `~` 版 URL 恒返回 **HTTP 200 + `参数错误`**（换各种 UA 都一样），
  看起来像「源活着但今天没数据」—— 和上面那个 push2 假失败同一类陷阱
- `_` 版才 302 到 `<token>?nwr_flag=1`，能取到标题和正文
- 64 个字符逐个扫：**只有 `_` 有效**
- 概率模型吻合：P(22 字符里至少含 1 个 `_`) = 1 − (63/64)^22 ≈ **29.3%**，
  实测 55/183 ≈ 30%

**修复**：`wechat_mp.py::_normalize_mp_url()`，只对 `https://mp.weixin.qq.com/s/`
前缀的 `url` **和 `guid`** 做 `~`→`_`。

**踩过的两个坑**：

1. **一开始只改 `url`、刻意保留原始 `guid`**（理由是「guid 是去重键，别动」）——
   结果远端 WeRSS 的 URL 修好后开始下发 `_` 形式，`ON CONFLICT (source_id, guid)`
   判成新文章，**同一篇插出两行（34 组重复）**。
   → `url` 和 `guid` 必须**同口径**，且要迁移库里的历史 guid（先删重复再改，
   否则 `UNIQUE (source_id, guid)` 直接 23505）。
2. WeRSS 自带的 `scripts/fix_weread_mp_urls.py` 注释声称做这件事，但它的判断条件
   `"~" in new_token` **永远不成立** —— 它是个 no-op，别指望它能修。

**结论口径**：判断「某篇抓没抓到正文」必须看 `content_html`/`content_text` 是否非空，
**不能只看 HTTP 200**。修完后 WeRSS 侧从 117/183 抓回到 **183/183**。

### WeRSS（101:8001）的登录密码与 `/rss` token

| 项 | 在哪 | 备注 |
|---|---|---|
| Web UI 登录 | `admin` + `/opt/we-mp-rss/.env.bak` 里的 `WERSS_PASSWORD` | **`.env` 当前那个值登不上** |
| `/rss` token | stock-advisor `config.yaml` → `mp.auth` | 不带 → `401 RSS requires token` |

`WERSS_PASSWORD` **只在首次初始化时用来建账号**。`.env` 在 09-29 改过一次，但
`users.password_hash`（bcrypt）匹配的仍是 09-27 的原始密码 —— **改 `.env` 不会同步
已有账号**。核对要用 `bcrypt.checkpw`（容器内 `/app/env_x86_64/bin/python3`，
系统 `python3` 没装 bcrypt），**别反复试登录**：接口回
`202 + {"code":40101,"message":"用户名或密码错误，您还有N次机会"}`，会锁号。

### ⚠️ WeRSS 停更根因：`message_tasks` 的 cron 是 `*/5`，把账号配额打爆（2026-10-03）

**症状**：10-03 之后公众号文章停更，最后一次成功采集停在 `00:20`，之后 11 小时零产出。

**根因**：`message_tasks` 里那条任务的 `cron_exp = */5 * * * *`，挂 12 个号。
`weread_mp.py` 的 `get_Articles(MaxPage=1)` **一次触发 = 1 个 `/api/mp/cover`**，
无翻页、无重试 → 每天 `288 触发 × 12 号 = 3456 次`。12 小时内观测到
**8471 次 `-2014`/499**。

**这条任务不是官方预设，也不是正常录入的**，三条佐证：
- 名字叫 `111`（测试痕迹）
- `created_at` / `updated_at` 全是 `None`（SQLAlchemy 没维护 → 非 ORM 创建）
- 10-02 00:56 的备份里已是 `*/5`；它只覆盖 12 个号，而第 13 个号
  `算力交易网`（10-01 23:23 加的）**从没被加进这个任务**

### `-2014 请求频率过高` 是**账号级**配额，不是 IP 级 —— 我判断错过

我最初断定「101 出口 IP 被限流」，用户连问三次才纠正。**实测证据**：

| 出口 IP | cookie (`fmO0LRRM`) | bookId | 结果 |
|---|---|---|---|
| 101（`101.43.25.101`） | 同一个 | 同上 | 499 `-2014` |
| 本机（`59.34.155.130`） | 同一个 | 同上 | 499 `-2014` |

**换 IP 结果完全一样 → 排除 IP 限流。** 推论：**把刷新/采集挪到本机不能绕过限流**，
「本机定时续期 + 推送 101」这条路从根上就不成立，别再设计。

**`-2014` 还是伪装码**：cookie 被截断（丢 `wr_rt`）时报的是 `-2014`，
还原成完整 cookie 后立刻变成 `-2013 鉴权失败`。所以看到 `-2014` 要先排除鉴权。

判据速查：

| 现象 | 含义 |
|---|---|
| 无 cookie | `401 -2010 用户不存在` ← 服务端正常区分身份 |
| 坏 cookie | `401 -2012 登录超时` |
| **配额耗尽** | cover 返回 `499 -2014`；articles 返回 `-2041`；auth/verify 误报 `-2012` |
| `/web/mp/cover` | `-2013 鉴权失败` |
| `/web/mp/articles` | `200 + -2041` = 配额耗尽，**恢复后不报此码** |

**「无 cookie 拿到 401」是很有用的基线**：它证明服务端活着且在正常鉴权，
`499` 就是这个账号的具体状态，不是服务挂了。

#### 判别「登录态失效」vs「配额/接口不可用」：好 cookie vs 坏 cookie 对照

单看一个错误码**无法**区分鉴权失败和配额耗尽 —— `-2014` 和 `-2012` 都可能出现在
「cookie 坏了」和「配额没了」两种情形。可靠做法是**造一个必然无效的 cookie**
（`wr_skey=ZZZZINVALID0` + `wr_vid=999999999`，其余字段保持不变），
拿两个 cookie 打**同一批接口**对比：

| 观察到的模式 | 结论 |
|---|---|
| 某接口在好 cookie 下有数据、坏 cookie 下报错 | **该接口能用**，且服务端在正常区分身份 → 登录态有效 |
| 好/坏 cookie 都报同一个码 | 该码**与登录态无关**（是接口级限制或配额）|
| 某接口在好 cookie 下也失败，但错误码与坏 cookie **不同** | 该接口对好 cookie 是**另一种拒绝**（如 `-2041`），不是鉴权问题 |
| 所有接口对好 cookie 都失败 | 才可能是登录态真的失效 |

判登录态的**单一决定性接口是 `/web/shelf/sync`**（书架同步）：它返回真实书目数据。
实测（2026-10-03）当前 cookie 返回 **33 本书** → 登录态有效。

> 这个手法比「打一个接口看错误码」强得多，因为它自动排除了「服务端挂了」
> 「这个号没权限」「接口已废弃」等混淆项。**任何遇到「E401/-2012/-2014」的排查
> 都该先做这个对照再下结论** —— 我这次就是靠它才发现列表接口是 `-2041`
> （与鉴权无关），而不是之前推测的「配额问题会一起解决」。

### 配额没有任何公开数值 —— 不要相信任何声称知道确切数字的说法

- 官方文档只说「有访问频率限制，全文请求间隔建议 `WEREAD_CONTENT_INTERVAL >= 2` 秒」
  （`/app/docs/weread-mp.md:43`），**没给数字**
- 响应头里**没有** `X-RateLimit-*`、没有 `Retry-After`。`set-cookie` 只有
  `wr_skey=xxx; Max-Age=2592000`（30 天）
- 响应体里有 `data.errlog`（如 `C6ey5Vr`），是服务端 trace id，不含配额信息

**唯一可靠的观测是入库时间分布**，它直接暴露了配额的形状：

```
2026-10-03     9 条   00:14:41 ~ 00:20:01   ← 全部挤在 20 分钟内
2026-10-02    12 条   00:00:00 ~ 00:15:00   ← 同上
2026-09-30   120 条   00:45:01 ~ 22:15:05   ← 那天还在补抓历史，节奏失真
```

**午夜重置 + 重置后 20 分钟内被烧干**，是每日配额重置的典型指纹。
下限可能只有 **30~60 次/天**（00:00~00:20 期间 `feeds.update_time` 只有 5~6 个号
被刷新到 10-03，其余仍停在 10-02/10-01 → 成功的远少于理论上的 60 次）。

### `/api/mp/cover` 只返回「最新一篇」→ **旧代码**的轮询频率直接决定丢多少文章

旧版 `weread_mp.py` 的 `get_Articles(MaxPage=1)` 只调一次 `cover`，返回该号**最新**一篇，
已入库就跳过。**两篇间隔内发 3 篇 → 只能拿到最新的 1 篇，另外 2 篇永久丢失**。

⚠️ 这条针对的是 **1.4.5 旧版**。新版 fork（`126993c8`）的主路径是
`/web/mp/articles` 列表接口 + 翻页，能补抓多篇 —— 同样的 `-2041` 在配额耗尽期
会出现，但恢复后接口正常，264 条/7 秒已实测。

所以降频有真实代价，不能一味降到最低。`wechat_mp._normalize_mp_url` 修好的
`mp.weixin.qq.com/s/<token>` 链接只能解决**已入库文章的正文**，救不回没入库的。

### 对照实验：证明「不是自写补丁的锅」（2026-10-03）

部署了第二个零补丁实例 `weread-clean`（同镜像 `we-mp-rss:local`，
md5 `4785ba53` = 镜像原版，`SA_` 标记 0），和生产用**同一个 cookie** 同时请求：

| | 零补丁 | 已补丁 |
|---|---|---|
| cookie skey | `fmO0LRRM` | `fmO0LRRM` |
| `_sa_quota_guard` | **False** | True |
| 结果 | FAIL 499 | FAIL 499 |
| 错误文本 | `WeRead MP API error 499: mp cover returned HTTP 499` | **一字不差** |

**零补丁实例报出完全相同的错误 → 补丁被实测证伪。**

> 做这个对照的三个前置检查，缺一个实验就无效：
> 1. **同镜像**：`we-mp-rss:local` 建于 2026-09-27，早于所有补丁 → 镜像本身干净。
>    用 `docker run --rm --entrypoint sh <image> -c "md5sum ..."` 验证基准。
>    不要拉新镜像：`/opt` 只剩 6.1G（84% 已用），镜像 4.34GB 有撑爆磁盘风险。
> 2. **零 API 消耗地确认「当前实际生效的调度任务」**：`docker exec` 起的是**另一个
>    Python 进程**，apscheduler 的 job 在运行中进程内存里，**外部读不到**。
>    `docker logs` 是**累积**的（跨多次 start），会把历史上加载过的表达式混进来 ——
>    实测同时看到 `0 */3` / `0 */4` / `*/59`，据此判断会出错。
>    唯一可靠办法：**`docker rm -f` + `compose up` 重建容器**清空日志后再读。
> 3. **别让对照组被饿死**：配额账号级共享，生产高频组 `*/30` 重置后 1 小时就吃掉
>    6 次。对照组若按 `0 */4`，04:00 触发时池子早被吃光 → 会因「抢不到」而误判成
>    「零补丁也坏了」。改成**每天 00:10**（重置后 10 分钟）才归因得到代码。

### 最终配置（交易时段 5 分钟一次，2026-10-03 22:55 改）

| 项 | 值 | 持久化位置 |
|---|---|---|
| **单任务** | `*/5 9-14 * * 1-5` = 周一~周五 9:00-14:55 每 5 分钟 | 宿主 SQLite |
| 覆盖 | **14 个号全部**（不再分档）| 宿主 SQLite |
| `WEREAD_MP_MAX_PAGES` | **1**（每号每次只取最新 1 页）| compose 环境变量 |
| 零成本报告 | `weread_report.sh`，每 3 小时 | 只读日志和库 |

**成本**：72 触发/天 × (1 shelf + 14 号 × 2 请求) = **2,088 请求/天**。
比 `*/5` 全天的 3744 次/天降了 44%，但比之前的 97 次/天高 21 倍 —— 这是「交易时段
5 分钟新鲜度」的代价。16:00 补积压那次（264 篇/6 号/7 秒 ≈ 300 请求）说明
**列表接口翻页在稳态下很快**，但补抓日会打光配额。

`*/5 9-14 * * 1-5` 覆盖 9:30-11:30 + 13:00-15:00 交易时段，**含午休 11:30-13:00**
（每天多 12 次空跑，但午休发的文章也能抓到）。

> **cron 配置在宿主 `/opt/we-mp-rss/data/db.db`（volume），容器重建也不丢。**
> 但 **WeRSS 用 Redis 持久化任务**，调度器从 Redis 加载 —— 改 DB 后必须
> **删 `/app/data/redis/dump.rdb` 再重启**，否则旧任务还在调度器里。

### ⚠️ 改 message_tasks 必须同步清 Redis（踩过）

WeRSS 启动时 `加载持久化数据成功 .../redis/dump.rdb`，调度器从 **Redis** 读任务，
不是从 SQLite。所以：

```
改 DB（SQLite）→ 重启容器 → 调度器加载的还是 Redis 里的旧任务
```

**正确做法**：
```bash
docker stop we-mp-rss
rm -f /opt/we-mp-rss/data/redis/dump.rdb   # 备份后再删
docker start we-mp-rss                        # Redis 从 DB 重新初始化
```

实测：改 DB 后不清 Redis，调度器会同时加载「新的 1 条 + 旧的 3 条」= 5 个 cron，
旧任务继续按 `0 */4` / `0 */8` / `0 */12` 跑，配额被双倍消耗。

> 凡是「改了配置但没生效」，**先查 Redis**，不是先查 DB。

### 叠层构建 `COPY . /app` 会丢掉可执行位（踩过，灰度才抓到）

**换版原因**：原先跑的 `we-mp-rss:local` 是从 `ghcr.io/rachelos/we-mp-rss:1.4.5`
（2026-08-13）叠层构建的，`weread_mp.py` 只有 **cover 单篇兜底**逻辑。
换成自己的 fork（`https://github.com/hellostronger/we-mp-rss.git`，HEAD `126993c8`，
2026-09-24，与上游 `status = identical` 即纯净镜像）后拿到两个关键提交：

| 提交 | 内容 |
|---|---|
| `c0dc66e9` (09-04) | 公众号采集**主路径改为 `/web/mp/articles` 列表接口增量补抓多篇**，cover 降为兜底 |
| `c480451b` (09-23) | 链接 token `~`→`_` 修复（就是上面「微信 `/s/` 短链」那节记的坑，**上游已修**）|

实测行为差异（同一 cookie、同一号、同一时刻）：

| | 旧 1.4.5 | 新 fork HEAD |
|---|---|---|
| 请求序列 | `/api/mp/cover` | `/web/shelf/sync` → `/web/mp/articles` → `/api/mp/cover`（兜底）|
| 列表接口能否多篇 | ❌ 只 cover 单篇 | ✅ 翻页补抓（16:00 实测 264 篇/7 秒）|
| 配额耗尽时的伪装 | `499 -2014` | `-2041` + `-2014` + auth/verify 误报 `-2012` 三码齐出 |

**⚠️ 修正（2026-10-03 16:00 实测推翻了以下判断）：`-2041` 不是「接口不可用」，是配额耗尽时的第二种伪装码。**

以下是我 15:20 测出来的「配额耗尽期单次快照」，但据此得出「列表接口不可用」是错的：

| 接口 | 配额耗尽期 15:20 | **配额恢复后 16:00 实测** |
|---|---|---|
| `/web/shelf/sync` | 200，33 本书 | 200，正常 |
| `/web/book/info` | 200 「登录超时」| 200，正常 |
| `/web/mp/articles` | 200 **`-2041`** | **264 篇文章 7 秒内入库，正常** |
| `/api/mp/cover` | **499 `-2014`** | 264 篇成功，`499` 和 `-2041` 同时消失 |

**决定性证据**：16:00 高频+中频组共 6 个号触发采集，7 秒内入库 **264 篇**真实文章，
同时 `499/-2014`、`-2041`、登录态失效三个错误**全部消失**（日志 0 条）。
这三个码是同一件事的三种伪装：**配额耗尽时 cover 返回 -2014、articles 返回 -2041、auth/verify 误报 -2012 登录态失效**。

**所以新代码的好处是实实在在的**：列表接口翻页能一次补抓多篇，16:00 那一轮
264 篇/6 号/7 秒，只有 ~44 次请求就完成了——比旧版cover 模式（每号每次 1 篇）快两个数量级。

但代价是：当 quota 满血时，每次触发 = `1（shelf/sync）+ N×翻页次数` 请求。
按当前分档，高峰每触发 7 请求（3 号每号 2 页），97 次/天是偏保守值，
不宜放宽到 >150/天。

### 叠层构建 `COPY . /app` 会丢掉可执行位（踩过，灰度才抓到）

换版时 `COPY . /app` 把镜像里 mode **755** 的 `/app/start.sh` 覆盖成宿主 clone 的
**644**，entrypoint 的 `exec /app/start.sh` 立刻报：

```
/usr/local/bin/wrss-entrypoint.sh: 18: exec: /app/start.sh: Permission denied
→ 容器 ExitCode 126，Restarting 循环
```

**只有灰度部署能抓到 —— 直接换生产就是当场宕机。**
`Dockerfile.local` 里已加 `chmod +x` + `test -x /app/start.sh` 自检。

顺带发现：git 里这些 `.sh` 的 mode 本来就是 `100644`（上游没设 +x），
镜像里的 755 是构建时另加的 —— 所以**从源码 clone 再 COPY 一定会掉权限**。

### `comm` 两边 sort 的 locale 必须一致（踩过，输出是纯垃圾）

对比容器与源码的文件清单时：

```bash
docker exec ... find ... | sort > a      # ← 这个 sort
( cd src && find ... ) | sort > b        # ← 和这个 sort 可能不是一个 locale
comm -23 a b
```

结果 **`comm: file 1 is not in sorted order`，而 comm 仍继续输出** ——
两个方向都列出**完全一样的 34 个文件**（其中包含 `core/wx/model/weread_mp.py`
这种明显两边都有的文件）。差点据此判断「fork 删了又加了一堆文件」。

**两侧都加 `LC_ALL=C sort`，并用 `sort -c` 校验后再信 `comm` 的结果。**

### 换版前必查的五项（缺一项就可能构建成功但运行时报错）

1. `requirements.txt` 与容器内是否**完全一致** → 决定能否只叠源码而不重跑 pip
   （一致才敢叠层；不一致必须走 fork 自带 Dockerfile 的全量构建）
2. `.py` 清单：fork **删除几个 / 新增几个** → 删除数 >0 意味着叠层会残留已删文件
3. `migrations/` 有无新增 → 有则要搞清升级路径
4. `config.yaml` 会不会被 `COPY` 覆盖（源码里只有 `config.example.yaml` 就安全）
5. 前端产物位置：`.dockerignore` 排除了 `dist`/`node_modules`，
   若镜像里的已构建前端在被覆盖的位置，UI 会空白
   （本次 `web_ui/index.html` 与 `static/index.html` 两边都有，安全）

**为什么用叠层而不是 fork 自带的 Dockerfile**：后者是
`FROM ghcr.io/rachelos/base-full:latest` 的**全量构建**，要新拉数 GB，
而当时 `/` 只剩 7.0G（82% 已用）。叠层只增加 2 层。重建步骤见
`/opt/we-mp-rss-src/README.rebuild.md`；回滚用镜像 `we-mp-rss:rollback-1.4.5`
+ `docker-compose.yml.pre_fork_20261003_150739` + `backup/db_pre_fork_20261003_150739`。

### `/opt/we-mp-rss` 下**没有源码**，compose 已无 `build:` 段

原先只留了 `Dockerfile` / `docker-compose.yml` / `.env` / `data/`（14 个条目），
没有 `.git`、没有 `core/`。`Dockerfile` 里有 `COPY . .`，
所以**任何 `--build` 都会失败**。已从 compose 移除 `build:` 段并注明原因。
源码现在在 `/opt/we-mp-rss-src`（clone 自 `hellostronger/we-mp-rss`）。

### 「假设成立」不等于「改完了」（三次都踩）

- `retier_prod.sh` 按**改后**的任务名去匹配，但库里存的是**改前**的名字
  （`高频组 (每30分钟)`），**0 行命中**，脚本却照样打印「合计 75 次/天」。
- 串行 UPDATE 同名列撞车：`*/30 → 0 */2` 改完后，高频组的新 cron 正好是下一条
  要匹配的 `0 */2` → **被二次处理**。而 `assert not left`（把 `0 */2` 列为残留旧值）
  **误报**了，它同时也是合法新值。
- 探针 `werss-b` 的 articles 表是从生产 `db.db` 复制来的 192 篇历史，
  报告里「探针累计 192 篇」**无法区分新代码采到的和继承来的** → 已清空为 0。

> **凡是脚本打印「合计 N」「已保存 N」，必须同时有 `assert` 或 `SELECT` 复核兜底。**
> 打印成功不等于写入成功 —— 见 AGENTS.md 开头第 2 条。
> 另外**灰度实例的数据目录要从生产「复制」而非共用**，且接入的采集任务
> 要立刻 `status=0` 禁用，否则测试期间就在抢配额。

### WeRSS SQLite 的列很容易选错（`content` 才是原文）

库在容器内 `/app/data/db.db`（SQLite），表 `articles`。三列长得都像正文：

| 列 | 实际是什么 | id=5236 长度 |
|---|---|---|
| **`content`** | **原文完整 HTML** ← 本地要的是这个 | **183,964** |
| `content_html` | 清洗后的短版（只有原文 1/10） | 18,895 |
| `description` | 203 字预览 | 203 |

本地 `sa_mp_articles.content_html` ← 远端 `articles.content`，**4 篇实测长度
逐字节相同**（183964 / 132098 / 100917 / 87183）；`content_text` 由
`wechat_mp._html_to_text(content)` 派生，远端没有对应列。

**为什么会有 21 篇本地缺正文**：RSS 只推最近 N 篇，旧文早出了 feed 窗口，
`sync_source` 的 backfill 永远轮不到它们。远端其实 21/21 都有
（实测 `有正文=21 空=0 库里没有=0`），所以要**直接从 SQLite 导出回填**：

1. 远端脚本 `SELECT title,url,content FROM articles WHERE url=?` → base64 JSON
2. 本地用 `wechat_mp._html_to_text()` 生成 `content_text`
3. **单向 UPDATE**：`WHERE url=%s AND COALESCE(content_html,'')=''`
   （与 `sync_source` 同规则，绝不覆盖已有正文）

回填后 183/183 全部有正文。`wechat_mp` 刻意不 `import app`（见 33 行注释），
所以工具脚本可以直接 `import wechat_mp` 拿 `_html_to_text`。

### PowerShell 的 `2>&1 | Where-Object { $_ -is [string] }` 会吃掉异常

stderr 进管道是 `ErrorRecord` 对象，**不是 `[string]`**，全被过滤掉 →
Python 明明抛了 traceback，我只看到「Exited with code 1」没有输出。
查 Python 异常时**不要加这个过滤**，直接 `2>&1 | Out-String`。

---

### 测试环境陷阱

- `notifier._db_exec` 内部 `from app import get_conn`，会**重新执行整个 app.py 并再起一套 daemon**，
  导致测试里出现抢游标。测试必须自己 `patch` 掉 `notifier._db_exec`。
- 看 git 提交内容用 `git cat-file -p` 并显式解码。PowerShell 里
  `git show > file` 再 `-match` 会得到 `System.Object[]`，导致误判。
- 测试脚本统一放 `%LOCALAPPDATA%\Temp\opencode\`，不要写进仓库。
- 多行中文脚本**不要用 `python -c "..."`**（PowerShell 会把换行和引号吃掉，
  表现为无输出 exit 1）。写成 `.py` 文件再跑。
- **别用 `python x.py > file` 落中文输出**（2026-10-07 白花 4 轮才认出来）：
  PowerShell 的 `>` 写 UTF-16，Python 按 `PYTHONIOENCODING` 写字节，两者一交叉
  **ASCII 活、CJK 全毁** —— 结果 `PASS ...（1/4）` 被我读成 `（4/4）`，
  然后去查「为什么和数据库对不上」，其实是输出坏了不是数据坏了。
  **要落盘就让 Python 自己 `open(..., 'w', encoding='utf-8')` 写**，
  或者干脆让脚本**只打 ASCII**（`DIAG nonempty=%d/%d`）。
  一旦怀疑显示不对，**别做编码取证**（试 utf-8/gbk/utf-16 三种解码只会越查越糊），
  直接改脚本重打一行数字 —— 一次就准。
- **`> 0` 是弱断言**：`chk(len(nonempty) > 0)` 只要混进一条非空就过，而且
  「几个非空」是拿被测 SQL 自己的返回算的 = 自证。正确做法是**独立查一条基线**
  （如 `count(*) FILTER (WHERE universe::text <> '[]')`）当预期。
  同理：源码里 `chk(` 的**出现次数 ≠ 实际执行条数**（有的在 `if` 分支里），
  写「N 项全过」前先数清楚是哪种。

### 提交范围

工作区里混有用户自己的 WIP（`jq_sandbox.py`、`sandbox_runner.py`、`valuation_data.py`、
`scripts/*.py`、`_dirty_backup_*.json`、`clade`）——**不要碰、不要提交**。
