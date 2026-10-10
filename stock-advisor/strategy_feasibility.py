# -*- coding: utf-8 -*-
"""数据可行性判定：一条策略摘要能不能在本项目现有数据上**真回测**。

与 `strategy_gate.py` 是**两个维度，不是替代**：

| 模块 | 回答的问题 | 判据来源 |
|---|---|---|
| `strategy_gate` | 代码跑出来的数**能不能信** | 源码 + 沙箱返回的指标 |
| 本模块 | 这条策略**有没有数据可跑** | 摘要文本 + 本地库实际内容 |

为什么必须有这一层（2026-10-06 实测）：让 LLM 按摘要写一段 pandas 回测
代码再跑一遍，看起来是最快的验证方式，但它给不出可信的否定结论 ——
生成的代码对缺失数据几乎总是**静默降级**（`fillna(0)`、`reindex().ffill()`、
直接跳过该股），于是你拿到一条漂亮净值曲线，却不知道它是在多少只标的的
截面上算的、涨跌停有没有生效、ST 有没有被当普通股。实测两个具体陷阱：

1. `pct` / `amount` / `turnover_rate` 三列 **100% 为 NULL**，而回测判
   涨跌停必须要涨跌幅 → 涨跌停约束「跑通了但从未生效」。
2. 裸关键词 `日内` 会把**纯日线策略**误判成需要分钟线（策略1 的 steps 里
   写着「日线级别数据」）→ 反过来，本该跑的策略被判成跑不了。

所以判据写成代码：能力清单 + 分级（fatal 硬缺 / degraded 降级），
让「缺什么、缺了会怎样」在页面和日报里都能直接列出来。

实测的库内现状（2026-10-06，`sa_` 前缀 59 张表全量扫描）：

| 能力 | 状态 | 依据 |
|---|---|---|
| 日线 OHLCV | ✓ | `sa_market_kline` 5284 只 / 334 万行 / 2022-10-10 起 |
| 横截面选股 | ✓ | 单日最多 5271 只 |
| 估值 PE/PB/市值 | ✓ | `sa_stock_valuation` 265 万行 PE>0 |
| ROE / 盈利 / 营收 | ✗ | 全库**数值型**列 0 个 |
| 指数成分股（含历史） | ✗ | 库内表 0 张；akshare 只能给当期快照 |
| ST / 退市标记 | ✓ | 名册 205 个 ST 简称 |
| 日内分钟线 | ✗ | 分钟线表 0 张 |
| ETF 池 | ✗ | 仅 6 只，且 511880/513100/518880 全无 |
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FATAL = "fatal"        # 缺了结论不成立，必须判否
DEGRADED = "degraded"  # 能跑但有系统性偏差，必须标注
COSMETIC = "cosmetic"  # 只影响细节

# ---------------------------------------------------------------------------
# 数据能力清单：cap -> (显示名, 可得性依据, 缺失分级, 缺失后果)
# 分级不是随手定的 —— 判据是「缺了之后，回测数字离真实现场的偏离程度」：
# 补不回来（结构性缺）= fatal；能自己推但有偏 = degraded。
# ---------------------------------------------------------------------------
CAPABILITIES = {
    "daily_ohlcv": (
        "日线 OHLCV", "sa_market_kline 的 OHLCV", FATAL,
        "所有策略的基础，无它无法回测"),
    "cross_section": (
        "横截面选股", "同一交易日的多标的行情", FATAL,
        "单标的策略不需要；选股/轮动策略缺了只剩一只，等于改了策略"),
    "valuation": (
        "估值 PE/PB/市值", "sa_stock_valuation", FATAL,
        "价值/多因子策略的核心因子，缺了因子集不完整"),
    "fundamental_roe": (
        "ROE / 盈利 / 营收 / 同比", "全库无数值型财务列", FATAL,
        "多因子策略的盈利因子整组缺失"),
    "index_constituent": (
        "指数成分股（含历史）", "库中无成分股表", FATAL,
        "用当期成分做历史回测 = 幸存者偏差（今天的名单买 2022 年的股票）"),
    "etf_pool": (
        "ETF 池（含防御品种）", "库中仅 6 只 ETF", FATAL,
        "轮动策略的池子缺失，只能换成作者没指定的替代品"),
    "intraday_bar": (
        "日内分钟线", "库中只有日线", FATAL,
        "日内择时/分批下单无法复现，只能改成日线近似"),
    "st_flag": (
        "ST / 退市标记", "sa_stock_roster.name", DEGRADED,
        "ST 涨跌停是 5%，按 10% 算会系统性高估收益"),
    "limit_price": (
        "涨跌停价", "可由前收×(1±幅度) 推出", DEGRADED,
        "不判一字板 → 回测能买卖现实中买不到的单"),
    "volume_liquidity": (
        "成交额 / 换手率", "库列为 NULL，可由 close×volume×100 推出", DEGRADED,
        "流动性过滤需自己补算，否则小盘垃圾股照样入选"),
}

# ---------------------------------------------------------------------------
# 文本 → 所需能力
#
# ⚠️ 两个反直觉的措辞陷阱（都是 2026-10-06 实测踩出来的）：
#   1) `日内` 不能裸匹配 —— 纯日线策略的 steps 里常写「日线级别数据」，
#      裸匹配会把它们全判成需要分钟线。真正的日内要求是「盘中某时刻
#      决策/执行」，用带时间点或日内机制名的模式。
#   2) `ROE` 可以裸匹配，但探测「库里有没有财务列」时**不能**用
#      `column_name LIKE '%eps%'` —— `sa_strategy_digest.s-t-e-ps` 会被命中，
#      那是存策略摘要的 jsonb。第一版据此误报「基本面可用」。
# ---------------------------------------------------------------------------
_RULES: list[tuple[str, list[str], str]] = [
    (r"选股|横截面|因子|股票池|成分股|多因子|打分|排名|分位数|轮动",
     ["cross_section"], "需要横截面比较"),
    (r"沪深\d+|上证\d+|中证\d+|创业板指|科创\d+|成分股",
     ["cross_section", "index_constituent"], "指定了指数成分股"),
    (r"PE|PB|市盈|市净|估值|低估值|价值",
     ["valuation"], "出现估值类因子"),
    (r"净资产收益|净利同比|净利润|营业收入|营收|同比|增长率|盈利能力|基本面|ROE|杜邦",
     ["fundamental_roe"], "出现盈利类基本面因子"),
    (r"\d{1,2}:\d{2}|TWAP|集合竞价|盘中|分钟线|tick|分时",
     ["intraday_bar"], "出现盘中执行/择时（带时间点或日内机制名）"),
    (r"ETF", ["etf_pool"], "策略基于 ETF 轮动"),
    (r"511880|513100|518880|511990|512880|防御\s*ETF",
     ["etf_pool"], "点名具体 ETF 代码"),
    (r"ST|退市|涨跌停|一字板|停牌",
     ["st_flag", "limit_price"], "出现 ST / 涨跌停类规则"),
    (r"成交额|换手率|流动性|量能", ["volume_liquidity"], "出现成交额/流动性过滤"),
    # 纯日线技术指标：不额外加能力（作为 baseline 的说明性命中）
    (r"均线|MA\d|BBI|BOLL|布林|MACD|RSI|突破|回踩|均线多头",
     [], "纯日线技术指标"),
]

# 所有 A 股策略都成立的 baseline，不靠关键词
_BASELINE = {
    "daily_ohlcv": "baseline：任何 A 股策略都要日线",
    "limit_price": "baseline：涨跌停对 A 股所有策略都成立",
}


@dataclass
class Finding:
    cap: str
    available: bool
    severity: str
    evidence: str      # 为什么需要这一项（含命中的原词）
    basis: str         # 可得性依据（探测到的库内事实）


@dataclass
class Feasibility:
    title: str
    strategy_type: str
    findings: list = field(default_factory=list)
    fatal_missing: list = field(default_factory=list)
    degraded_missing: list = field(default_factory=list)
    ok: bool = False

    @property
    def caps(self) -> list:
        return [f.cap for f in self.findings]


# ---------------------------------------------------------------------------

def required_caps(text: str) -> tuple[list[str], dict[str, str]]:
    """从摘要/步骤/参数文本提取所需数据能力。

    返回 (能力列表, {能力: 证据})。
    **证据按能力存字典** —— 第一版返回 list，渲染时取 `findings[0].evidence`
    导致每一行都显示同一句（策略1 显示成了策略2 的「13:10」理由）。
    """
    caps: set[str] = set()
    ev: dict[str, list[str]] = {}
    for pat, need, why in _RULES:
        m = re.search(pat, text, re.IGNORECASE)
        if not m or not m.group(0):
            continue
        for c in need:
            caps.add(c)
            ev.setdefault(c, []).append(f"{why}（原文「{m.group(0)}」）")
    for c, why in _BASELINE.items():
        caps.add(c)
        ev.setdefault(c, []).insert(0, why)
    return sorted(caps), {k: " / ".join(v) for k, v in ev.items()}


def judge(title: str, strategy_type: str, text: str,
          avail: dict[str, bool]) -> Feasibility:
    """判定一条策略的数据可行性。

    `avail[cap]` 由 `probe()` 实测得出。**没探测到的 key 按 False 处理**
    （宁可判不可行，也不要在信息不足时放行）。
    """
    caps, ev = required_caps(text)
    fe = Feasibility(title=title, strategy_type=strategy_type or "")
    for cap in caps:
        name, basis, sev, why = CAPABILITIES[cap]
        available = bool(avail.get(cap, False))
        fe.findings.append(Finding(cap, available, sev, ev[cap], basis))
        if not available:
            if sev == FATAL:
                fe.fatal_missing.append(f"{name} —— {why}")
            else:
                fe.degraded_missing.append(f"{name} —— {why}")
    fe.ok = not fe.fatal_missing
    return fe


def one_line(fe: Feasibility) -> str:
    if fe.ok and not fe.degraded_missing:
        return f"\u2705 {fe.title}：可回测（{len(fe.findings)} 项数据齐备）"
    if fe.ok:
        return (f"\u26a0\ufe0f {fe.title}：可回测但结果有系统性偏差"
                f"（{len(fe.degraded_missing)} 项降级："
                f"{'；'.join(fe.degraded_missing)}）")
    return (f"\u274c {fe.title}：不可回测（{len(fe.fatal_missing)} 项硬缺："
            f"{'；'.join(fe.fatal_missing)}）")


def detail_lines(fe: Feasibility) -> list[str]:
    """逐项明细。缺项额外一行说明「依据 + 缺了会怎样」，
    让看的人不用去翻模块源码就知道为什么否。"""
    out = [one_line(fe)]
    for f in fe.findings:
        mark = "\u2713" if f.available else "\u2717"
        out.append(f"    {mark} {CAPABILITIES[f.cap][0]}：{f.evidence}")
        if not f.available:
            out.append(f"        可得性依据：{f.basis}｜缺了会怎样："
                       f"{CAPABILITIES[f.cap][3]}")
    return out


# ---------------------------------------------------------------------------
# 实测可得性（连库 / 联网，不在离线断言里跑）
# ---------------------------------------------------------------------------

_NUMERIC_TYPES = ("numeric", "double precision", "real", "bigint", "integer")


def probe(conn, *, use_network: bool = True) -> tuple[dict[str, bool], dict[str, str]]:
    """探测本项目实际具备哪些数据能力。返回 (avail, counts)。

    全部是实测查询，不是假设 —— 库结构会变，判据跟着变才有意义。
    """
    cur = conn.cursor()
    avail: dict[str, bool] = {}
    cnt: dict[str, str] = {}

    cur.execute("""SELECT count(*), count(DISTINCT code), min(trade_date), max(trade_date)
                   FROM sa_market_kline WHERE close IS NOT NULL""")
    n, nc, d0, d1 = cur.fetchone()
    avail["daily_ohlcv"] = n > 100_000
    cnt["daily_ohlcv"] = f"{nc} 只 / {n} 行 / {d0} ~ {d1}"

    cur.execute("""SELECT max(c) FROM (
                   SELECT trade_date, count(DISTINCT code) c
                   FROM sa_market_kline WHERE trade_date >= '2026-09-01'
                   GROUP BY trade_date) x""")
    mx = cur.fetchone()[0] or 0
    avail["cross_section"] = mx >= 100
    cnt["cross_section"] = f"单日最多 {mx} 只标的"

    try:
        cur.execute("""SELECT count(*) FROM sa_stock_valuation
                       WHERE pe_ttm > 0 AND trade_date >= '2024-01-01'""")
        nv = cur.fetchone()[0]
        avail["valuation"] = nv > 100_000
        cnt["valuation"] = f"{nv} 行 PE>0（2024 起）"
    except Exception:
        avail["valuation"] = False
        cnt["valuation"] = "sa_stock_valuation 不可查"

    # 财务因子：只认数值型列，排除存策略摘要的 jsonb
    # （`%eps%` 会命中 sa_strategy_digest.steps，第一版据此误报可用）
    cur.execute("""SELECT table_name || '.' || column_name
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name LIKE 'sa\\_%'
                     AND data_type = ANY(%s)
                     AND (column_name ILIKE ANY(ARRAY['%%roe%%','%%netprofit%%',
                            '%%revenue%%','%%profit%%','%%yoy%%','%%eps%%']))""",
                (list(_NUMERIC_TYPES),))
    frows = cur.fetchall()
    avail["fundamental_roe"] = len(frows) > 0
    cnt["fundamental_roe"] = (f"数值型财务列 {len(frows)} 个"
                              + (f"（{frows[:3]}）" if frows else "，jsonb 摘要列已排除"))

    cur.execute("""SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='public'
                     AND (table_name ILIKE '%cons%' OR table_name ILIKE '%component%'
                          OR table_name ILIKE '%index%')""")
    ntab = cur.fetchone()[0]
    bench = ""
    if use_network:
        try:
            import bt_data
            bench = bt_data.load_bench_codes(conn, "000300")
        except Exception:
            bench = []
    # 只有当期快照不算「可用于历史回测的成分股」
    avail["index_constituent"] = ntab > 0
    cnt["index_constituent"] = (
        f"库内成分股表 {ntab} 张"
        + (f"；akshare 当期快照 {len(bench)} 只（当期≠历史，不能回测）" if bench else ""))

    try:
        cur.execute("""SELECT count(*) FROM sa_stock_roster WHERE name ILIKE '%ST%'""")
        nst = cur.fetchone()[0]
        avail["st_flag"] = nst > 0
        cnt["st_flag"] = f"名册含 ST 简称 {nst} 个"
    except Exception:
        avail["st_flag"] = False
        cnt["st_flag"] = "sa_stock_roster 不可查"

    avail["limit_price"] = True
    cnt["limit_price"] = "可由前收×(1±幅度) 推出，见 bt_data.add_derived"

    cur.execute("""SELECT count(*) FROM information_schema.tables
                   WHERE table_schema='public'
                     AND (table_name ILIKE '%minute%' OR table_name ILIKE '%intraday%')""")
    nmin = cur.fetchone()[0]
    avail["intraday_bar"] = nmin > 0
    cnt["intraday_bar"] = f"分钟线表 {nmin} 张"

    cur.execute("""SELECT count(DISTINCT code) FROM sa_market_kline
                   WHERE code LIKE '51%%' OR code LIKE '15%%' OR code LIKE '56%%'
                      OR code LIKE '58%%' OR code LIKE '159%%'""")
    netf = cur.fetchone()[0]
    avail["etf_pool"] = netf >= 20
    cnt["etf_pool"] = f"仅 {netf} 只 ETF（轮动策略通常需 20+）"

    avail["volume_liquidity"] = True
    cnt["volume_liquidity"] = "库列为 NULL，可用 close×volume×100 推出"

    cur.close()
    return avail, cnt


def judge_from_db(conn, digest_rows: list[dict], *, use_network: bool = True) -> list:
    """对一批 `sa_strategy_digest` 行做判定。行需含
    title/title_zh、strategy_type、summary、steps、universe、params。"""
    import json
    avail, cnt = probe(conn, use_network=use_network)
    out = []
    for r in digest_rows:
        parts = [r.get("summary") or ""]
        for k in ("steps", "universe"):
            v = r.get(k)
            if v is None:
                continue
            parts.append(json.dumps(v, ensure_ascii=False)
                         if not isinstance(v, str) else v)
        parts.append(str(r.get("params") or ""))
        fe = judge(r.get("title_zh") or r.get("title") or "?",
                   r.get("strategy_type") or "",
                   " ".join(p for p in parts if p), avail)
        out.append(fe)
    return out


def report(feasibilities: list, avail: dict[str, bool],
           cnt: dict[str, str] | None = None) -> str:
    """完整报告：先列本项目数据现状，再逐条判定。"""
    L = ["=" * 72, "本项目数据能力现状（实测）", ""]
    for cap, (name, _b, _s, _w) in CAPABILITIES.items():
        L.append(f"  {'✓' if avail.get(cap) else '✗'} {name:22s} "
                 f"{(cnt or {}).get(cap, '')}")
    L += ["", "=" * 72, "逐条判定", ""]
    for fe in feasibilities:
        L += detail_lines(fe)
        L.append("")
    ok_n = sum(1 for f in feasibilities if f.ok)
    L += ["=" * 72, f"可回测 {ok_n}/{len(feasibilities)}"]
    return "\n".join(L)