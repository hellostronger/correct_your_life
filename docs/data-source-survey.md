# A 股板块/资金流数据源：16 个开源方案实测报告

> 调研时间 2026-09-30 ~ 2026-10-01。所有结论均为**本机/101 实际发请求**得出，
> 不采信 README 宣称。每条都记录了失败时的确切异常。
>
> 调研起因：项目自研的东财直连采集器在 2026-09-21 被 WAF 掐断后静默停更 9 天，
> 页面照常渲染 9 天前的旧数据。

## 零、两个「看起来失败其实不是」的反例（本项目自己踩的）

调研过程中我犯过一个值得写下来的错误：**把一个参数名写错导致的失败，
误判成「数据源被 WAF 封禁」，并把它写进了 `AGENTS.md` 和提交信息。**
这两个反例比后面的结论更值得记住。

### 反例 1：`HTTP 200 + data:null` 被我误判成「push2ex 已封」

| | 请求 |
|---|---|
| 我写的 | `?d=20261001&ut=fa5fd1943c7b386f172d6893dbfba10b` |
| 正确的 | `?date=20261001&ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=10000&sort=fbt:asc` |

差异只有两处：**参数名是 `date` 不是 `d`**，`ut` 是涨停池专用 token。
用 `d=` 会命中一个仍然存在但语义不同的旧接口，返回：

```json
{"rc":102,"rt":1,"svr":177618782,"lt":2,"full":1,"data":null}
```

**HTTP 200、JSON 合法、`data` 为空。** 任何「检查状态码」「检查 JSON 非空」
的逻辑都会得出「源活着，只是今天没数据」，然后进一步误判成被封。

而同期 `ak.stock_zt_pool_em(date="20260930")` 打到**同一个 host** 返回
**52 行**，`stock_zt_pool_strong_em` 返回 199 行。

**判定「某源被封」必须有 `ConnectionError` / `403` / `timeout` 之类明确的
失败证据。`200 + data:null` 只能说明参数或日期不对。**

这条直接违反了「优先用开源方案」这条自己定的规则：私有接口不该手写封装，
akshare 的参数是现成且已验证的。

### 反例 2：涨停池 52 → 8，是我选的降级榜从物理上就不可能装涨停股

push2ex 出问题时我写的降级档用两个同花顺榜反推涨停家数：

| 榜 | 行数 | 涨跌幅实测区间 | `>=9.8%` 的只数 |
|---|---|---|---|
| `stock_rank_cxfl_ths`（**放**量天榜） | 180 | 最低 -11.54，p50 1.45，p90 5.68，**最高 15.85** | **10** |
| `stock_rank_cxsl_ths`（**缩**量天榜） | 680 | 最低 -7.11，p50 -0.63，p90 0.98，**最高 4.98** | **0** |

原因很直白：**涨停当天必然巨量成交，缩量榜按定义就是「成交萎缩」的股票**，
两个集合几乎不相交。我把 680 只白拉进逻辑里，还多花 9 个请求，
实际只靠 180 只的放量榜在出货 —— 覆盖全市场 7423 只的 2.4%，
所以涨停家数只有个位数。

修正：只用放量榜，并在 `coverage` 里如实写明覆盖度。

### 附：休市日让上面两个数字都不可直接采信

上面所有实测都在 **2026-10-01（国庆休市）** 做的，三个坑叠在一起：

1. **push2ex 忽略 `date` 参数，固定返回最新交易日**
   ```
   date=20261001 -> 52 条, qdate=20260930
   date=20260930 -> 52 条, qdate=20260930   （与上行 52/52 完全相同）
   date=20260929 -> 57 条, qdate=20260930   （传 0929 却给 0930 的数据！）
   ```
   只有响应里的 `qdate` 可信，自己传进去的日期一律不信。
   akshare 会把 `qdate` 丢掉（只保留重命名后的业务列），要读就得读原始接口。

2. **levistock 休市日返回 0**：`2026-10-01` 三个类别全 0，
   `2026-09-30` 有 405 条。不回溯就会误判成源挂。

3. **入库日期必须是「真实数据日」**：休市日采集到的是上一交易日数据，
   用 `date.today()` 入库会在 `sa_sector_snapshots` 写进错标日期的行。
   日历用 `ak.tool_trade_date_hist_sina()`；**日历不可用时
   `is_trading_day` 必须返回 `None` 而不是 `False`** —— 分不清「休市」
   和「日历挂了」时把它当休市，会在交易时段错误地跳过采集。

## 一、结论先行

**没有任何一个方案能直接部署使用。** 每个都缺关键一环：

| 缺口 | 涉及项目 |
|---|---|
| 单一厂商（同花顺三子域同源），无成分股 | akshare、aktools |
| 无 License 字段；板块仅 405 个；`net_inflow` 字段错名 | levistock |
| 板块类齐全但**一调就炸**（`WorkdayService._loaded` AttributeError） | eltdx |
| 板块接口打的正是被封的 `push2/clist` | adata |
| 根本没有板块行情 API | jqdatasdk |
| 需 token / 积分 | tushare、HiThink |
| 完全没有板块功能 | easyquotation、Ashare |
| 协议服务器连不上 | qstock、pytdx、mootdx |
| 概念/地域服务端已废弃 | baostock |
| 上游已死 | pywencai |

因此走「集各家优点自建」路线，产出 `stock-advisor/data_service/`。

## 二、逐项实测记录

### 1. akshare 1.18.88 — ✅ 采用为主源
- 22,800 star / MIT / 最近提交 **2026-09-30**（调研当天）
- 实现：库函数，每个函数硬编码一个数据源 URL（`_em`东财 / `_ths`同花顺 / `_sinajs`新浪）
- 同花顺族实测：
  | 函数 | 行数 | 请求 | 耗时 | host |
  |---|---|---|---|---|
  | `stock_board_industry_summary_ths` | 90 | 3 | 0.6s | q.10jqka |
  | `stock_board_industry_name_ths` | 90 | 1 | 0.3s | q.10jqka |
  | `stock_board_concept_name_ths` | 375 | 41 | 4.6s | q.10jqka |
  | `stock_fund_flow_concept` | 387 | 9 | 1.1s | data.10jqka |
  | `stock_fund_flow_industry` | 90 | 3 | 0.5s | data.10jqka |
  | `stock_board_industry_index_ths` | 21 | 1-2 | 0.4s | d.10jqka |
  - 东财族全部失败（`push2*` 全封），实测 `stock_board_industry_name_em` 500
- 坑：
  - `stock_board_concept_summary_ths` 名字像行情快照，实际是**概念新闻流**，
    `日期` 停在 **2026-07-31** —— 当实时数据用会静默拿到两个月前的值
  - `stock_zt_pool_em` 不传 `date` 返回 `200 []`；假日传 date 返回**上一交易日**集合
  - 无 THS 成分股函数（`stock_board_*_cons_ths` 不存在）
  - 三子域同属同花顺一家
- 裸 HTML 交叉验证：`q.10jqka.com.cn/thshy/` 首行 HTML 与 akshare 输出**逐字段一致**
  （生物制品 4.63/878.68/236.62/14.36/53/2/26.93/康希诺），
  证明 akshare 就是解析该页；但裸 HTML 翻页 page≥2 一律 **401**，
  akshare 靠处理反爬 header 拿到 90/90 → **不要自己爬，用 akshare**

### 2. levistock 0.1.8 — ✅ 采用为第二厂商
- PyPI 元数据**无 License 字段**（授权不明，仅供个人研究）
- 实现：`sector_ranking_kph(date, zs_type, fetch_all)`，zs_type 4=行业 6=地域 7=题材
- 实测：行业 104 + 题材 259 + 地域 42 = **405**，fetch_all 0.15-0.7s
- 比同花顺多：`turnover_rate`、`buy_amount`、`sell_amount`、`market_cap`
- 坑：
  - subagent 报「904 板块 / 有 499 概念端点」**实测不成立**：
    合法 zs_type 只有 4/6/7，无「499 概念」端点，实际 405
  - `net_inflow` 与 `net_inflow_5d` **名字对调**：405/405 条满足
    `buy_amount + sell_amount == net_inflow_5d`，故 `net_inflow_5d` 才是当日净流入
  - `amount` **不是成交额**：会返回负值（IT服务 -79.0 而同花顺 254.9），
    中位相对偏差 127%
  - 休市日（2026-10-01 国庆）返回 0，需回溯最近交易日

### 3. aktools 0.0.91 — 可作部署形态
- akshare 官方 HTTP 化，FastAPI 把 `dir(ak)` 全部 1083 项映射到 `/api/public/{name}`
- 官方 Dockerfile 存在（python:3.13-slim + gunicorn，`--bind 0.0.0.0:8080`）
- 响应体实测统一 `list[dict]`（`df.to_json(orient="records")`），
  **不是** `{"data":{"columns":..}}` —— 我第一版测试脚本按后者解析，全是假失败
- 缺陷：只是 akshare 的壳，同样单一厂商；`/openapi.json` 只声明 7 条路由
- 启动：`python -m aktools` → 127.0.0.1:8080

### 4. adata 2.9.5 — ⚠️ 仅取成分股
- 5259 star / Apache-2.0 / 最近提交 2025-12-26
- 实现：多数据源融合设计，但**只有 4 个方法真有多源链**
- ❌ 板块列表/资金流全部打 `push2/clist`（被封）
- ❌ 无涨停池；`up_count`/`down_count`/`lead_stock`/`turnover_rate` 整个包里不存在
- ❌ `all_concept_code_east()` 干净安装即报 `FileNotFoundError`（缓存 CSV 不在包里）
- ✅ **可用**：`info.concept_constituent_ths(index_code=)`
  - `881121` → 189 行，`881142` → 56 行（行业 881xxx 体系，**6/6 直通**）
  - `886013` → 303 行（概念指数 886xxx 体系）
  - akshare 的概念 code 是 3xxxxx，直喂 **0/6 全败** → 无桥接
  - 坑：参数名写错成 `concept_code=` 会**静默返回 0 行**不报错
- 桥接方案：用 `info.get_concept_ths(stock_code=)` 探针建 name→886xxx 映射，
  探 40 只股票覆盖 43.5% 概念（20 只即 41.9%，收益饱和），且撞 `ValueError` bug

### 5. baostock 0.9.4 — ⚠️ 仅日K 兜底
- 匿名登录可用（1292ms），`public-api.baostock.com:10030` 自定义 TCP 协议
- ❌ `query_stock_concept` / `query_stock_area` 服务端已废弃
  （`code=10004020 msg=错误的消息类型`）
- ❌ `query_stock_industry` 仅 84 个证监会行业（需 496），6% 空值，T-1 延迟
- ❌ 无资金流 API；无涨停池；单只日K 16.9s（51550 只需 242h）
- ✅ 日K 14 字段含 `turn`（换手率），可做非东财兜底

### 6. efinance 0.5.9 — ❌
- ❌ import 即报 `ImportError: cannot import name 'jsonpath'`（包名冲突）
- ❌ **无板块列表 API**；唯一入口 `get_realtime_quotes('行业板块')` 打 `clist/get`，21/21 全败
- ❌ 100% 东财，零兜底
- ❌ 硬编码 https —— 实测 `push2/ulist.np` 在 **http 稳、https 被 RST**
- ✅ 可用部分：`get_quote_snapshot`（含涨停价）、`get_daily_billboard`（龙虎榜）

### 7. jqdatasdk 1.9.8 — ❌
- ❌ **根本没有板块行情 API**（77 个公开函数逐个枚举确认）
- ❌ `auth_by_token` 服务端已废弃（thrift 直连返回「该方法已弃用」）；
  `auth()` 需手机号+密码
- 注：项目 `config.yaml` 里的 `jq.token` 是 **joinquant.com 社区 cookie**，
  不是 JQData 凭证
- 网络非瓶颈：thrift `39.107.190.114:7000` 连通 451ms

### 8. tushare 1.4.29 — ❌
- ❌ 无 token（`.env` 无该键；config.yaml 那 40 位 hex 是 `jq.token`）
- ❌ `moneyflow_ind_ths` 需 5000 积分
- ❌ 老免费分类接口 `file.tushare.org` **DNS 已死**（URLError 11001）
- 网络通：`api.tushare.pro` 8.140.225.26，40101「token 不对」

### 9. easyquotation 0.7.7 — ❌
- 8 个 source 逐一测试，`board/sector/industry/concept/plate/limit/classify` 全 None
- ❌ `daykline._gen_stock_prefix` 硬编码 `hk` → A股返回 `[]`
- ❌ `timekline` 返回 2021 年僵尸数据
- ✅ 仅 `use('tencent').stocks()` 可用（53 列，含涨停价/换手率）

### 10. qstock 1.3.8 / pytdx 1.72 / mootdx 0.11.7 — ❌
- 通达信二进制协议 TCP:7709
- ❌ 实测 7 个公网服务器 **6 个 TCP 超时**，唯一连上的（124.71.187.122）
  在 API 调用层报 `TdxFunctionCallError`
- mootdx 仅覆盖成分股

### 11. eltdx 3.2.2 — ⚠️ 能连但板块功能全崩
- 546 star / **非 OSI 开源**（Research-Only）/ PyPI 无 License 字段
- ✅ **连接成功 137ms** —— 靠 `probe_hosts=True, probe_workers=32` 并行探测选活主机，
  这正是 pytdx 缺的能力
- ✅ 类齐全：`BoardQuoteTable` / `BoardMemberQuoteTable` / `ThemeStrengthTable` /
  `LimitBoardLadder` / `StockTopics`
- ❌ 但一调就炸：
  | 调用 | 结果 |
  |---|---|
  | `board_quotes(category='概念')` | 返回 0 行 |
  | `theme_strength_rank()` | `AttributeError: 'WorkdayService' object has no attribute '_loaded'` |
  | `limit_ladder()` | 同一 bug |
  | `board_member_quotes('880301')` | `ValueError: unknown board code` |
  | `buy_sell_strength('600519')` | `{}` 空 |
- ✅ 个股行情/日K 正常（`total_amount` 与新浪完全一致，精确到个位）

### 12-16. 其余
- **Ashare**（3888 star，无 License）：0 个板块函数
- **pywencai / zsrl**（900 star MIT）：`/customized/chart/get-robot-data` → 403 openresty
- **HiThink Financial-API**（3929 star MIT）：需 key，资金流端内专用
- **a-stock-data**（10460 star Apache-2.0）：34 源的 skill 提示词，不是库
- **tqsdk / xtquant / rqalpha**：subagent 基础设施 ECONNRESET，未取得结果（跳过）
- **zvt**（4318 star MIT）：其 Sina 通道 `ssl_bkzj_bk` 被本项目直接采用

## 三、意外收获：新浪资金流接口（直接采用）

`zvt` 的 Sina 通道实测可用，本项目验证后纳入第三数据源：

```
http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk
  ?page=1&num=1000&sort=netamount&asc=0&fenlei={0|1|2}
```
- fenlei 0=行业(48) 1=概念(181) 2=证监会行业(154)，共 **383**，**单请求 ~140ms，无翻页**
- 15 列含 `netamount`(净流入) / `inamount` / `outamount` / `turnover` /
  `ts_name`(领涨股) / `ts_changeratio`
- 定位为**辅助源**的实测理由：
  - `avg_changeratio` 是比率且只 2 位小数 → 涨跌幅精度 **±0.5pp**，
    不够做主源（实测 医疗器械 新浪 0.02 vs 同花顺 1.15）
  - 申万口径，与同花顺 90 行业只重叠 4 个
  - `ratioamount` 是比率不是绝对额，拿不到绝对主力净流入
  - 但 `netamount` 绝对额可靠 → 适合作补缺

## 四、跨厂商口径核验（重要）

同花顺 90 个行业 ∩ 开盘红 104 个行业 = **90 个全重叠**，逐项对比：

| 指标 | 结果 | 判读 |
|---|---|---|
| 涨跌幅 | 中位差 **0.029pp**，最大 0.668pp，90/90 在 1pp 内 | **同口径，可用做交叉校验** |
| 成交额 | 中位相对偏差 **127%**，且开盘红给负值 | 不同口径，开盘红 amount 不可信 |
| 资金流方向 | 同号率仅 **65.6%** | **定义差异**：同花顺=主力大单，开盘红 buy+sell=全主动买卖 |

**结论：跨厂商校验只能用涨跌幅。** 拿资金流方向当告警阈值会天天误报
（本项目第一版就是这么写的，实测 61.3% 一致率误报后改成涨跌幅校验）。

## 五、原实现被烧死的机制

`sector.py` 的 `_sector_intraday_loop`：

```
交易时段每 5 分钟一轮（config.yaml: interval_minutes: 5）
每轮 fetch_all_boards() = 行业5页 + 概念6页 + 地域1页 = 12 个 clist 请求
3 个并发 worker 同时打
=> 4 小时交易时段 × 12 轮/小时 = 48 轮/日 × 12 = 576 请求/日，打同一个 host
```

`server.log` 实测：234 轮成功 + 87 轮失败 = 321 轮 × 12 ≈ **3852 个请求**。

致命点：**被封后循环不停**，继续每 5 分钟捶 12 次 → 封禁被永久续期，
永不自行恢复。且失败只 `print` 到日志，页面/微信全静默。

→ 本服务的对策：按 host 记账（`SA_HOST_DAILY_BUDGET`，默认 200），
超预算跳过该源；`sector.py` 侧加指数退避（5/10/20/40…分钟，封顶 1h）
+ 连续 3 次失败推告警 + 页面陈旧横幅。
