"""字段规范化：把各家的脏字段映射成 stock-advisor 认识的统一结构。

统一结构（与原 sector.py._row_to_snapshot 一致）:
    code, name, kind, pct, turnover, turnover_rate, main_inflow,
    up_count, down_count, lead_stock, lead_stock_pct, quote_ts, source
"""
from __future__ import annotations

import math
from typing import Any

# kind 取值沿用原项目
KIND_INDUSTRY = "industry"
KIND_CONCEPT = "concept"
KIND_REGION = "region"
KIND_THEME = "theme"

KIND_NAME = {
    KIND_INDUSTRY: "行业",
    KIND_CONCEPT: "概念",
    KIND_REGION: "地域",
    KIND_THEME: "题材",
}


def num(v: Any) -> float | None:
    """宽松转 float：处理 None / '' / '-' / NaN / 带单位的字符串。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    s = str(v).strip().replace(",", "").replace("%", "")
    if not s or s in ("-", "--", "None", "nan", "NaN"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def int_or_none(v: Any) -> int | None:
    f = num(v)
    return None if f is None else int(round(f))


def _base(code, name, kind, source) -> dict:
    return {
        "code": code or "",
        "name": (name or "").strip(),
        "kind": kind,
        "pct": None,
        "turnover": None,
        "turnover_rate": None,
        "main_inflow": None,
        "up_count": None,
        "down_count": None,
        "lead_stock": "",
        "lead_stock_pct": None,
        "quote_ts": None,
        "source": source,
    }


def from_ths_industry(row: dict, code: str | None = None) -> dict:
    """akshare stock_board_industry_summary_ths 的一行。

    实测列: 序号 板块 涨跌幅 总成交量 总成交额 净流入 上涨家数 下跌家数
            均价 领涨股 领涨股-最新价 领涨股-涨跌幅
    单位: 总成交额/净流入 都是**亿**
    """
    out = _base(code or "", row.get("板块") or row.get("name"),
                KIND_INDUSTRY, "ths")
    out["pct"] = num(row.get("涨跌幅"))
    out["turnover"] = num(row.get("总成交额"))
    out["main_inflow"] = num(row.get("净流入"))
    out["up_count"] = int_or_none(row.get("上涨家数"))
    out["down_count"] = int_or_none(row.get("下跌家数"))
    out["lead_stock"] = str(row.get("领涨股") or "").strip()
    out["lead_stock_pct"] = num(row.get("领涨股-涨跌幅"))
    # 同花顺不给换手率；留 None 而不是填 0（原项目注释：腾讯源没有，留 NULL 不填 0）
    out["turnover_rate"] = None
    return out


def from_ths_concept(row: dict, code: str | None = None) -> dict:
    """akshare stock_fund_flow_concept 的一行。

    实测列: 序号 行业 行业指数 行业-涨跌幅 流入资金 流出资金 净额
            公司家数 领涨股 领涨股-涨跌幅 当前价
    已知缺失: 成交额 / 换手率 / 涨跌家数拆分（只有公司家数总数）
    """
    out = _base(code or "", row.get("行业") or row.get("name"),
                KIND_CONCEPT, "ths")
    out["pct"] = num(row.get("行业-涨跌幅"))
    out["main_inflow"] = num(row.get("净额"))
    out["lead_stock"] = str(row.get("领涨股") or "").strip()
    out["lead_stock_pct"] = num(row.get("领涨股-涨跌幅"))
    out["n_stocks"] = int_or_none(row.get("公司家数"))
    out["turnover"] = None          # 源不提供
    out["turnover_rate"] = None     # 源不提供
    out["up_count"] = None          # 源不提供
    out["down_count"] = None        # 源不提供
    return out


def from_kph(row: dict, kind: str) -> dict:
    """levistock sector_ranking_kph 的一行（开盘红/财联社）。

    实测列: plate_id plate_name amount change_pct amplitude net_inflow
            net_inflow_5d buy_amount sell_amount turnover_rate market_cap
            avg_change stock_count change_pct2

    两个实测出来的坑，都必须处理，否则会静默污染数据：

    1) **字段错名**：405/405 条满足 `buy_amount + sell_amount == net_inflow_5d`，
       所以 **net_inflow_5d 才是当日净流入**，net_inflow 反而是 5 日累计。
       这里把当日值放进 main_inflow，真 5 日值另存 main_inflow_5d。

    2) **`amount` 不是成交额**。实测同花顺/开盘红成交额中位相对偏差 127%，
       而且开盘红会给出**负值**（IT服务 -79.0 而同花顺 254.9）——负成交额
       物理上不可能。所以只采信正值，负值/缺失一律留 None，不覆盖主源。
    """
    out = _base(row.get("plate_id"), row.get("plate_name"), kind, "kph")
    out["pct"] = num(row.get("change_pct"))
    out["turnover_rate"] = num(row.get("turnover_rate"))
    buy = num(row.get("buy_amount"))
    sell = num(row.get("sell_amount"))
    daily = num(row.get("net_inflow_5d"))
    if daily is None and (buy is not None or sell is not None):
        daily = (buy or 0.0) + (sell or 0.0)
    # 原始单位是元，统一成亿，与同花顺口径一致便于比较
    out["main_inflow"] = None if daily is None else round(daily / 1e8, 4)
    raw5 = num(row.get("net_inflow"))
    out["main_inflow_5d"] = None if raw5 is None else round(raw5 / 1e8, 4)
    out["n_stocks"] = int_or_none(row.get("stock_count"))
    out["amplitude"] = num(row.get("amplitude"))
    out["market_cap"] = num(row.get("market_cap"))
    amt = num(row.get("amount"))
    out["turnover"] = amt if (amt is not None and amt > 0) else None
    out["amount_raw"] = amt          # 留原始值供排查，不作为成交额使用
    return out


def from_sina(row: dict, kind: str) -> dict:
    """新浪 MoneyFlow.ssl_bkzj_bk 的一行。第三厂商，仅用于交叉校验与补缺。

    实测列: cate_type category name avg_price avg_changeratio turnover
            inamount outamount netamount ratioamount
            ts_symbol ts_name ts_trade ts_changeratio ts_ratioamount

    实测局限（决定了它只能当辅助源，不能当主源）：
    - `avg_changeratio` 是**比率**（0.0246 = 2.46%）且只保留 2 位小数，
      精度只有 ±0.5pp。轮动评分对涨跌幅敏感，不够格做主源。
    - 分类是申万口径，与同花顺 90 个行业只重叠 4 个。
    - `ratioamount` 是比率不是绝对额，拿不到绝对主力净流入。
    - 但 `netamount`（净流入）是绝对额且可靠，可用于补概念的资金流。
    """
    out = _base("", row.get("name"), kind, "sina")
    ratio = num(row.get("avg_changeratio"))
    out["pct"] = None if ratio is None else round(ratio * 100, 2)
    out["pct_low_precision"] = True      # 合并时不覆盖主源
    net = num(row.get("netamount"))
    out["main_inflow"] = None if net is None else round(net / 1e8, 4)
    out["turnover"] = num(row.get("turnover"))
    out["lead_stock"] = str(row.get("ts_name") or "").strip()
    tsr = num(row.get("ts_changeratio"))
    out["lead_stock_pct"] = None if tsr is None else round(tsr * 100, 2)
    out["lead_stock_code"] = str(row.get("ts_symbol") or "").strip()
    ina = num(row.get("inamount"))
    outa = num(row.get("outamount"))
    out["in_amount"] = None if ina is None else round(ina / 1e8, 4)
    out["out_amount"] = None if outa is None else round(outa / 1e8, 4)
    return out


def board_from_zt(hybk: str, lbc: int | None = None) -> dict:
    """涨停池聚合出的一行（push2ex 东财，仍可用）。"""
    out = _base("", hybk, KIND_CONCEPT, "ztpool")
    out["zt_count"] = int_or_none(lbc)
    return out
