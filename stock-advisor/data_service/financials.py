# -*- coding: utf-8 -*-
"""财务数据代理：业绩报表 / 业绩预告 / 业绩快报 / 机构一致预期 / 财务摘要。

为什么放在 101
--------------
用户口径（2026-10-22）：**本机不直连任何数据源**。业绩与我们关心的「披露前冻结预期、
披露后核对实际」全靠这里，本机只看 HTTP。

实测（101 容器 akshare **1.18.97**，2026-10-29）
------------------------------------------------
| 接口 | 实测 |
|---|---|
| `stock_yjbb_em(date="20260930")` | 4254 行 / **冷启动 229s**、热 2s |
| `stock_yjyg_em(date="20260930")` | 531 行 / 0.5s |
| `stock_yjkb_em(date="20260930")` | 11 行 / 0.1s |
| `stock_profit_forecast_em()` | 2824 行 / 1.4s |
| `stock_financial_abstract(symbol=)` | 80 行 / 0.5s |

口径（别改错）
--------------
- **单位**：净利润/营收一律 **元**（源就是元，不换万元）；增长率是 **%**；EPS 是 **元/股**。
  换算成亿元/万元是展示层的事，服务端不 round、不换单位。
- `stock_yjbb_em` 的 `净利润-净利润` 是**归母净利润**（东财业绩报表口径），
  字段名里没有「归母」二字，别当成本净利润。
- 全表按报告期缓存在进程内，**按 codes 过滤发生在缓存之外**
  —— 否则「这次查 3 只、下次查另外 3 只」会把整张 4254 行的表重复拉 2 次。
- 报告期只接受季末（0331/0630/0930/1231）；非法期直接 400，不让 akshare 静默回空表。
- **不做标的白名单**：过滤权在调用方（谁的持仓谁关心），加标的不用重建镜像。
"""
from __future__ import annotations

import math
import os
import re
import threading
import time

from .market import UpstreamError

PERIOD_RE = re.compile(r"^(?:19|20)\d{2}(?:0331|0630|0930|1231)$")
CODE_RE = re.compile(r"^\d{6}$")

#: 报告期表（当日披露季内每天在变）15 分钟；行业级（一致预期/摘要）1 小时。
TTL_TABLE = float(os.environ.get("SA_FIN_TTL_TABLE", "900"))
TTL_SLOW = float(os.environ.get("SA_FIN_TTL_SLOW", "3600"))

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_MAX = 64

#: 返回给客户端的单位声明，避免下游再把「元」当「万元」用。
UNITS = {"net_profit": "元", "revenue": "元", "eps": "元/股",
         "growth": "%", "gross_margin": "%"}


def _ak():
    import akshare
    return akshare


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _s(v) -> str:
    s = str(v if v is not None else "").strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _col(row, *names):
    """按候选列名取值 —— akshare 改了列名不该让整个接口变空。"""
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _norm_period(v: str) -> str:
    s = re.sub(r"\D", "", str(v or ""))
    if not PERIOD_RE.match(s):
        raise ValueError("period 应为季末报告期，如 20260930 / 2026-09-30")
    return s


def _iso(period: str) -> str:
    return "%s-%s-%s" % (period[:4], period[4:6], period[6:])


def _norm_codes(codes) -> list[str] | None:
    if not codes:
        return None
    if isinstance(codes, str):
        items = [c.strip() for c in codes.split(",")]
    else:
        items = [str(c).strip() for c in codes]
    out = []
    for c in items:
        if not c:
            continue
        if not CODE_RE.match(c):
            raise ValueError("codes 里含非法代码 %r（只接受 6 位数字）" % (c,))
        if c not in out:
            out.append(c)
    return out or None


def _cached(key: str, ttl: float, fn):
    """命中与回源在同一把锁里（与 valuation.py 同范式）。

    yjbb 冷启动实测 229s，并发回源会把同花顺打爆并让每个请求都等满。
    """
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        out = fn()
        _CACHE[key] = (time.time(), out)
        if len(_CACHE) > _CACHE_MAX:
            cut = time.time() - TTL_SLOW
            for k in [k for k, (t, _x) in _CACHE.items() if t < cut]:
                _CACHE.pop(k, None)
        return out


def _pick(items: list[dict], codes: list[str] | None) -> list[dict]:
    if not codes:
        return items
    want = set(codes)
    return [it for it in items if it["code"] in want]


def _guard(name: str, period: str, exc: Exception):
    raise UpstreamError("%s %s 取数失败：%s: %s"
                        % (name, period, type(exc).__name__,
                           str(exc)[:160])) from exc


# ---------------- 业绩报表（正式披露值，东财口径） ----------------

def _report_all(period: str) -> list[dict]:
    def run():
        ak = _ak()
        try:
            df = ak.stock_yjbb_em(date=period)
        except Exception as exc:
            _guard("业绩报表", period, exc)
        if df is None or not len(df):
            return []
        out: list[dict] = []
        for _, r in df.iterrows():
            code = _s(_col(r, "股票代码"))
            if not CODE_RE.match(code):
                continue
            out.append({
                "code": code,
                "name": _s(_col(r, "股票简称")),
                "period": _iso(period),
                "source": "yjbb",
                "net_profit": _f(_col(r, "净利润-净利润", "净利润")),
                "revenue": _f(_col(r, "营业总收入-营业总收入", "营业收入-营业收入",
                                    "营业总收入")),
                "eps": _f(_col(r, "每股收益")),
                "net_profit_yoy": _f(_col(r, "净利润-同比增长")),
                "revenue_yoy": _f(_col(r, "营业总收入-同比增长",
                                        "营业收入-同比增长")),
                "gross_margin": _f(_col(r, "销售毛利率", "毛利率")),
                "industry": _s(_col(r, "所处行业")),
                "announce_date": _s(_col(r, "最新公告日期", "公告日期")),
            })
        return out
    return _cached("report:" + period, TTL_TABLE, run)


def report(period: str, codes=None) -> dict:
    """正式业绩报表。返回 {period, units, count, items: [...]}。

    注意：只有**已披露**的公司才在表里；「查不到」= 还没披露，不等于亏损。
    """
    p = _norm_period(period)
    want = _norm_codes(codes)
    rows = _report_all(p)
    items = _pick(rows, want)
    missing = [c for c in (want or []) if c not in {it["code"] for it in items}]
    notes = []
    if not rows:
        notes.append("报告期 %s 东财业绩报表为空（可能尚未进入披露期）" % _iso(p))
    return {"period": _iso(p), "units": UNITS, "count": len(items),
            "items": items, "missing": missing, "notes": notes}


# ---------------- 业绩预告（预告区间，未审计） ----------------

def _preannounce_all(period: str) -> list[dict]:
    def run():
        ak = _ak()
        try:
            df = ak.stock_yjyg_em(date=period)
        except Exception as exc:
            _guard("业绩预告", period, exc)
        if df is None or not len(df):
            return []
        out: list[dict] = []
        for _, r in df.iterrows():
            code = _s(_col(r, "股票代码"))
            if not CODE_RE.match(code):
                continue
            out.append({
                "code": code,
                "name": _s(_col(r, "股票简称")),
                "period": _iso(period),
                "source": "yjyg",
                "indicator": _s(_col(r, "预测指标")),
                "change_text": _s(_col(r, "业绩变动")),
                "reason": _s(_col(r, "业绩变动原因")),
                "kind": _s(_col(r, "预告类型")),          # 预增/预减/扭亏/首亏/续盈/续亏/略增/略减/不确定
                # 预告区间的**中值**（源给的是 midpoint），不是实际值
                "profit_mid": _f(_col(r, "预测数值")),
                "change_pct": _f(_col(r, "业绩变动幅度")),
                "last_year": _f(_col(r, "上年同期值")),
                "announce_date": _s(_col(r, "公告日期")),
            })
        return out
    return _cached("pre:" + period, TTL_TABLE, run)


def preannounce(period: str, codes=None) -> dict:
    """业绩预告。`profit_mid` 是**预告区间中值**，不是实际值。"""
    p = _norm_period(period)
    want = _norm_codes(codes)
    rows = _pick(_preannounce_all(p), want)
    return {"period": _iso(p), "units": UNITS, "count": len(rows),
            "items": rows,
            "missing": [c for c in (want or [])
                        if c not in {it["code"] for it in rows}],
            "notes": [] if rows else ["报告期 %s 无业绩预告记录" % _iso(p)]}


# ---------------- 业绩快报（未正式审计的近似值） ----------------

def _express_all(period: str) -> list[dict]:
    def run():
        ak = _ak()
        try:
            df = ak.stock_yjkb_em(date=period)
        except Exception as exc:
            _guard("业绩快报", period, exc)
        if df is None or not len(df):
            return []
        out: list[dict] = []
        for _, r in df.iterrows():
            code = _s(_col(r, "股票代码"))
            if not CODE_RE.match(code):
                continue
            out.append({
                "code": code,
                "name": _s(_col(r, "股票简称")),
                "period": _iso(period),
                "source": "yjkb",
                "net_profit": _f(_col(r, "净利润-净利润", "净利润")),
                "revenue": _f(_col(r, "营业收入-营业收入", "营业总收入-营业总收入")),
                "eps": _f(_col(r, "每股收益")),
                "net_profit_yoy": _f(_col(r, "净利润-同比增长")),
                "revenue_yoy": _f(_col(r, "营业收入-同比增长")),
                "industry": _s(_col(r, "所处行业")),
                "announce_date": _s(_col(r, "公告日期")),
            })
        return out
    return _cached("exp:" + period, TTL_TABLE, run)


def express(period: str, codes=None) -> dict:
    """业绩快报（比正式报表早、但未经审计）。"""
    p = _norm_period(period)
    want = _norm_codes(codes)
    rows = _pick(_express_all(p), want)
    return {"period": _iso(p), "units": UNITS, "count": len(rows),
            "items": rows,
            "missing": [c for c in (want or [])
                        if c not in {it["code"] for it in rows}],
            "notes": [] if rows else ["报告期 %s 无业绩快报" % _iso(p)]}


# ---------------- 机构一致预期（**年度** EPS，唯一可得的口径） ----------------

def _consensus_all() -> list[dict]:
    def run():
        ak = _ak()
        try:
            df = ak.stock_profit_forecast_em()
        except Exception as exc:
            _guard("机构一致预期", "latest", exc)
        if df is None or not len(df):
            return []
        eps_cols = [c for c in df.columns if "预测每股收益" in str(c)]
        out: list[dict] = []
        for _, r in df.iterrows():
            code = _s(_col(r, "代码"))
            if not CODE_RE.match(code):
                continue
            eps = {}
            for c in eps_cols:
                year = re.sub(r"\D", "", str(c))[:4]
                v = _f(r.get(c))
                if year and v is not None:
                    eps[year] = v
            if not eps:
                continue
            out.append({
                "code": code,
                "name": _s(_col(r, "名称")),
                "source": "profit_forecast",
                "reports": _f(_col(r, "研报数")),
                "eps_by_year": eps,
                "ratings": {
                    "buy": _f(_col(r, "机构投资评级(近六个月)-买入")),
                    "add": _f(_col(r, "机构投资评级(近六个月)-增持")),
                    "neutral": _f(_col(r, "机构投资评级(近六个月)-中性")),
                    "reduce": _f(_col(r, "机构投资评级(近六个月)-减持")),
                    "sell": _f(_col(r, "机构投资评级(近六个月)-卖出")),
                },
            })
        return out
    return _cached("consensus", TTL_SLOW, run)


def consensus(codes=None) -> dict:
    """机构一致预期 —— **只有年度 EPS，没有季度共识**。

    这是本功能最硬的缺口：用户口中的「季度预期 9 亿」在任何公开免费源里都取不到，
    所以客户端必须把「年度共识 × 季节性」的推算明确标成**推算**，不能冒充共识。
    """
    want = _norm_codes(codes)
    rows = _pick(_consensus_all(), want)
    return {"units": {"eps": "元/股"}, "count": len(rows), "items": rows,
            "missing": [c for c in (want or [])
                        if c not in {it["code"] for it in rows}],
            "notes": ["一致预期只有年度 EPS，无季度共识"]}


# ---------------- 财务摘要（历史序列，算季节性比例用） ----------------

_KEEP = {
    "归母净利润": "net_profit",
    "扣非净利润": "deducted_net_profit",
    "营业总收入": "revenue",
    "基本每股收益": "eps",
    "毛利率": "gross_margin",
    "净利润": "net_profit_total",
}


def _abstract_one(code: str) -> dict:
    def run():
        ak = _ak()
        try:
            df = ak.stock_financial_abstract(symbol=code)
        except Exception as exc:
            _guard("财务摘要", code, exc)
        if df is None or not len(df):
            return {"code": code, "periods": [], "series": {}}
        periods: list[str] = []
        series: dict[str, dict[str, float]] = {}
        for _, r in df.iterrows():
            if _s(r.get("选项")) != "常用指标":
                continue
            label = _s(r.get("指标"))
            key = _KEEP.get(label)
            if not key or key in series:
                continue
            vals: dict[str, float] = {}
            for c in df.columns:
                cs = str(c)
                if not re.fullmatch(r"\d{8}", cs):
                    continue
                v = _f(r.get(c))
                if v is None:
                    continue
                day = "%s-%s-%s" % (cs[:4], cs[4:6], cs[6:])
                vals[day] = v
                if day not in periods:
                    periods.append(day)
            if vals:
                series[key] = vals
        periods.sort()
        return {"code": code, "periods": periods, "series": series}
    return _cached("abs:" + code, TTL_SLOW, run)


def abstract(code: str) -> dict:
    """单只票的历史财务序列（元 / 元每股 / %）。"""
    c = str(code or "").strip()
    if not CODE_RE.match(c):
        raise ValueError("code 应为 6 位数字，例如 300394")
    d = _abstract_one(c)
    return {"code": c, "units": UNITS, "periods": d["periods"],
            "series": d["series"],
            "notes": [] if d["series"] else ["财务摘要无常用指标数据"]}


# ---------------- 一次拿齐（客户端主用） ----------------

def bundle(code: str, period: str) -> dict:
    """一只票 + 一个报告期的全部可用财务事实。

    各子表独立 try/except：**一个源挂了不能让整只票的预案变成空白**
    （宁可少一块，也要把已经拿到的部分交出去，并在 parts 里标明哪块失败）。
    """
    c = str(code or "").strip()
    if not CODE_RE.match(c):
        raise ValueError("code 应为 6 位数字，例如 300394")
    p = _norm_period(period)

    out: dict = {"code": c, "period": _iso(p), "units": UNITS, "parts": {}}
    parts = out["parts"]

    def one(name: str, fn):
        try:
            parts[name] = fn()
        except (ValueError, UpstreamError) as exc:
            parts[name] = None
            out.setdefault("errors", {})[name] = str(exc)[:200]
        except Exception as exc:                        # noqa: BLE001
            parts[name] = None
            out.setdefault("errors", {})[name] = "%s: %s" % (
                type(exc).__name__, str(exc)[:160])

    one("report", lambda: (report(p, [c])["items"] or [None])[0])
    one("preannounce", lambda: (preannounce(p, [c])["items"] or [None])[0])
    one("express", lambda: (express(p, [c])["items"] or [None])[0])
    one("consensus", lambda: (consensus([c])["items"] or [None])[0])
    one("abstract", lambda: abstract(c))
    return out
