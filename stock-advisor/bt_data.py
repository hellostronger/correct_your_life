# -*- coding: utf-8 -*-
"""回测数据准备层：把库里的原始行情补成回测能直接用的形态。

存在的理由（2026-10-06 实测，非推测）：
`sa_market_kline` 的 `pct` / `amount` / `turnover_rate` **三列 100% 为 NULL**
（全表 334 万行，唯一 `source='tx'` 落库时没带这几列）。而任何 A 股回测都
要回答两个问题：「今天是不是涨停（买不进）」「这只票成交额够不够」——前者
要涨跌幅、后者要成交额。直接 SELECT 出去给回测代码用，它拿到的是 `None`，
而 pandas 对 `None` 的处理往往是**静默降级**：`fillna(0)`、`reindex().ffill()`、
甚至直接跳过 —— 表现就是「回测跑通了」，但涨跌停约束根本没生效、流动性
过滤形同虚设。这与 AGENTS.md 第 2 条「区分 HTTP 200 和有数据」同一类：
**数据看着在，就是不能用。**

所以这层只做两件事：搬运 + 派生。不含任何交易逻辑，方便离线断言。

实测依据：
* `volume` 单位是**手**。用 `sa_stock_valuation.free_shares` 反推：
  600519 日成交 12,307 手 ×100 = 1230.7 万股，占其流通股本 12.5 亿股的
  0.098%；且 `circulating_market_cap / last_price` 与 `free_shares` 逐位
  相等（600519: 1,267,840,760 vs 1,250,081,601，差异来自 last_price 时点），
  说明两表同源对齐。
* ST 判定：全名册 5639 行扫描，含 `ST` 子串的 205 个简称**全部**以
  `ST`/`*ST`/`SST`/`S*ST` 开头，含 ST 但非 ST 形式的 **0 条**。
* 涨停价容差必须 **< 0.01 元**（A 股最小变动单位）：实测 2026-08 起的
  真实开盘价距涨停价 gap 落在 0.40~0.67 分（涨停价按分取整，前收×1.1
  不一定正好到分），用 0.011 会把其中一部分误判成封板 → 买不进。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

# 涨跌停幅度按板块分档。顺序有意义：更长前缀在前。
LIMIT_BY_PREFIX = (
    ("688", 0.20),   # 科创板
    ("300", 0.20), ("301", 0.20),   # 创业板
    ("920", 0.30), ("83", 0.30), ("87", 0.30), ("43", 0.30),  # 北交所
)
LIMIT_MAIN = 0.10   # 沪深主板
LIMIT_ST = 0.05     # ST / *ST

# 涨跌停价比对容差（元）。必须小于 0.01，见模块 docstring。
PRICE_TOL = 0.005

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(os.path.dirname(BASE_DIR), ".env")


# ---------------------------------------------------------------------------
# 涨跌停分档
# ---------------------------------------------------------------------------

def is_st_name(name: str) -> bool:
    """简称是否带风险警示。

    子串匹配在本名册下不会误判（实测 205/205 全是 ST 形式，0 例外），
    但这个结论**依赖数据** —— 名册换源之后要重跑该检查。
    """
    return "ST" in (name or "").upper().replace(" ", "")


def limit_pct(code: str, name: str = "") -> float:
    """单日涨跌停幅度。ST 优先于板块档：ST 股里主板/创业板都有，实际一律 5%。"""
    if is_st_name(name):
        return LIMIT_ST
    for pfx, pct in LIMIT_BY_PREFIX:
        if code.startswith(pfx):
            return pct
    return LIMIT_MAIN


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def load_env(env_file: str | None = None) -> None:
    """把 .env 读进 os.environ（不覆盖已有值），供 psycopg2 使用。"""
    p = Path(env_file or ENV_FILE)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def get_conn():
    """新建只读连接。用本模块而**不要** `from app import get_conn` ——
    那会 import 整个 app.py 并启动所有守护线程（实测 46.8 秒）。"""
    import psycopg2
    load_env()
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", ""),
        port=os.environ.get("DB_PORT", "5432"),
        user=os.environ.get("DB_USERNAME", ""),
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ.get("DB_DATABASE", ""),
    )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

_ROSTER_COLS = "code, coalesce(name,'') AS name, coalesce(industry,'') AS industry, " \
              "list_date, coalesce(market,'') AS market"


def load_roster(conn) -> pd.DataFrame:
    """股票名册：ST 判定 / 行业（中性化用）/ 上市日期（次新股过滤用）/ 市场。"""
    return pd.read_sql_query(f"SELECT {_ROSTER_COLS} FROM sa_stock_roster",
                             conn, parse_dates=["list_date"])


def load_kline(conn, codes: list[str] | None = None, start: str = "2024-01-01",
               end: str | None = None, limit: int | None = None) -> pd.DataFrame:
    """日线长表（原始 6 列，不含库里为 NULL 的 pct/amount）。"""
    where = ["trade_date >= %s"]
    args: list = [start]
    if end:
        where.append("trade_date <= %s")
        args.append(end)
    if codes:
        where.append("code = ANY(%s)")
        args.append([str(c) for c in codes])
    if limit:
        # 先取「有数据的代码」再筛，limit 是**代码数**不是行数
        where.append("code IN (SELECT code FROM sa_market_kline "
                     "WHERE trade_date >= %s GROUP BY code LIMIT %s)")
        args += [start, limit]
    sql = (f"SELECT code, trade_date, open, high, low, close, volume "
           f"FROM sa_market_kline WHERE {' AND '.join(where)} ORDER BY code, trade_date")
    return pd.read_sql_query(sql, conn, parse_dates=["trade_date"])


def load_valuation(conn, codes: list[str] | None = None,
                   start: str = "2024-01-01") -> pd.DataFrame:
    """估值长表。**不要用 turnover_rate** —— 实测 360 万行里只有 6 行非空。"""
    sql = ("SELECT code, trade_date, last_price, total_shares, free_shares, "
           "total_market_cap, circulating_market_cap, pe_ttm, pb, ps_ttm "
           "FROM sa_stock_valuation WHERE trade_date >= %s")
    args: list = [start]
    if codes:
        sql += " AND code = ANY(%s)"
        args.append([str(c) for c in codes])
    return pd.read_sql_query(sql + " ORDER BY code, trade_date", conn,
                             parse_dates=["trade_date"])


def load_bench_codes(conn, symbol: str = "000300") -> list[str]:
    """指数成分股（沪深300 等）。

    ⚠️ 库中**没有**成分股表，这里只能拿 akshare 的**当期快照**。用它做
    历史回测就是幸存者偏差（用今天的成分股名单买 2022 年的股票），
    所以 `strategy_feasibility` 把 index_constituent 判为 fatal 缺失。
    取失败返回空列表，不抛异常、不假装成功。
    """
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol=symbol)
        if df is None or df.empty or "成分券代码" not in df.columns:
            return []
        return [str(x).zfill(6) for x in df["成分券代码"].tolist()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 派生
# ---------------------------------------------------------------------------

def add_derived(df: pd.DataFrame, names: dict[str, str] | None = None) -> pd.DataFrame:
    """补出回测必需但库里为 NULL 的派生列。**先按 code 排序分组**。

    产出列：
      prev_close    上一交易日收盘（首日为 NaN）
      pct           close/prev_close-1（首日 NaN，不是 0 —— 0 会被当成「平盘」）
      amount        close×volume×100（volume 单位是手，见模块 docstring）
      limit_pct     按板块/ST 分档
      limit_up/dn   前收×(1±幅度)，按分取整
      at_limit_up/dn     盘中触及涨停/跌停（一字板判定）
      tradable_buy/sell  开盘是否未封板 → 当日能否买入/卖出
    """
    df = df.sort_values(["code", "trade_date"]).copy()
    g = df.groupby("code", sort=False)
    df["prev_close"] = g["close"].shift(1)
    df["pct"] = df["close"] / df["prev_close"] - 1.0
    df.loc[df["prev_close"].isna(), "pct"] = float("nan")
    df["amount"] = df["close"] * df["volume"] * 100.0
    names = names or {}
    df["limit_pct"] = [limit_pct(c, names.get(c, "")) for c in df["code"]]
    df["limit_up"] = (df["prev_close"] * (1 + df["limit_pct"])).round(2)
    df["limit_dn"] = (df["prev_close"] * (1 - df["limit_pct"])).round(2)
    df["at_limit_up"] = df["high"] >= (df["limit_up"] - PRICE_TOL)
    df["at_limit_dn"] = df["low"] <= (df["limit_dn"] + PRICE_TOL)
    df["tradable_buy"] = df["open"] < (df["limit_up"] - PRICE_TOL)
    df["tradable_sell"] = df["open"] > (df["limit_dn"] + PRICE_TOL)
    return df


def to_panel(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """长表 → {code: DataFrame(index=trade_date)}，回测代码的标准输入形态。"""
    return {code: sub.set_index("trade_date").sort_index()
            for code, sub in df.groupby("code", sort=False)}


def prepare(conn, codes: list[str] | None = None, start: str = "2024-01-01",
            end: str | None = None, limit: int | None = None,
            drop_st: bool = False, min_list_days: int = 0
            ) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """一步到位：读取 + ST 过滤 + 派生 + 转 panel。

    `drop_st`      剔除 ST/*ST（实测 205 只）。
    `min_list_days` 剔除上市不足 N 天的次新股（需要名册的 list_date）。
    返回 (panel, roster)。roster 供因子中性化取行业用。
    """
    roster = load_roster(conn)
    kl = load_kline(conn, codes=codes, start=start, end=end, limit=limit)
    if kl.empty:
        return {}, roster
    name_map = dict(zip(roster["code"], roster["name"]))
    if drop_st:
        st_codes = {c for c, n in name_map.items() if is_st_name(n)}
        kl = kl[~kl["code"].isin(st_codes)]
    if min_list_days > 0:
        r2 = roster.copy()
        r2["list_date"] = pd.to_datetime(r2["list_date"])
        cutoff = pd.Timestamp(end or kl["trade_date"].max()) - pd.Timedelta(days=min_list_days)
        young = set(r2.loc[r2["list_date"] > cutoff, "code"])
        kl = kl[~kl["code"].isin(young)]
    kl = add_derived(kl, names=name_map)
    return to_panel(kl), roster