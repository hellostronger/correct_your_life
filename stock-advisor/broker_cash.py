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
- 同一笔重复提交不做自动去重（可能真的有两笔同额转账），靠 id 修改 / 删除。
- 修改走 `update_flow(id, changes)`：保留原 id 与 created_at，只覆盖传入字段，
  改完自动反映到快照报告的净出入金与账户锚点（两者都是实时读侧车算的）。
- 2026-11-20 时间戳升级：`date` 从 `YYYY-MM-DD` 升级为 `YYYY-MM-DD HH:MM:SS`
  （旧记录的时刻原来只存在备注尾部，`migrate_note_times()` 一次性回填）；
  所有按日比较经 `_date10` 截断，净出入金/快照基准日口径逐字节不变。
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
    # 2026-11-20 时间戳升级：date 已带时分秒，全串排序 = 真实时间序
    # （旧键按 _date10+id 会把同日多笔的时刻顺序按 uuid 打乱）。
    return sorted(rows, key=lambda f: (str(f.get("date") or ""), f.get("id", "")), reverse=True)


def store_status(path=None) -> dict:
    p = Path(path) if path else DEFAULT_STORE
    data = load_store(path)
    return {"file": str(p), "exists": p.exists(),
            "count": len(data.get("flows", [])), "error": data.get("error")}


FLOW_FIELDS = ("date", "side", "amount", "currency", "account", "note")

# 2026-11-20 全系统时间戳升级：date 统一存 `YYYY-MM-DD HH:MM:SS`。
_FLOW_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{1,2}))?)?$")


def _norm_flow_ts(date) -> str:
    """归一转账时间：`YYYY-MM-DD [HH:MM[:SS]]` → 带秒全串（缺时刻落 00:00:00）。

    所有「按日」的比较（net_in_range / summarize / 快照基准日）走 `_date10`
    截断，语义与纯日期时代逐字节一致；带时刻后同日多笔能按真实时间排序。
    """
    m = _FLOW_TS_RE.match(str(date or "").strip())
    if not m:
        raise ValueError("日期格式应为 YYYY-MM-DD [HH:MM:SS]")
    if m.group(2) is None:
        return m.group(1) + " 00:00:00"
    hh, mm, sec = int(m.group(2)), m.group(3), m.group(4)
    if not (0 <= hh <= 23):
        raise ValueError("时刻的小时应为 00-23")
    if not (0 <= int(mm) <= 59):
        raise ValueError("时刻的分钟应为 00-59")
    if sec is not None and int(sec) > 59:
        raise ValueError("时刻的秒应为 00-59")
    return f"{m.group(1)} {hh:02d}:{mm}:{'00' if sec is None else sec}"


# 公开别名：交易时间归一（app._norm_trade_ts 场景）复用同一套规则，
# 不在 app.py 里再写一份正则 —— 两份规则必漂移。
norm_ts = _norm_flow_ts


def _norm_flow_fields(date, side, amount, currency, note="", account="") -> dict:
    """校验并归一转账字段。**新增与修改共用这一处校验**，避免两份规则漂移。"""
    date = _norm_flow_ts(date)
    side = str(side).strip().lower()
    if side not in SIDES:
        raise ValueError("方向应为 in（入金）或 out（出金）")
    amount = _f(amount)
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    currency = str(currency).strip().upper()
    if currency not in CURRENCIES:
        raise ValueError("币种只支持 CNY / HKD")
    return {"date": date, "side": side, "amount": _r(amount), "currency": currency,
            "account": str(account or "").strip()[:40],
            "note": str(note or "").strip()[:120]}


def add_flow(date: str, side: str, amount, currency: str,
             note: str = "", account: str = "", path=None) -> dict:
    """记一笔银证转账。校验失败直接 ValueError（调用方转 400）。"""
    norm = _norm_flow_fields(date, side, amount, currency, note, account)
    flow = {"id": uuid.uuid4().hex[:12], **norm,
            "created_at": datetime.now().isoformat(timespec="seconds")}
    with _LOCK:
        data = load_store(path)
        data.setdefault("flows", []).append(flow)
        data["version"] = STORE_VERSION
        _write_store(data, path)
    return flow


def update_flow(flow_id: str, changes: dict, path=None) -> dict | None:
    """改一笔银证转账：只覆盖 `changes` 里出现的字段，其余保持原值。

    id 与 created_at 永不变（快照/对账按 id 认人，改了就等于删旧记新）；
    写入 updated_at 便于追溯。找不到返回 None；校验失败抛 ValueError，
    且**先校验后落盘**——任何非法入参都不会把台账写坏。
    """
    unknown = set(changes or {}) - set(FLOW_FIELDS)
    if unknown:
        raise ValueError("不支持修改的字段: " + ", ".join(sorted(unknown)))
    with _LOCK:
        data = load_store(path)
        rows = data.get("flows", [])
        hit = next((f for f in rows if isinstance(f, dict) and f.get("id") == flow_id), None)
        if hit is None:
            return None
        merged = {k: hit.get(k) for k in FLOW_FIELDS}
        merged.update({k: v for k, v in (changes or {}).items() if k in FLOW_FIELDS})
        norm = _norm_flow_fields(merged["date"], merged["side"], merged["amount"],
                                 merged["currency"], merged["note"], merged["account"])
        hit.update(norm)
        hit["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_store(data, path)
        return dict(hit)


def delete_flow(flow_id: str, path=None) -> bool:
    with _LOCK:
        data = load_store(path)
        before = len(data.get("flows", []))
        data["flows"] = [f for f in data.get("flows", []) if f.get("id") != flow_id]
        if len(data["flows"]) == before:
            return False
        _write_store(data, path)
        return True


# 旧备注惯例把时刻塞在末尾：「截图导入 近1年人民币 银行转证券 14:14:15」
_NOTE_TIME_RE = re.compile(r"(?:^|\s)([0-2]?\d):([0-5]\d)(?::([0-5]\d))?\s*$")


def migrate_note_times(path=None) -> dict:
    """一次性迁移（2026-11-20）：把备注尾部的 HH:MM[:SS] 解析进 date 字段。

    - date 已带时刻（含空格）的行跳过 → **幂等**，可重复执行；
    - 备注原文保留（它记录截图来源），只把 date 升级成 `YYYY-MM-DD HH:MM:SS`；
    - 解析不出合法时刻的行保持纯日期（落 00:00:00 由下次保存时归一），不编造。
    """
    with _LOCK:
        data = load_store(path)
        changed = skipped = kept = 0
        for f in data.get("flows", []):
            if not isinstance(f, dict):
                continue
            d = str(f.get("date") or "").strip()
            if not d:
                continue
            if " " in d:
                skipped += 1
                continue
            m = _NOTE_TIME_RE.search(str(f.get("note") or ""))
            if not m:
                kept += 1
                continue
            hh = int(m.group(1))
            if hh > 23:
                kept += 1
                continue
            f["date"] = f"{d} {hh:02d}:{m.group(2)}:{m.group(3) or '00'}"
            changed += 1
        if changed:
            _write_store(data, path)
        return {"changed": changed, "already_ts": skipped,
                "no_time_in_note": kept, "total": len(data.get("flows", []))}


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
