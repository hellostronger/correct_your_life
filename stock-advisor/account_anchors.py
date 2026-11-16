"""账户级真实锚点：真实现金 + 总参考收益（累计总盈亏金额，折 CNY）。

为什么需要
----------
这个账号在 2026-09-09 才开始进 `sa_trades`，之前的本金、盈亏、出入金都没录入系统。
所以系统里由流水推算出来的两个量都不等于账户真实值：

- 持仓页「累计总盈亏」= Σ 各票 `total_pnl_cny`，只覆盖已录入的流水；
- 快照报告「推演组合价值」= 持仓市值 + **按流水推算的虚拟现金**（Σ卖−Σ买），
  不是券商账户总资产（2026-11-13 实测相差 7.87 万）。

用户手填两个锚点后，后续所有复盘以它俩为准：

    账户真实总资产 = 当前持仓市值 + 真实现金
    系统未覆盖收益缺口 = 总参考收益 − 系统已录入累计总盈亏
    反推总投入本金 = 账户真实总资产 − 总参考收益

设计边界
--------
- 不建表（AGENTS.md §0），侧车 `data/account_anchors.json`，模块级锁 + tmp 原子写。
- 不 import app，纯标准库；金额一律折人民币（CNY）。
- **未录入 = None**，绝不默认成 0 冒充真实数据；`compute` 对未录入项返回 None。
- 只存当前值（单条记录），不是流水；历史出入金在 `broker_cash` 侧车里。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime
from pathlib import Path

STORE_VERSION = 1
DEFAULT_STORE = Path(__file__).resolve().parent / "data" / "account_anchors.json"
_LOCK = threading.Lock()


def _f(v, default=0.0):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _r(v):
    return round(_f(v), 2)


def empty() -> dict:
    return {"version": STORE_VERSION, "cash_cny": None,
            "reference_pnl_cny": None, "note": "", "updated_at": None,
            # 2026-11-14 加：与实盘买卖联动（用户要求「后续实盘买入卖出都要与
            # 真实现金、账号实际总收益互动」）。
            # asof_date = 锚点数值的基准日（默认录入当天）；**此日之后的** sa_trades
            # 净现金流与 broker_cash 出入金会自动叠加到锚点上，现金与收益随每笔
            # 买卖实时联动，不用重复手填。此日当天及之前的交易视为已含在手填值里。
            "asof_date": None,
            # system_pnl_snapshot_cny = 录入锚点那一刻「系统已录入累计总盈亏」。
            # 有了它，未覆盖缺口被钉死在录入时点（账号 9-9 前的历史，不随行情漂移），
            # 而实时参考收益 = 参考收益 + (系统盈亏现在值 − 快照)，随行情与交易滚动。
            "system_pnl_snapshot_cny": None}


def load_store(path=None) -> dict:
    p = Path(path) if path else DEFAULT_STORE
    if not p.exists():
        return empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {**empty(), "error": "账户锚点格式不对"}
        out = empty()
        for k in ("cash_cny", "reference_pnl_cny"):
            v = data.get(k)
            out[k] = _f(v) if v is not None else None
        # 快照严格解析：垃圾值兜底成 None（未快照），绝不拿 0.0 冒充录入时点
        v = data.get("system_pnl_snapshot_cny")
        try:
            out["system_pnl_snapshot_cny"] = (
                round(float(v), 2) if v is not None and math.isfinite(float(v)) else None)
        except (TypeError, ValueError):
            out["system_pnl_snapshot_cny"] = None
        out["note"] = str(data.get("note") or "")
        out["updated_at"] = data.get("updated_at")
        d = str(data.get("asof_date") or "")
        out["asof_date"] = d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None
        return out
    except (OSError, ValueError) as exc:
        return {**empty(), "error": f"账户锚点读取失败: {exc}"}


def _write(data: dict, path=None) -> None:
    p = Path(path) if path else DEFAULT_STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def save(cash_cny, reference_pnl_cny, note: str = "", asof_date: str | None = None,
         system_pnl_snapshot_cny: float | None = None, path=None) -> dict:
    """保存两个锚点。传入 None 表示「未录入」；现金不能为负。

    asof_date            锚点数值的基准日（YYYY-MM-DD，默认今天）；
                         之后的交易/出入金由 compute 动态叠加，不改动这里存的值。
    system_pnl_snapshot_cny  录入时系统已录入的累计总盈亏，用于钉死未覆盖缺口
                         并滚动计算实时参考收益（None=未快照，compute 走兼容口径）。
    """
    def norm(v):
        if v is None or v == "":
            return None
        f = _f(v, float("nan"))
        if not math.isfinite(f):
            raise ValueError("金额必须是数字")
        return round(f, 2)

    cash = norm(cash_cny)
    ref = norm(reference_pnl_cny)
    if cash is not None and cash < 0:
        raise ValueError("现金不能为负")
    d = str(asof_date or "").strip()
    if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError("基准日期格式应为 YYYY-MM-DD")
    snap = norm(system_pnl_snapshot_cny)
    data = empty()
    data.update({"cash_cny": cash, "reference_pnl_cny": ref,
                 "note": str(note or "").strip()[:120],
                 "asof_date": d or None,
                 "system_pnl_snapshot_cny": snap if (cash is not None or ref is not None) else None,
                 "updated_at": datetime.now().isoformat(timespec="seconds")})
    with _LOCK:
        _write(data, path)
    return data


def status(path=None) -> dict:
    p = Path(path) if path else DEFAULT_STORE
    d = load_store(path)
    return {"file": str(p), "exists": p.exists(),
            "set": d.get("cash_cny") is not None or d.get("reference_pnl_cny") is not None,
            "error": d.get("error")}


def compute(anchors: dict, holdings_mv_cny: float, system_pnl_cny: float,
            net_trade_flow_cny: float = 0.0, ext_flow_cny: float = 0.0) -> dict:
    """把锚点与系统推算值对齐，算出真实总资产与未覆盖缺口（2026-11-14 起与流水联动）。

    holdings_mv_cny    当前持仓市值（折 CNY，来自 /api/holdings）
    system_pnl_cny     系统已录入的累计总盈亏（Σ total_pnl_cny）现在值
    net_trade_flow_cny 基准日(asof_date)之后 sa_trades 的净现金流（Σ卖−Σ买，折 CNY）
    ext_flow_cny       基准日之后 broker_cash 的净出入金（入−出，折 CNY）

    联动口径
    --------
    真实现金(现在) = 锚点现金 + 净交易现金流 + 净出入金
      —— 每记一笔买入自动扣现金、卖出自动加现金，不需要重录锚点；
    实时参考收益 = 锚点参考收益 + (系统盈亏现在值 − 录入时快照)
      —— 录入后系统新记的每一笔买卖、以及行情涨跌，都实时滚进账号真实收益；
    未覆盖缺口 = 锚点参考收益 − 录入时系统盈亏快照（钉死在录入时点，
      代表 9-9 之前没进系统的历史盈亏，不随行情漂移；旧数据无快照时退化为
      参考收益 − 当前系统盈亏，与历史行为一致）。
    """
    a = anchors or {}
    cash = a.get("cash_cny")
    ref = a.get("reference_pnl_cny")
    snap = a.get("system_pnl_snapshot_cny")
    mv, sysp = _f(holdings_mv_cny), _f(system_pnl_cny)
    flow, ext = _f(net_trade_flow_cny), _f(ext_flow_cny)
    # 未覆盖缺口：优先用录入时快照钉死；旧侧车没有快照则沿用旧口径（当前系统值）
    gap_base = snap if snap is not None else sysp
    gap = _r(ref - gap_base) if ref is not None else None
    # 实时参考收益：无快照（旧数据）时无法滚动，退化为手填值本身
    real_pnl = _r(ref + (sysp - snap)) if (ref is not None and snap is not None) else ref
    # 现金联动：锚点 + 基准日后的交易净现金流 + 出入金
    cash_now = _r(cash + flow + ext) if cash is not None else None
    real_assets = _r(mv + cash_now) if cash_now is not None else None
    principal = _r(real_assets - real_pnl) if (real_assets is not None and real_pnl is not None) else None
    return {
        "holdings_mv_cny": _r(mv),
        "system_total_pnl_cny": _r(sysp),
        "cash_cny": cash,
        "reference_pnl_cny": ref,
        "asof_date": a.get("asof_date"),
        "system_pnl_snapshot_cny": snap,
        "net_trade_flow_cny": _r(flow),
        "ext_flow_cny": _r(ext),
        "cash_now_cny": cash_now,
        "real_pnl_now_cny": real_pnl,
        "real_assets_cny": real_assets,
        "pnl_gap_cny": gap,
        "implied_principal_cny": principal,
        "note": a.get("note") or "",
        "updated_at": a.get("updated_at"),
        "ready": cash is not None and ref is not None,
    }
