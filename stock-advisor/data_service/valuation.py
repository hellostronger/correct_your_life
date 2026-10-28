# -*- coding: utf-8 -*-
"""估值代理：申万行业 / 中证指数的 PE 与收盘（市场温度计专用）。

为什么放在 101
--------------
用户口径（2026-10-22 行情、2026-10-28 扩到估值）：**本机不直连任何数据源**。
akshare 的申万 / 中证接口在 101 上实测可用（容器内 akshare **1.18.97**）：

| 接口 | 实测 |
|---|---|
| `index_analysis_week_month_sw("month")` | 321 行 / 7.0s |
| `index_analysis_monthly_sw("一级行业", 20260930)` | 31 行 / 13.7s |
| `index_analysis_daily_sw("一级行业", 20261027)` | 31 行 / 14.3s |
| `stock_zh_index_hist_csindex("000300", 20161201..)` | 2404 行 / 0.6s |

口径（别改错）
--------------
- **服务端不做标的过滤**：选哪些指数是客户端（`market_temperature.UNIVERSE`）的事。
  过滤放这里会让「加一个标的」变成「必须重建镜像」。
- 只留 **6 位纯数字代码**（申万的表里混着非指数行）。
- `/index-hist` 只回精简 `{d, close, pe}` —— 原始表 16 列 × 2400 行，回传是几 MB 的浪费。
- `pe` 是源原值（申万=指数市盈率、中证=**滚动**市盈率），`close` 是收盘点位，
  两者都不做换算、不 round（round 在展示层做）。
- 单位/时间：`d` 一律 `YYYY-MM-DD`，与客户端采样日同口径。

缓存 + 一把全局锁
-----------------
101 上月报/日报是 14s 级，而同一份数据一天内不变 ⇒ 进程内 TTL 缓存。
月报/日历/历史 12 小时（可用 `SA_VALUATION_TTL_LONG` 覆盖），
日报 10 分钟（当日 PE 随行情变，`SA_VALUATION_TTL_DAY`）。

**缓存命中与回源都在同一把锁里**：akshare 底层打同花顺/中证，并发请求既慢又容易被
对端限流；宁可让第二个请求排队，也不要打出并发风暴。
"""

from __future__ import annotations

import math
import os
import re
import threading
import time

from .market import UpstreamError

CODE_RE = re.compile(r"^\d{6}$")

TTL_LONG = float(os.environ.get("SA_VALUATION_TTL_LONG", "43200"))
TTL_DAY = float(os.environ.get("SA_VALUATION_TTL_DAY", "600"))

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_MAX = 512


def _ak():
    import akshare
    return akshare


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _norm_date(v: str) -> str:
    s = re.sub(r"\D", "", str(v or ""))
    if len(s) != 8:
        raise ValueError("date 应为 YYYYMMDD 或 YYYY-MM-DD，例如 20260930")
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:])


def _cached(key: str, ttl: float, fn):
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
            cut = time.time() - TTL_LONG
            for k in [k for k, (t, _v) in _CACHE.items() if t < cut]:
                _CACHE.pop(k, None)
        return out


def _merge(items: dict[str, dict], df) -> None:
    for _, r in df.iterrows():
        code = str(r.get("指数代码") or "").strip()
        if not CODE_RE.match(code):
            continue
        pe, close = _f(r.get("市盈率")), _f(r.get("收盘指数"))
        if pe is None and close is None:
            continue
        items[code] = {"pe": pe, "close": close,
                       "name": str(r.get("指数名称") or "")}


def sw_calendar() -> dict:
    """申万公布过的月末交易日。返回 {dates: ['2016-12-30', ...]}。"""
    def run():
        ak = _ak()
        try:
            df = ak.index_analysis_week_month_sw(symbol="month")
        except Exception as exc:
            raise UpstreamError("申万月末日历取不到：%s: %s"
                                % (type(exc).__name__, str(exc)[:160])) from exc
        if df is None or not len(df):
            raise UpstreamError("申万月末日历返回空表")
        col = df.columns[0]
        dates = sorted({s for s in (str(v)[:10] for v in df[col].tolist())
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s)})
        if not dates:
            raise UpstreamError("申万月末日历没有可用日期（首列=%r）" % (col,))
        return {"dates": dates}
    return _cached("calendar", TTL_LONG, run)


def sw_month(date_str: str) -> dict:
    """某个月末 → {code: {pe, close, name}}（一级行业 + 市场表征）。"""
    d = _norm_date(date_str)

    def run():
        ak = _ak()
        items: dict[str, dict] = {}
        notes: list[str] = []
        for sym in ("一级行业", "市场表征"):
            try:
                df = ak.index_analysis_monthly_sw(symbol=sym,
                                                  date=d.replace("-", ""))
            except Exception as exc:
                notes.append("%s 取数失败 %s" % (sym, type(exc).__name__))
                continue
            if df is None or not len(df):
                notes.append("%s 返回空表" % sym)
                continue
            _merge(items, df)
        if not items:
            raise UpstreamError("申万月报 %s 无数据（%s）"
                                % (d, "; ".join(notes) or "两个表都返回空"))
        return {"date": d, "items": items, "notes": notes}
    return _cached("month:" + d, TTL_LONG, run)


def sw_day(date_str: str) -> dict:
    """单个交易日 → {code: {pe, close, name}}（全部一级行业 + 市场表征）。"""
    d = _norm_date(date_str)

    def run():
        ak = _ak()
        items: dict[str, dict] = {}
        notes: list[str] = []
        for sym in ("一级行业", "市场表征"):
            try:
                df = ak.index_analysis_daily_sw(symbol=sym,
                                                start_date=d.replace("-", ""),
                                                end_date=d.replace("-", ""))
            except Exception as exc:
                notes.append("%s 取数失败 %s" % (sym, type(exc).__name__))
                continue
            if df is None or not len(df):
                notes.append("%s 返回空表" % sym)
                continue
            _merge(items, df)
        if not items:
            raise UpstreamError("申万日报 %s 无数据（%s）"
                                % (d, "; ".join(notes) or "两个表都返回空"))
        return {"date": d, "items": items, "notes": notes}
    return _cached("day:" + d, TTL_DAY, run)


def index_hist(code: str, start: str, end: str) -> dict:
    """中证指数日线 → {code, start, end, rows: [{d, close, pe}]}。"""
    code = str(code or "").strip()
    if not CODE_RE.match(code):
        raise ValueError("code 应为 6 位数字指数代码，例如 000300 / 931573")
    s, e = _norm_date(start).replace("-", ""), _norm_date(end).replace("-", "")
    if s > e:
        raise ValueError("start 不能晚于 end（%s > %s）" % (s, e))

    def run():
        ak = _ak()
        try:
            df = ak.stock_zh_index_hist_csindex(symbol=code, start_date=s,
                                                end_date=e)
        except Exception as exc:
            raise UpstreamError("中证指数 %s 日线取不到：%s: %s"
                                % (code, type(exc).__name__,
                                   str(exc)[:160])) from exc
        if df is None or not len(df):
            raise UpstreamError("中证指数 %s 返回空表（%s~%s）" % (code, s, e))
        rows: list[dict] = []
        for _, r in df.iterrows():
            day = str(r.get("日期") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                continue
            rows.append({"d": day,
                         "close": _f(r.get("收盘")),
                         "pe": _f(r.get("滚动市盈率"))})
        rows.sort(key=lambda x: x["d"])
        if not rows:
            raise UpstreamError("中证指数 %s 日线没有可用行（列=%s）"
                                % (code, "|".join(str(c) for c in df.columns)))
        return {"code": code, "start": s, "end": e, "rows": rows}
    return _cached("hist:%s:%s:%s" % (code, s, e), TTL_LONG, run)
