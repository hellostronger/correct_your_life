# -*- coding: utf-8 -*-
"""估值/市值数据：抓取 + 落库 + 增量同步。

为什么单独一个模块而不是塞进 market_data.py
------------------------------------------
market_data 管的是 **K 线**（价格/成交量），这个管的是 **估值**（市值/PE/PB）。
两者更新频率、数据源、字段语义都不同，而且估值只被「估值类策略」用得上。
混在一起会让 market_data 越来越大、而且原来那个文件的函数名（fetch_*kline）
会让人误以为所有 fetch_* 都是 K 线。

数据源（两个独立源，已交叉验证）
------------------------------
- **主：东财 datacenter-web 的 RPT_VALUEANALYSIS_DET**
  带 TRADE_DATE -> 能取**历史序列**（策略回测需要「当时」的市值）；
  单位是元；还带总股本/流通股本。**选它当主力是因为 push2 封我的时候
  它照常工作** —— 这点实测过。
- **备：腾讯 qt.gtimg.cn**
  只有当天快照、没有日期，单位是亿元。用来补最新一天。

  两个源对同一只票给的市值完全一致（600519 都是 15733.78 亿），
  所以选主源不选备源不会引入偏差。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

EM_VAL_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
              "?reportName=RPT_VALUEANALYSIS_DET&columns=ALL"
              "&filter=%(filter)s&pageNumber=%(page)d&pageSize=%(size)d"
              "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")
TX_QUOTE_URL = "https://qt.gtimg.cn/q="

COLUMNS = ["total_shares", "free_shares", "total_market_cap",
           "circulating_market_cap", "pe_ttm", "pb", "ps_ttm",
           "turnover_rate", "last_price"]

# 东财字段名 -> 我们的列名
EM_MAP = {
    "TRADE_DATE": "trade_date",
    "SECURITY_CODE": "code",
    "TOTAL_SHARES": "total_shares",
    "FREE_SHARES_A": "free_shares",
    "TOTAL_MARKET_CAP": "total_market_cap",
    "NOTLIMITED_MARKETCAP_A": "circulating_market_cap",
    "PE_TTM": "pe_ttm",
    "PB_MRQ": "pb",
    "PS_TTM": "ps_ttm",
    "CLOSE_PRICE": "last_price",
}


def _f(v):
    """东财的空值/占位符统一成 None。

    它用 '-' 表示缺数据，直接塞进 NUMERIC 列会报错；而 0 也不对
    （市值 0 会让「取最小市值」的策略选出这一只）。所以必须转 None。
    """
    if v is None or v == "" or v == "-":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def fetch_em_valuation(code: str, start: str, end: str,
                       page_size: int = 500, max_pages: int = 20) -> list[dict]:
    """东财 datacenter-web 的估值历史序列。

    分页是因为单页有上限；按 TRADE_DATE 倒序取，所以要在本地再排一次。
    """
    out: list[dict] = []
    flt = urllib.parse.quote('(SECURITY_CODE="%s")' % code, safe="()=")
    for page in range(1, max_pages + 1):
        url = EM_VAL_URL % {"filter": flt, "page": page, "size": page_size}
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Referer": "https://data.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        res = (d.get("result") or {})
        rows = res.get("data") or []
        if not rows:
            break
        for it in rows:
            td = str(it.get("TRADE_DATE") or "")[:10]
            if not td:
                continue
            if td < start or td > end:
                continue
            row = {v: _f(it.get(k)) for k, v in EM_MAP.items()}
            row["code"] = code
            row["trade_date"] = td
            # 东财不给换手率，用「成交量/流通股本」算不了（没有成交量字段），
            # 留 None 而不是编一个
            row.setdefault("turnover_rate", None)
            row["source"] = "em_datacenter"
            out.append(row)
        if len(rows) < page_size:
            break
        time.sleep(0.25)
    return sorted(out, key=lambda r: r["trade_date"])


def tx_symbol(code: str) -> str:
    c = str(code).strip()
    if c.startswith(("sh", "sz", "bj")):
        return c
    if c.startswith(("5", "6", "9")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


def fetch_tx_quote(codes: list) -> dict:
    """腾讯实时行情（当天快照）。返回 {code: {...}}，市值单位换算成**元**。

    腾讯给的是「亿元」，而东财给「元」。这里统一成元 ——
    混用单位是最容易出的错：市值差 1e8 倍会让「取最小市值」的策略
    选出的完全是另一批票，而且不报错。
    """
    q = ",".join(tx_symbol(c) for c in codes)
    req = urllib.request.Request(TX_QUOTE_URL + q,
                                 headers={"User-Agent": UA,
                                          "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        txt = r.read().decode("gbk", "replace")
    today = date.today().isoformat()
    out = {}
    for line in txt.strip().split("\n"):
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        code = left.replace("v_", "")
        for p in ("sh", "sz", "bj"):
            if code.startswith(p):
                code = code[len(p):]
                break
        f = right.strip().strip(";").strip('"').split("~")
        if len(f) < 50:
            continue
        yi = _f(f[45]) if len(f) > 45 else None     # 总市值（亿元）
        yi_c = _f(f[44]) if len(f) > 44 else None   # 流通市值（亿元）
        out[code] = {
            "code": code, "trade_date": today,
            "last_price": _f(f[3]), "total_shares": None, "free_shares": None,
            "total_market_cap": yi * 1e8 if yi is not None else None,
            "circulating_market_cap": yi_c * 1e8 if yi_c is not None else None,
            "pe_ttm": _f(f[39]) if len(f) > 39 else None,
            "pb": _f(f[46]) if len(f) > 46 else None,
            "ps_ttm": None,
            "turnover_rate": _f(f[38]) if len(f) > 38 else None,
            "source": "tx_quote",
        }
    return out


def store_valuation(cur, rows: list[dict]) -> int:
    """落库（upsert）。返回写入行数。

    用 execute_values 而不是 executemany：远程云库单行往返 ~65ms，
    executemany 800 行要 52 秒，execute_values 一次就完（实测同样的
    名册刷新 200s -> 20.8s）。
    """
    from psycopg2.extras import execute_values
    rows = [r for r in rows if r.get("code") and r.get("trade_date")]
    if not rows:
        return 0
    vals = [(r["code"], r["trade_date"]) + tuple(r.get(c) for c in COLUMNS)
            + (r.get("source") or "",) for r in rows]
    execute_values(cur, """
        INSERT INTO sa_stock_valuation
            (code, trade_date, total_shares, free_shares, total_market_cap,
             circulating_market_cap, pe_ttm, pb, ps_ttm, turnover_rate,
             last_price, source)
        VALUES %s
        ON CONFLICT (code, trade_date) DO UPDATE SET
            total_shares=EXCLUDED.total_shares,
            free_shares=EXCLUDED.free_shares,
            total_market_cap=EXCLUDED.total_market_cap,
            circulating_market_cap=EXCLUDED.circulating_market_cap,
            pe_ttm=EXCLUDED.pe_ttm, pb=EXCLUDED.pb, ps_ttm=EXCLUDED.ps_ttm,
            turnover_rate=EXCLUDED.turnover_rate,
            last_price=EXCLUDED.last_price, source=EXCLUDED.source""",
        vals, page_size=500)
    return len(vals)


def sync_valuation(get_conn, code: str, years: float = 4.0,
                   end: str = None) -> dict:
    """同步一只标的的估值（增量：只补库里没有的日期）。"""
    end = end or date.today().isoformat()
    start = (date.today() - timedelta(days=int(years * 365))).isoformat()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM sa_stock_valuation WHERE code=%s",
                    (code,))
        have = cur.fetchone()[0]
        if have and str(have) >= end:
            return {"code": code, "fetched": 0, "up_to_date": True,
                    "last_date": str(have)}
        frm = (str(have) + "T00:00:00") if have else start
        errs = []
        rows = []
        try:
            rows = fetch_em_valuation(code, start, end)
        except Exception as exc:                # noqa: BLE001
            errs.append("em_datacenter: %s" % str(exc)[:100])
        # 主源失败或缺当天时，用腾讯补当天快照
        try:
            snap = fetch_tx_quote([code]).get(code)
        except Exception as exc:                # noqa: BLE001
            snap = None
            errs.append("tx_quote: %s" % str(exc)[:100])
        if snap:
            have_dates = {r["trade_date"] for r in rows}
            if snap["trade_date"] not in have_dates:
                rows.append(snap)
        if not rows:
            return {"code": code, "fetched": 0, "errors": errs,
                    "why": "两个源都没数据"}
        n = store_valuation(cur, rows)
        conn.commit()
    return {"code": code, "fetched": n, "from": frm, "to": end,
            "errors": errs, "oldest": rows[0]["trade_date"],
            "newest": rows[-1]["trade_date"],
            "sources": sorted({r.get("source", "") for r in rows})}


def coverage(get_conn, codes: list) -> list[dict]:
    """看估值数据覆盖（缺数据就别指望跑估值类策略）。"""
    if not codes:
        return []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT code, count(*), min(trade_date), max(trade_date),
                              count(*) FILTER (WHERE total_market_cap IS NOT NULL)
                       FROM sa_stock_valuation WHERE code = ANY(%s)
                       GROUP BY code ORDER BY count(*) DESC""",
                    (list(codes),))
        got = {r[0]: r[1:] for r in cur.fetchall()}
    return [{"code": c, "days": (got.get(c) or (0,))[0],
             "from": (got.get(c) or (0, None))[1],
             "to": (got.get(c) or (0, None, None))[2],
             "days_with_cap": (got.get(c) or (0, 0, None, None))[3]}
            for c in codes]
