"""现金基准（LPR + 货基 7 日年化）采集 —— 决策的「无风险机会成本」数据源。

2026-11-08 新增。背景：主应用决策上下文里只有现金余额数字，没有
「不买股票、持有存款/货基也能稳赚 X%」这条机会成本线，LLM 看到
「近20日涨37%」就倾向追 —— 用户要求把存款/现金类 baseline 接进决策。

为什么放在本服务（而不是主应用本机直连 akshare）：所有取数统一在
101 data_service（2026-11-08 用户定），主应用只消费 HTTP。

数据源实测（本机 akshare 1.20.35 + 101 容器 akshare 1.18.97，双端通过）：
- LPR   ``ak.macro_china_lpr()``            月频，列 TRADE_DATE/LPR1Y/LPR5Y
- 货基  ``ak.fund_money_fund_daily_em()``   545 只，列 f"{date}-7日年化%"
        （带 ``%`` 的字符串，``---`` 表示缺）

已否决（别再试）：
- ``ak.macro_rmb_deposit`` —— 是「新增存款**量**」，不是利率；
- 东财 ``RPT_ECONOMY_DEPOSIT_RATE`` —— 是央行**基准**利率，2015-10-24 起
  冻结在 1.5%，远高于现行大行挂牌 ~0.95%，拿去当基准会高估无风险收益。

⚠️ 存款**挂牌利率**没有可靠在线源（各银行自定、东财那页只有冻结的基准），
由主应用 config 手工维护，本服务不提供。
"""
from __future__ import annotations

import os
import statistics
import threading
import time
from datetime import datetime

#: 快照缓存秒数。LPR 月频、货基日频，6 小时足够；env 可覆盖。
CACHE_TTL = float(os.environ.get("SA_RATES_CACHE_TTL", "21600"))

_lock = threading.Lock()
_cache: dict = {"snap": None, "at": 0.0}


class UpstreamError(Exception):
    """两个源都失败且没有旧快照时抛给 HTTP 层（对齐 valuation/market 惯例）。"""


def _num(v) -> float | None:
    """数值闸门：str/Decimal/None -> float|None。

    ⚠️ NaN 检查必须用 ``f != f``（AGENTS.md 老坑：``float('nan')`` 不抛异常，
    ``v == v`` 对 NaN 是 False 但对 None 会先在 float(None) 崩）。"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%")
        if not v or v in {"---", "--", "-"}:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def fetch_lpr(fn=None) -> dict | None:
    """LPR 1年/5年。返回 {"date", "lpr1y", "lpr5y"}，无有效数据返回 None。

    ``fn`` 可注入（离线测试）：无参调用、返回带 TRADE_DATE/LPR1Y/LPR5Y 键的
    dict 行（pandas ``tail(1)`` 后的记录）。"""
    if fn is None:
        import akshare as ak
        df = ak.macro_china_lpr()
        rows = df.tail(1).to_dict("records")
        if not rows:
            return None
        row = rows[0]
    else:
        row = fn()
    out = {"date": str(row.get("TRADE_DATE", ""))[:10]}
    for k, col in (("lpr1y", "LPR1Y"), ("lpr5y", "LPR5Y")):
        v = _num(row.get(col))
        if v is not None:
            out[k] = v
    return out if len(out) > 1 else None


def fetch_money_fund(columns=None, get_fn=None, fn=None) -> dict | None:
    """货基 7 日年化的**中位数**（单只基金有噪音，中位数当市场水平更稳）。

    返回 {"date", "median", "count", "min", "max"}。可解析条目 < 30 视为
    源异常（整个接口挂掉通常表现为列全空），返回 None 而不是给一个
    少样本中位数。

    ``fn`` 可注入（离线测试）：无参调用、返回带 ``columns`` 与
    ``__getitem__`` 的对象（pandas DataFrame 形状即可）。
    """
    if fn is None:
        import akshare as ak
        fn = ak.fund_money_fund_daily_em
    df = fn() if columns is None else None
    cols = list(df.columns) if df is not None else list(columns)
    col = next((c for c in cols if "7日年化" in str(c)), None)
    if col is None:
        raise ValueError(f"返回列里没有 7 日年化: {cols[:6]}...")
    raw = df[col] if df is not None else get_fn(col)
    vals = [v for v in (_num(x) for x in raw) if v is not None]
    if len(vals) < 30:
        raise ValueError(f"可解析的 7 日年化只有 {len(vals)} 条，疑似源异常")
    asof = str(col).split("-7日年化")[0]
    return {"date": asof, "median": round(statistics.median(vals), 3),
            "count": len(vals), "min": round(min(vals), 3),
            "max": round(max(vals), 3)}


def snapshot(force: bool = False, lpr_fn=None, mf_fn=None) -> dict:
    """两源合并快照（带 TTL 缓存）。结构对齐 financials.bundle：
    每块独立失败（null + errors），一个源挂了不拖垮另一个。

    两个源都失败时：有旧快照则沿用并在 notes 里说明（决策宁可用 6 小时前
    的利率也比空白强），没有才抛 UpstreamError。
    """
    now = time.time()
    with _lock:
        if not force and _cache["snap"] is not None \
                and (now - _cache["at"]) < CACHE_TTL:
            return _cache["snap"]

    data: dict = {"lpr": None, "money_fund": None}
    errors: dict = {}
    for key, fn in (("lpr", lpr_fn or fetch_lpr),
                    ("money_fund", mf_fn or fetch_money_fund)):
        try:
            data[key] = fn()
        except Exception as exc:                        # noqa: BLE001
            errors[key] = f"{type(exc).__name__}: {exc}"[:300]

    with _lock:
        stale = _cache["snap"]
    notes: list = []
    if data["lpr"] is None and data["money_fund"] is None:
        if stale:
            notes.append(f"两个源都失败，沿用 {stale.get('fetched_at')} 的旧快照")
            out = {**stale, "stale": True, "errors": errors, "notes": notes}
            return out
        raise UpstreamError("LPR 与货基两个源都失败且没有旧快照: "
                            + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    if errors:
        notes.append("部分源失败，缺的块见 errors")

    out = {"fetched_at": datetime.now().isoformat(timespec="seconds"),
           "lpr": data["lpr"], "money_fund": data["money_fund"],
           "errors": errors, "notes": notes}
    with _lock:
        _cache.update({"snap": out, "at": time.time()})
    return out


def _reset_cache() -> None:
    """仅测试用：清空模块级快照缓存。"""
    with _lock:
        _cache.update({"snap": None, "at": 0.0})
