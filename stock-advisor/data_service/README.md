# 板块数据服务：独立部署与接入说明

## 是什么

一个**独立、无状态**的板块行情采集服务。它替代 `stock-advisor/sector.py`
里手写的东财直连采集器（那个在 2026-09-21 被 WAF 掐断后静默停更 9 天）。

服务只做一件事：**从多个互不相关的厂商拿板块行情，归一化后用 HTTP 暴露**。
它不碰数据库、不依赖 stock-advisor 主应用，可以单独部署到任何有 Python 3.10+
和 Docker 的机器上。

## 为什么不直接用现成开源项目

调研了 16 个方案（见 `docs/data-source-survey.md`），**没有一个能直接用**：

- akshare / aktools：同花顺一家独大，三子域同源，无成分股
- levistock：无 License、板块功能只有 405 个、`net_inflow` 字段错名
- eltdx：类齐全但一调就炸（`WorkdayService._loaded` AttributeError）
- adata：板块接口打的正是被封的 `push2/clist`
- jqdatasdk / tushare / efinance / easyquotation / baostock / qstock / pytdx：
  要么没有板块数据，要么没凭证，要么连不上

所以本服务做的是**集各家之长**：借 akshare 的同花顺口径、借 levistock 的
第二厂商与 `turnover_rate`、借新浪的申万口径、借 adata 的成分股能力，
外面套一层**请求量管控、跨厂商校验、显式降级**。

## 三个数据源

| 源 | 通道 | 板块数 | 独有字段 | 定位 |
|---|---|---|---|---|
| **同花顺** | `q.10jqka.com.cn` / `data.` | 477（行业90+概念387） | 涨跌家数、均价 | **主源**，涨跌幅精度最高 |
| **开盘红/财联社** | 经 `levistock` | 405（行业104+题材259+地域42） | `turnover_rate`、`buy/sell_amount`、市值 | 辅助源，补地域/题材 |
| **新浪** | `vip.stock.finance.sina.com.cn` | 383（行业48+概念181+证监会154） | 流入/流出分列 | 辅助源，只补空不覆盖 |

三套系统互不相关，**涨跌幅交叉校验中位差 0.029pp**（同花顺 vs 开盘红），
说明数据可信。

## 五个实测踩过的坑（都已处理，勿回退）

1. **原实现被烧死的机制**：`sector._sector_intraday_loop` 交易时段每 5 分钟
   一轮、每轮 12 个请求全打 `push2delay` 一个 host = 576 请求/日，触发 WAF。
   被封后循环不停，继续捶，**永久续期永不恢复**。
   → 本服务按 host 记账（`SA_HOST_DAILY_BUDGET`，默认 200），超预算跳过该源。

2. **levistock 字段错名**：405/405 条满足 `buy_amount + sell_amount ==
   net_inflow_5d`，所以 **`net_inflow_5d` 才是当日净流入**，`net_inflow`
   反而是 5 日累计。已在 `normalize.from_kph` 换名修正。

3. **levistock 的 `amount` 不是成交额**：实测会返回**负值**（IT服务 -79.0
   而同花顺 254.9），中位相对偏差 127%。负成交额物理上不可能。
   → 只采信正值，负值/缺失一律留 `None`，不覆盖主源。

4. **跨厂商不能拿资金流方向当校验阈值**：同花顺「净流入」是主力大单口径，
   开盘红 `buy+sell` 是全主动买卖，实测同号率只有 **65.6%** —— 这是定义
   差异不是数据错误，用它告警会天天误报。→ 只校验**涨跌幅**。

5. **同花顺板块成分股 akshare 没有**：`stock_board_*_cons_ths` 不存在。
   adata 的 `concept_constituent_ths(index_code=)` 可以，但**只吃 881xxx
   （行业，实测 6/6 通）和 886xxx（概念指数）**；akshare 的概念 code 是
   3xxxxx，直喂 adata **0/6 全败**。→ 对 3xxxxx 明确返回可读 404，
   不静默返回 0 行。

另外两个不处理的坑，写在这里免得后人踩：
`stock_board_concept_summary_ths` 名字像行情快照，实际是概念新闻流
（`日期` 停在 2026-07-31）；`stock_zt_pool_em` 不传 `date` 返回 `200 []`。

## 端点

```
GET  /health                            服务 + 各源健康度 + 请求量预算
GET  /boards                            全量板块 + 轮动评分（默认不截断）
     ?include_concept=false               只取行业（省 9 个请求）
     &include_aux=false                   不用辅助源（最省）
     &include_sina=false                  不用新浪
     &with_momentum=true&momentum_limit=30 附带动量分（会多打请求）
     &limit=N                             只返回前 N
     &refresh=true                        跳过缓存强制重采
GET  /boards/{code}/constituents        板块成分股（881xxx 可用）
GET  /zt-pool                            涨停池
GET  /history/{板块名}                    板块指数历史日线
POST /collect                            强制采集一轮
```

`/boards` 响应关键字段：

```json
{
  "total_boards": 679,
  "boards": [{
    "code": "881142", "name": "生物制品", "kind": "industry",
    "pct": 4.63, "turnover": 236.62, "main_inflow": 14.36,
    "up_count": 53, "down_count": 2, "lead_stock": "康希诺",
    "score": 67.2,
    "score_parts": {"pct": 39.7, "flow": 27.3, "zt": 0, "mom": 0.0},
    "source": "ths"
  }],
  "sources_used": ["ths", "kph", "sina", "ztpool"],
  "sources_failed": [],
  "degraded": false,
  "cross_check": {
    "metric": "pct_change",
    "pairs": [{"pair": ["ths","kph"], "common": 90,
               "median_pp": 0.029, "p90_pp": 0.11, "max_pp": 0.668,
               "within_1pp_pct": 100.0}]
  },
  "meter": {"day": "2026-10-01", "budget_per_host": 200,
            "used": {"q.10jqka.com.cn": 4, "data.10jqka.com.cn": 9}}
}
```

`score_parts` 四个分量对应 `score = 涨幅分(0-40) + 资金分(0-30) + 涨停分(0-20) + 动量分(0-10)`，
与原 `sector.py` 公式一致（`_momentum` 改成了真复利，原实现是简单求和，
连续 3 天各 +10% 时原实现给 30%、实际 33.1%，板块级别差 3pp 足以改变排名）。

## 部署

```bash
cd stock-advisor/data_service
docker compose up -d --build
curl -s localhost:8080/health
curl -s 'localhost:8080/boards?limit=5'
```

端口只绑 `127.0.0.1`，**不暴露公网**。外部访问走 SSH 隧道
（与本机 WeRSS 8001 同一模式）：

```powershell
ssh -N -L 8080:127.0.0.1:8080 root@101.43.25.101
```

## 接入 stock-advisor

`sector.py` 把原来的 `fetch_all_boards()` 换成调本服务，其余
`build_overview` / `save_snapshot` / 前端逻辑**完全不动** —— 因为本服务
输出的字段名与原 `_row_to_snapshot` 一致。失败时回落到原东财实现
（等东财解封就是免费的双保险），再失败才标记 `degraded`。

配置项：`config.yaml` 的 `sector.data_service_url`，
留空则不启用（保持原行为）。
