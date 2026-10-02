"""ipo_calendar.py —— A股/港股新股上市日历（申购→中签→缴款→上市 全时间轴）。

为什么要有：这是**唯一一个「已排期、能提前几天知道」的基本面冲击源**。
自选股里只要有一只即将上市的新股，它上市当天的资金分流、板块情绪外溢、
以及「同概念已上市标的被比价」都会影响持仓 —— 而这件事在现有的
「挖新股（stock_roster 扫正文找新公司）」里是**看不到的**：那边等的是公司
被提到，上市排期是另一条数据。

数据源（2026-10-01 实测）：
- A 股：`akshare.stock_xgsglb_em()`（东财）—— 4033 行 × 24 列，含
  申购日期 / 中签号公布日 / 中签缴款日期 / 上市日期 / 发行价 / 顶格申购需配市值。
  样例：某新股申购 2026-10-19、中签公布 10-21、缴款随后。
- 港股：`akshare.stock_ipo_hk_ths()`（同花顺）—— 港股 IPO 排期。

⚠️ 两个必须如实说明的局限（别把「查不到」当成「没这回事」）：

1. **港股覆盖不完整**。同花顺的港股 IPO 表只收录「已招股/即将招股」的一小部分，
   且中签/缴款日期常年为空 —— 港股没有 A 股那种统一摇号配售。所以港股这边
   只做「招股中 + 上市日」两个点，`status()` 里 `hk_covered` 会如实告诉你
   这次拿到了多少行。
2. **日期为空就是空**。新股还没走到那一步时，对应日期列就是 NaN。这里
   **不猜** —— 缺哪一步就只显示已有步骤，不按惯例补（否则会推出错误的
   「中签日」，而中签日是要拿真去申购的）。

缓存：data/ipo.json，每天同步一次即可（排期变动不频繁，但上市日会前移）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# 本机有系统代理（Windows WinINET），不设会让 akshare/requests 全报 ProxyError。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "data" / "ipo.json"

_lock = threading.Lock()

# 时间轴的四个步骤：(中文标签, 源列前缀, 级别)
STEPS = [
    ("申购日", "申购日期", "mid"),
    ("中签公布", "中签号公布日", "low"),
    ("缴款日", "中签缴款日期", "low"),
    ("上市日", "上市日期", "high"),      # 上市日才是真正影响盘面的那一天
]


def _to_date(v: Any) -> date | None:
    """akshare 给的是 Timestamp/NaT/None/str 混着，统一成 date 或 None。"""
    if v is None or v != v:                       # None / NaN / NaT
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s or s in ("nan", "NaT", "None", "-", "--"):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None


# ==========================================================================
# 取数 + 规范化
# ==========================================================================

def fetch_a() -> list[dict]:
    """A 股新股全表（akshare，东财口径）。"""
    import akshare as ak
    df = ak.stock_xgsglb_em()
    out = []
    for _, r in df.iterrows():
        code = str(r.get("股票代码") or "").strip()
        name = str(r.get("股票简称") or "").strip()
        if not code:
            continue
        item = {
            "market": "A",
            "code": code,
            "name": name,
            "exchange": str(r.get("交易所") or "").strip(),
            "board": str(r.get("板块") or "").strip(),
            "price": _num(r.get("发行价格")),
            "shares_wan": _num(r.get("发行总数")),
            "market_cap_need": _num(r.get("顶格申购需配市值")),
            # 打新策略要用的字段（2026-10-01 加）：
            # 中签率（发行公告披露后才非空）、发行/行业市盈率（判断稀缺性）、
            # 每中一签盈利 + 首日收盘 + 涨幅（已上市的用来校准收益模型）
            "lot_rate": _num(r.get("中签率")),
            "online_wan": _num(r.get("网上发行")),      # 万股：算中签率要它
            "sub_cap": _num(r.get("申购上限")),          # 万股：顶格能申购多少
            "issue_pe": _num(r.get("发行市盈率")),
            "industry_pe": _num(r.get("行业市盈率")),
            "profit_per_lot": _num(r.get("每中一签盈利")),
            "first_day_close": _num(r.get("首日收盘价")),
            "gain_pct": _num(r.get("涨幅")),
            "steps": {},
        }
        any_date = False
        for label, col, level in STEPS:
            d = _to_date(r.get(col))
            if d:
                item["steps"][label] = {"date": d.isoformat(), "level": level}
                any_date = True
        # 没有未来日期、但有已实现数据的（已上市新股）也留着 ——
        # 它们是**收益模型的校准样本**：ipo_strategy 用它们统计
        # 「各板块新股首日涨幅中位数」，用来给未来的新股做预估，
        # 免得用拍脑袋的涨幅假设。
        if not any_date and not item["first_day_close"]:
            continue
        out.append(item)
    return out


def fetch_hk() -> list[dict]:
    """港股新股（同花顺）。覆盖不全，返回空列表也不报错。"""
    import akshare as ak
    if not hasattr(ak, "stock_ipo_hk_ths"):
        return []
    df = ak.stock_ipo_hk_ths()
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        name = str(r.get("股票简称") or r.get("名称") or "").strip()
        if not name:
            continue
        code = str(r.get("股票代码") or r.get("代码") or "").strip()
        item = {"market": "H", "code": code, "name": name,
                "exchange": "香港交易所", "board": "",
                "price": _num(r.get("招股价") or r.get("发行价")),
                "shares_wan": None, "market_cap_need": None, "steps": {}}
        # 同花顺这表的列名每年变，按候选列名挨个试，找不到就留空（不猜）
        for label, cols, level in (
                ("申购日", ["申购日期", "招股日期", "认购日期"], "mid"),
                ("上市日", ["上市日期", "上市日"], "high")):
            for c in cols:
                if c in df.columns:
                    d = _to_date(r.get(c))
                    if d:
                        item["steps"][label] = {"date": d.isoformat(), "level": level}
                    break
        if item["steps"]:
            out.append(item)
    return out


def _num(v) -> float | None:
    if v is None or v != v:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sync(*, keep_days: int = 400) -> dict:
    """刷新缓存。A 股失败不影响港股，反之亦然。"""
    res: dict[str, Any] = {"ok": [], "failed": {}}
    items: list[dict] = []
    for mkt, fn in (("A", fetch_a), ("H", fetch_hk)):
        try:
            got = fn()
            items += got
            res["ok"].append({"market": mkt, "rows": len(got)})
        except Exception as exc:
            res["failed"][mkt] = f"{type(exc).__name__}: {str(exc)[:160]}"

    if items:
        # 只保留未来 keep_days 天内有任何步骤的 + 最近 30 天已上市的
        today = date.today()
        horizon = today + timedelta(days=keep_days)
        past = today - timedelta(days=30)

        def keep(it):
            for s in it["steps"].values():
                d = date.fromisoformat(s["date"])
                if past <= d <= horizon:
                    return True
            return False

        items = [it for it in items if keep(it)]
        items.sort(key=lambda it: min(s["date"] for s in it["steps"].values()))

    with _lock:
        payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "items": items, **res}
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(CACHE_FILE)
    return res


# ==========================================================================
# 查询
# ==========================================================================

def _load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"updated_at": "", "items": [], "ok": [], "failed": {}}


def status() -> dict:
    d = _load()
    return {"updated_at": d.get("updated_at", ""),
            "count": len(d.get("items") or []),
            "ok": d.get("ok") or [], "failed": d.get("failed") or {},
            "cache_file": str(CACHE_FILE)}


def upcoming(days: int = 30, *, market: str = "") -> list[dict]:
    """未来 days 天内的步骤（按日期升序，同日多条按市场/级别排）。"""
    d = _load()
    today = date.today()
    end = (today + timedelta(days=days)).isoformat()
    rows = []
    for it in d.get("items") or []:
        if market and it["market"] != market:
            continue
        for label, s in it["steps"].items():
            if today.isoformat() <= s["date"] <= end:
                rows.append({"date": s["date"], "step": label,
                             "level": s.get("level", "low"),
                             "market": it["market"], "code": it["code"],
                             "name": it["name"], "board": it.get("board") or "",
                             "price": it.get("price")})
    rows.sort(key=lambda r: (r["date"], r["market"], r["step"]))
    return rows


def calendar_events(days: int = 90) -> list[dict]:
    """给财经日历合并视图用（形态与 econ_calendar 的事件一致）。"""
    out = []
    for r in upcoming(days=days):
        out.append({
            "date": r["date"], "code": "ipo_" + r["market"],
            "title": f"{r['market']}股新股{r['step']}：{r['name']}({r['code']})",
            "time": "", "market": "cn" if r["market"] == "A" else "hk",
            "level": r["level"], "source": "ipo",
            "note": (f"发行价 {r['price']}／{r['board']}" if r.get("price")
                     else (r["board"] or "")),
        })
    return out


def by_code(code: str) -> dict | None:
    """按代码取一条完整记录（含 lot_rate / issue_pe 等策略字段）。"""
    want = str(code or "").strip()
    for it in _load().get("items") or []:
        if str(it.get("code")) == want:
            return it
    return None


def upcoming_full(days: int = 30, market: str = "") -> list[dict]:
    """未来 days 天内**有申购日**的新股完整记录（给打新策略用）。

    与 upcoming() 的区别：那个返回「步骤」，这个返回「整只票」——
    策略需要 code/价格/市值门槛/中签率才能算期望收益。
    """
    d = _load()
    today = date.today()
    end = (today + timedelta(days=days)).isoformat()
    out = []
    for it in d.get("items") or []:
        if market and it["market"] != market:
            continue
        sub = (it.get("steps") or {}).get("申购日")
        if not sub:
            continue
        if not (today.isoformat() <= sub["date"] <= end):
            continue
        out.append(dict(it, sub_date=sub["date"],
                        lead_days=(date.fromisoformat(sub["date"]) - today).days))
    out.sort(key=lambda x: x["lead_days"])
    return out


def listing_alerts(*, lead_days: int = 1, cooldown_hours: int = 12) -> list[dict]:
    """上市日提醒：提前 lead_days 天推一次（同向冷却）。"""
    d = _load()
    today = date.today()
    target = (today + timedelta(days=lead_days)).isoformat()
    state_file = BASE_DIR / "data" / "ipo_alert_state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"sent": {}}
    sent = state.setdefault("sent", {})
    out = []
    for it in d.get("items") or []:
        s = (it["steps"] or {}).get("上市日")
        if not s or s["date"] != target:
            continue
        key = f"{it['market']}:{it['code']}:{s['date']}"
        prev = float(sent.get(key) or 0)
        if prev and (time.time() - prev) < cooldown_hours * 3600:
            continue
        price = it.get("price")
        lines = [f"{it['market']}股 {it['name']}({it['code']}) 明日上市"]
        if price:
            lines.append(f"发行价 {price} 元")
        if it.get("market_cap_need"):
            lines.append(f"顶格申购需配市值 {it['market_cap_need']} 万元")
        lines.append("新股上市当日同板块已上市标的常被比价，情绪外溢需留意。")
        out.append({"key": key, "title": "🆕 新股上市提醒",
                    "content": "\n".join(lines),
                    "market": it["market"], "code": it["code"], "date": s["date"]})
        sent[key] = time.time()
    if out:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return out


def digest(days: int = 30) -> str:
    """给 LLM 报告用的 markdown（新股排期 + 打新策略要点）。"""
    rows = upcoming(days=days)
    if not rows:
        return ""
    lines = [f"### 新股日历（未来 {days} 天）", ""]
    lines.append("| 申购日 | 距今 | 市场 | 名称 | 代码 | 发行价 | 顶格市值门槛 |")
    lines.append("|---|---|---|---|---|---|---|")

    # 只对「即将申购」的票做策略评估（其余步骤行不重复列）
    subs = upcoming_full(days=days)
    sub_map = {r["code"]: r for r in subs}
    for r in rows:
        if r["step"] != "申购日":
            continue
        full = sub_map.get(r["code"]) or {}
        lines.append("| %s | T-%s 天 | %s | %s | %s | %s | %s 万 |" % (
            r["date"],
            full.get("lead_days", "—"),
            r["market"], r["name"], r["code"],
            r["price"] if r.get("price") else "—",
            full.get("market_cap_need") or "—"))

    other = [r for r in rows if r["step"] != "申购日"]
    if other:
        lines.append("")
        lines.append("其他关键节点：")
        for r in other[:12]:
            lines.append("- %s %s股 %s(%s)" % (r["date"], r["market"],
                                              r["name"], r["code"]))

    if subs:
        lines.append("")
        lines.append("> 打新要点：顶格申购只认**本市场**市值（沪市新股只算沪市持仓，"
                     "深市/基金/债券/现金都不计）；市值按 **T-2 日**定格，"
                     "所以最早 T-2 就要把底仓建好。")
        lines.append("> 底仓唯一职责是占住市值等打新，不求涨、但不能跌。")
    return "\n".join(lines)


if __name__ == "__main__":       # python ipo_calendar.py
    print(json.dumps(sync(), ensure_ascii=False))
    print(json.dumps(upcoming(30)[:8], ensure_ascii=False, indent=1))
