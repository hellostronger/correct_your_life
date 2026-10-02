# AGENTS.md

本文件的规则对所有会话生效。与用户口头指示冲突时，以用户当次指示为准。

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
- 本机直连出口：`121.32.254.148`（广东电信）。无系统代理、无 VPN
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
  只绑 `127.0.0.1:8080`（不暴露公网），外部走 SSH 隧道
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

### 板块资金流只有 3 家（2026-10-01 确认，别再找第四家）

`同花顺`(主) / `开盘红·财联社` / `新浪` —— 就这三家有**板块级资金流**。
东财的 `push2` 系已全封；akshare 里其余板块资金流函数要么走东财要么走新浪。
要「板块级净流入绝对额」时只能轮询这三家，**不要再设计第四路**。

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

### 提交范围

工作区里混有用户自己的 WIP（`jq_sandbox.py`、`sandbox_runner.py`、`valuation_data.py`、
`scripts/*.py`、`_dirty_backup_*.json`、`clade`）——**不要碰、不要提交**。
