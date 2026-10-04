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

### 已知局限

- **Bull 和 Bear 用同一个模型**。TradingAgents 的 `role_llms` 支持给多空辩手配
  不同厂商模型来避免「同源模型互相不反驳」，**本项目没实现**。当前两方同源，
  辩论的独立性打了折扣。真要强化，需要在 `debate.py` 里加 per-role 模型配置。
- **Judge 的推理可能输出英文**（Bull/Bear/RiskCritic 是中文）。评级行 `Rating:`
  能被正确提取，但正文语言不稳定。
- 测试：`%LOCALAPPDATA%\Temp\opencode\test_rc_veto.py`（45 项，
  含「正文含 buy/sell 但评级相反」「ETF 不误判为 ST」「否决只拦相反方向」）。

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

### 提交范围

工作区里混有用户自己的 WIP（`jq_sandbox.py`、`sandbox_runner.py`、`valuation_data.py`、
`scripts/*.py`、`_dirty_backup_*.json`、`clade`）——**不要碰、不要提交**。
