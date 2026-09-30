"""A 股板块数据服务 —— 独立可 Docker 部署的数据供给模块。

为什么存在
----------
原实现（2026-09-21 前）是 stock-advisor/sector.py 里手写的 requests 采集器，
直连东财 `push2delay.eastmoney.com/api/qt/clist/get` 翻页取 1031 个板块。
问题（2026-09-30 定位）：

  1. `sector._sector_intraday_loop` 交易时段每 5 分钟一轮，每轮 12 个 clist 请求
     = 576 请求/日 打在同一个 host 上，被 WAF 掐死。
  2. 被掐之后循环**不会停**，继续每 5 分钟捶 12 次 —— 永久续期，永远不恢复。
  3. 失败只 print 到日志，页面照常渲染 9 天前的旧数据，没人发现。
  4. 单一数据源，无任何降级。

调研 16 个开源方案（见 docs/data-source-survey.md）后的结论是
**没有任何一个能直接部署**，于是集各家之长自建：

采纳的各家长处
--------------
- **akshare**（同花顺）：板块口径最全，行业 90 个 8 字段 + 概念 387 个 + 板块指数
  历史日线（动量分不必再自己攒快照）。
- **levistock**（开盘红/财联社）：**第二厂商**，字段更全（多 `turnover_rate` /
  `buy_amount` / `sell_amount`），带 42 个地域板块（同花顺/新浪都没有）。
- **adata**：`concept_constituent_ths(index_code=)` 能按板块取成分股，
  akshare 完全没有这个能力。
- **baostock**：日 K 第二兜底（14 字段含换手率）。
- **push2ex**（东财，未被封）：涨停池，含连板数/炸板数。

规避的各家坑（都实测踩过）
--------------------------
- levistock 的 `net_inflow` 与 `net_inflow_5d` **名字对调**：
  实测 405/405 条满足 `buy_amount + sell_amount == net_inflow_5d`，
  所以 `net_inflow_5d` 才是**当日**净流入。本模块做了换名修正。
- akshare 的 `stock_board_concept_summary_ths` 名字像行情快照，实际是**概念新闻流**，
  `日期` 停在 2026-07-31。当实时数据用会静默拿到两个月前的值 —— 本模块不用它。
- akshare `stock_zt_pool_em` 不传 `date` 返回 `200 []`；且**假日传 date 会返回上一
  交易日的集合**（不校验就会把错标日期的数据写进历史）。本模块强制校验。
- 涨停池和板块代码体系不通用：概念成分股要用 886xxx（adata 的 `index_code`），
  akshare 给的是 3xxxxx，**直喂 0/6 全败**；行业用 881xxx 则 6/6 全通。
  本模块按类别分发不同 code，并记录桥接失败率。
- 同花顺三子域 `q.` / `data.` / `d.` **同属一家**，所以请求按子域分摊记账，
  并在超过阈值时自动降频 —— 这正是原实现被烧死的原因。
- 裸 HTML 抓 `q.10jqka.com.cn/thshy/` 翻页第 2 页起一律 401，
  akshare 靠处理反爬 header 拿到 90/90。**不要自己爬，用 akshare。**

对 stock-advisor 的接口约定
---------------------------
保持与原 `sector.py` 相同的输出结构（字段名 code/name/kind/pct/turnover/
turnover_rate/main_inflow/up_count/down_count/lead_stock/lead_stock_pct），
这样 `sector.py` 只需换掉 `fetch_all_boards()` 的实现，其余评分/存库/前端逻辑不动。
"""

__version__ = "1.0.0"
