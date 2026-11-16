"""银证转账（银行 ↔ 券商）入金/出金台账。只读记账，不改 schema、不碰交易逻辑。

为什么需要它
------------
`trade_snapshot.build_report` 的「推演组合价值」= 当前持仓市值 + 按 sa_trades
流水推算的净现金（Σ卖 − Σ买）。这个推算默认「卖出的钱一直躺在券商账户、
期间没有出入金、A股/港股资金可自由互转」，所以常常**不等于券商账户总资产**
（2026-11-13 用户实测：报告 70.6w、账户只有 63w，差 7.87w）。
把真实的银行→券商 / 券商→银行资金流记下来，就能把那笔虚拟现金修正回真实值：

    账户应有资产 = 推演组合价值 + 期间净出入金（入金为正、出金为负）

差额（操作 vs 不动）与胜率只依赖成交价和现价，**不因出入金而改变** —— 两种情景
都经历同一笔外部转账，所以 `build_report` 会把净额同时加到躺平与实际两边。

设计边界
--------
- 低频数据，走侧车 `data/broker_cash_flows.json`（模块级锁 + tmp 原子 replace），
  不建表（AGENTS.md §0：改 schema 要先问用户）。
- 不 import app，纯标准库；金额原币保存，折人民币由调用方传 fx。
- 只记**外部**资金流（银行↔券商）；账户内部买股票/卖股票不记（那是 sa_trades）。
- 同一笔重复提交不做自动去重（可能真的有两笔同额转账），靠 id 删除。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

STORE_VERSION = 1
DEFAULT_STORE = Path(__file__).resolve().parent / "data" / "broker_cash_flows.json"
_LOCK = threading.Lock()
CURRENCIES = ("CNY", "HKD")
SIDES = ("in", "out")


def _f(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _r(v, n=2) -> float:
    return round(_f(v), n)


def _date10(v) -> str:
    return str(v or "")[:10]


def is_hk(currency: str) -> bool:
    return str(currency).upper() == "HKD"


def load_store(path=None) -> dict:
    """读侧车；不存在/损坏都返回空库，损坏时带 error（不静默当成功）。"""
    p = Path(path) if path else DEFAULT_STORE
    empty = {"version": STORE_VERSION, "flows": []}
    if not p.exists():
        return empty
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("flows"), list):
            return {**empty, "error": "出入金台账格式不对"}
        data.setdefault("version", STORE_VERSION)
        return data
    except (OSError, ValueError) as exc:
        return {**empty, "error": f"出入金台账读取失败: {exc}"}


def _write_store(data: dict, path=None) -> None:
    p = Path(path) if path else DEFAULT_STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def list_flows(path=None, start: str | None = None, end: str | None = None) -> list[dict]:
    rows = [f for f in load_store(path).get("flows", []) if isinstance(f, dict)]
    if start:
        # 列表筛选是闭区间（>= start）；只有 net_in_range 的 start 是严格大于，
        # 因为它对应「快照日之后」，基准日当天的转账属于基准日现金。
        rows = [f for f in rows if _date10(f.get("date")) >= start]
    if end:
        rows = [f for f in rows if _date10(f.get("date")) <= end]
    return sorted(rows, key=lambda f: (_date10(f.get("date")), f.get("id", "")), reverse=True)


def store_status(path=None) -> dict:
    p = Path(path) if path else DEFAULT_STORE
    data = load_store(path)
    return {"file": str(p), "exists": p.exists(),
            "count": len(data.get("flows", [])), "error": data.get("error")}


def add_flow(date: str, side: str, amount, currency: str,
             note: str = "", account: str = "", path=None) -> dict:
    """记一笔银证转账。校验失败直接 ValueError（调用方转 400）。"""
    date = _date10(date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    side = str(side).strip().lower()
    if side not in SIDES:
        raise ValueError("方向应为 in（入金）或 out（出金）")
    amount = _f(amount)
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    currency = str(currency).strip().upper()
    if currency not in CURRENCIES:
        raise ValueError("币种只支持 CNY / HKD")
    flow = {"id": uuid.uuid4().hex[:12], "date": date, "side": side,
            "amount": _r(amount), "currency": currency,
            "account": str(account or "").strip()[:40],
            "note": str(note or "").strip()[:120],
            "created_at": datetime.now().isoformat(timespec="seconds")}
    with _LOCK:
        data = load_store(path)
        data.setdefault("flows", []).append(flow)
        data["version"] = STORE_VERSION
        _write_store(data, path)
    return flow


def delete_flow(flow_id: str, path=None) -> bool:
    with _LOCK:
        data = load_store(path)
        before = len(data.get("flows", []))
        data["flows"] = [f for f in data.get("flows", []) if f.get("id") != flow_id]
        if len(data["flows"]) == before:
            return False
        _write_store(data, path)
        return True


def signed_local(flow: dict) -> float:
    """入金为正、出金为负（原币）。"""
    return _f(flow.get("amount")) * (1 if flow.get("side") == "in" else -1)


def net_in_range(flows: list[dict], start: str | None = None,
                 end: str | None = None, fx: float = 1.0) -> dict:
    """区间内净出入金。start 为**严格大于**（与快照报告的 date > 基准日一致）。"""
    rows = [f for f in (flows or [])
            if (not start or _date10(f.get("date")) > start)
            and (not end or _date10(f.get("date")) <= end)]
    by_ccy = {c: {"in": 0.0, "out": 0.0, "net": 0.0, "count": 0} for c in CURRENCIES}
    net_cny = 0.0
    for f in rows:
        c = str(f.get("currency", "CNY")).upper()
        if c not in by_ccy:
            continue
        a = _f(f.get("amount"))
        if f.get("side") == "in":
            by_ccy[c]["in"] += a
        else:
            by_ccy[c]["out"] += a
        by_ccy[c]["net"] += signed_local(f)
        by_ccy[c]["count"] += 1
        net_cny += signed_local(f) * (fx if c == "HKD" else 1.0)
    for c in by_ccy:
        for k in ("in", "out", "net"):
            by_ccy[c][k] = _r(by_ccy[c][k])
    return {"count": len(rows), "by_currency": by_ccy, "net_cny": _r(net_cny)}


def summarize(flows: list[dict], start: str | None = None,
              end: str | None = None, fx: float = 1.0) -> dict:
    """台账汇总：累计入/出金、净额（分币种 + 折 CNY）、按月明细。"""
    rows = [f for f in (flows or [])
            if (not start or _date10(f.get("date")) >= start)
            and (not end or _date10(f.get("date")) <= end)]
    base = net_in_range(rows, None, None, fx)
    months: dict[str, dict] = {}
    for f in rows:
        m = _date10(f.get("date"))[:7]
        d = months.setdefault(m, {"month": m, "in_cny": 0.0, "out_cny": 0.0,
                                  "net_cny": 0.0, "count": 0})
        c = str(f.get("currency", "CNY")).upper()
        v = _f(f.get("amount")) * (fx if c == "HKD" else 1.0)
        if f.get("side") == "in":
            d["in_cny"] += v
        else:
            d["out_cny"] += v
        d["net_cny"] += signed_local(f) * (fx if c == "HKD" else 1.0)
        d["count"] += 1
    out = []
    for m in sorted(months):
        d = months[m]
        out.append({"month": m, "count": d["count"],
                    "in_cny": _r(d["in_cny"]), "out_cny": _r(d["out_cny"]),
                    "net_cny": _r(d["net_cny"])})
    return {"count": len(rows), "total_in_cny": _r(
                sum(_f(f.get("amount")) * (fx if str(f.get("currency", "CNY")).upper() == "HKD" else 1.0)
                    for f in rows if f.get("side") == "in")),
            "total_out_cny": _r(
                sum(_f(f.get("amount")) * (fx if str(f.get("currency", "CNY")).upper() == "HKD" else 1.0)
                    for f in rows if f.get("side") == "out")),
            "net_cny": base["net_cny"], "by_currency": base["by_currency"],
            "by_month": out}
