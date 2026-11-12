"""实盘「快照 + 操作胜率」（只读分析，不改 schema / 不改交易逻辑）。

这个模块回答两个问题：

1. **快照对照**：在某个基准日把当时的持仓与收盘价存成一份快照；之后任意时刻，
   对比「从那天起一动不动」与「实际这一通操作」哪个赚得多。
2. **操作胜率**：以「不动」为对手盘，给每一笔买卖打分 —— 买入笔看
   (现价-买价)×股数，卖出笔看 (卖价-现价)×股数。>0 = 这笔操作比不动好（赢），
   <0 = 不如不动（输），=0 = 平。另给「已实现胜率」（卖出笔按当时摊薄成本，
   与 app.calc_position 同一平均成本法），以及按股票 / 按月份 / 买卖方向的分组。

设计边界（刻意如此，别改）
--------------------------
- **不 import app**：app 启动要 60~80 秒并起全套守护线程；所有数据经参数注入，
  模块只做纯计算 + 侧车文件读写，方便离线测试。
- **不建数据库表**：快照是低频数据，存 `data/trade_snapshots.json`（加锁 + 原子
  replace）。改 schema 按 AGENTS.md §0 需要先问用户，这里绕开。
- **不改交易口径**：只复盘，不下单、不拦单、不写 sa_trades。
- **币种**：港股 5 位代码 = HKD，其余 = CNY；分币种算完再按调用方传入的 fx
  折成人民币合计，**绝不能把 HKD 和 CNY 直接相加**。
- **现价缺失不猜**：缺价的那笔标 win=None、不进胜率分母，并回传 missing_prices。

差额公式（与 10/30 基准价、外部入金、现金利息都无关）::

    实际期末 - 躺平期末 = Σ买 n×(P_now-P_buy) + Σ卖 n×(P_sell-P_now)

所以「多赚多少」只取决于成交价与今日价，快照价格只影响两种情景各自绝对收益。
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
DEFAULT_STORE = Path(__file__).resolve().parent / "data" / "trade_snapshots.json"
_LOCK = threading.Lock()          # 侧车写入必须串行，否则会交错写出半行 JSON


# ---------------- 基础助手 ----------------

def is_hk(code: str) -> bool:
    """与 app._market_of 同口径：5 位数字是港股，其余按 A 股（CNY）处理。"""
    return bool(re.fullmatch(r"\d{5}", str(code)))


def currency_of(code: str) -> str:
    return "HKD" if is_hk(code) else "CNY"


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


def trade_key(t: dict, idx: int) -> str:
    """流水唯一键：优先用数据库 id，没有则用位置补位（测试夹具可不带 id）。"""
    return str(t.get("id") if t.get("id") is not None else f"{_date10(t.get('date') or t.get('trade_date'))}-{idx}")


def normalize_trades(trades: list[dict]) -> list[dict]:
    """把 sa_trades 行 / 测试夹具统一成 {id,date,code,side,shares,price,note}。"""
    out = []
    for i, t in enumerate(trades or []):
        d = _date10(t.get("date") or t.get("trade_date"))
        side = str(t.get("side") or "").lower()
        if not d or not t.get("code") or side not in ("buy", "sell"):
            continue
        out.append({"id": t.get("id", i), "date": d, "code": str(t["code"]),
                    "side": side, "shares": int(_f(t.get("shares"))),
                    "price": _f(t.get("price")),
                    "note": str(t.get("note") or "")})
    # 次级键统一成 (int, int, str)，避免夹具里 int/str id 混用触发 TypeError
    out.sort(key=lambda x: (x["date"], 0, int(x["id"]), "") if str(x["id"]).isdigit()
             else (x["date"], 1, 0, str(x["id"])))
    return out


# ---------------- 侧车存取 ----------------

def load_store(path=None) -> dict:
    """读侧车；文件不存在/损坏时返回空库（附 error，绝不静默当成功）。"""
    p = Path(path) if path else DEFAULT_STORE
    empty = {"version": STORE_VERSION, "snapshots": []}
    if not p.exists():
        return empty
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
            return {**empty, "error": "快照文件格式不对"}
        data.setdefault("version", STORE_VERSION)
        return data
    except (OSError, ValueError) as exc:
        return {**empty, "error": f"快照文件读取失败: {exc}"}


def _write_store(data: dict, path=None) -> None:
    p = Path(path) if path else DEFAULT_STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)          # 原子替换：写一半崩了也不会毁掉旧快照


def list_snapshots(path=None) -> list[dict]:
    snaps = [s for s in load_store(path).get("snapshots", []) if isinstance(s, dict)]
    return sorted(snaps, key=lambda s: s.get("date", ""), reverse=True)


def store_status(path=None) -> dict:
    """给 API/UI 报侧车健康度：读坏了要看得见，不能静默当成「还没有快照」。"""
    p = Path(path) if path else DEFAULT_STORE
    data = load_store(path)
    return {"file": str(p), "exists": p.exists(), "count": len(data.get("snapshots", [])),
            "error": data.get("error")}


def save_snapshot(snap: dict, path=None) -> dict:
    """按 date 幂等 upsert（同一天重复打 = 覆盖，不会堆出一堆同日快照）。"""
    with _LOCK:
        data = load_store(path)
        snaps = [s for s in data.get("snapshots", []) if s.get("date") != snap["date"]]
        snaps.append(snap)
        data["snapshots"] = sorted(snaps, key=lambda s: s.get("date", ""))
        data["version"] = STORE_VERSION
        _write_store(data, path)
    return snap


def delete_snapshot(date: str, path=None) -> bool:
    with _LOCK:
        data = load_store(path)
        before = len(data.get("snapshots", []))
        data["snapshots"] = [s for s in data.get("snapshots", []) if s.get("date") != date]
        if len(data["snapshots"]) == before:
            return False
        _write_store(data, path)
        return True


# ---------------- 快照构建 ----------------

def parse_close_on(klines, date: str):
    """从 /kline 的 ``"YYYY-MM-DD,close"`` 列表里取 <= date 的最后一根。

    返回 **(price, price_date)** —— 调用方别按「日期在前」解包（101 返回的
    是「日期,收盘价」，2026-11-11 真机补打历史快照时在这里把顺序搞反过，
    结果 31 只票的基准价全空）。周末/节假日自然落到前一交易日。
    """
    rows = []
    for k in klines or []:
        parts = str(k).split(",")
        if len(parts) < 2:
            continue
        try:
            p = float(parts[1])
        except ValueError:
            continue
        if parts[0][:10] <= date and p > 0:
            rows.append((parts[0][:10], p))
    rows.sort(key=lambda x: x[0])
    return (rows[-1][1], rows[-1][0]) if rows else (None, None)


def replay_positions(trades: list[dict], as_of: str) -> dict[str, int]:
    """回放 as_of（含当日）之前的流水，得到基准日收盘持股。"""
    pos: dict[str, int] = {}
    for t in normalize_trades(trades):
        if t["date"] > as_of:
            break
        pos[t["code"]] = pos.get(t["code"], 0) + t["shares"] * (1 if t["side"] == "buy" else -1)
    return {c: n for c, n in pos.items() if n != 0}


def build_snapshot(date: str, positions: dict[str, int], prices: dict[str, float],
                   fx: float, names: dict | None = None, label: str = "",
                   price_dates: dict | None = None, path=None) -> dict:
    """存一份基准快照。prices 必须是基准日（或其前一交易日）的收盘价，由调用方取。

    缺价的代码 **不会** 被静默丢弃：仍记进 positions（price=None），报告里会
    在 missing_prices 里点名，避免「少算一只票还以为数据齐了」。
    """
    names = names or {}
    price_dates = price_dates or {}
    rows, base_cny = [], 0.0
    for code, shares in sorted(positions.items()):
        px = prices.get(code)
        px = _f(px) if px not in (None, "") and _f(px) > 0 else None
        k = fx if is_hk(code) else 1.0
        val = (px or 0.0) * shares
        base_cny += val * k
        rows.append({"code": code, "name": names.get(code, code), "shares": int(shares),
                     "price": px, "price_date": price_dates.get(code, date),
                     "currency": currency_of(code),
                     "value_cny": _r(val * k)})
    snap = {"date": date, "label": label or date, "created_at": datetime.now().isoformat(timespec="seconds"),
            "fx": _r(fx, 4), "baseline_value_cny": _r(base_cny), "positions": rows,
            "missing_prices": [r["code"] for r in rows if r["price"] is None]}
    save_snapshot(snap, path)
    return snap


# ---------------- 打分：每笔操作 vs 不动 ----------------

def sell_realized_map(trades: list[dict]) -> dict[str, float]:
    """每笔卖出的已实现盈亏 = (卖价 - 卖出时点摊薄成本)×股数（平均成本法）。

    与 app.calc_position 同口径；历史流水若出现超卖（删账导致）不抛异常，
    按当时实际持股封顶并继续回放。
    """
    state: dict[str, dict] = {}
    out: dict[str, float] = {}
    for i, t in enumerate(normalize_trades(trades)):
        st = state.setdefault(t["code"], {"net": 0, "avg": 0.0})
        n, p = t["shares"], t["price"]
        if t["side"] == "buy":
            if n > 0:
                st["avg"] = (st["avg"] * st["net"] + p * n) / (st["net"] + n)
                st["net"] += n
        else:
            eff = min(n, st["net"])
            out[trade_key(t, i)] = (p - st["avg"]) * eff
            st["net"] -= eff
    return out


def _rate(wins: int, losses: int) -> float | None:
    total = wins + losses
    return round(wins * 100.0 / total, 2) if total else None


def _score_bucket(items: list[dict]) -> dict:
    wins = sum(1 for x in items if x["win"] is True)
    losses = sum(1 for x in items if x["win"] is False)
    flat = sum(1 for x in items if x["win"] is None)
    return {"count": len(items), "wins": wins, "losses": losses, "flat": flat,
            "win_rate": _rate(wins, losses),
            "contribution_cny": _r(sum(x["contribution_cny"] for x in items))}


def score_trades(trades: list[dict], prices: dict[str, float], fx: float,
                 names: dict | None = None, since: str | None = None,
                 all_trades: list[dict] | None = None) -> dict:
    """给每笔交易打「相对不动」的分。since 非空时只算该日之后（不含当日）。

    - buy : (现价 - 买价) × 股数   —— 买完涨了 = 比不买强
    - sell: (卖价 - 现价) × 股数   —— 卖完跌了 = 比不卖强
    所有金额先按原币算，再折成人民币；胜率分母不含缺价/平手的笔。
    """
    names = names or {}
    rows = normalize_trades(trades)
    realized = sell_realized_map(all_trades if all_trades is not None else rows)
    missing = set()
    scored: list[dict] = []
    for i, t in enumerate(rows):
        if since and t["date"] <= since:
            continue
        px = prices.get(t["code"])
        px = _f(px) if px not in (None, "") and _f(px) > 0 else None
        k = fx if is_hk(t["code"]) else 1.0
        if px is None:
            contrib_local = None
            missing.add(t["code"])
        else:
            contrib_local = t["shares"] * ((px - t["price"]) if t["side"] == "buy"
                                           else (t["price"] - px))
        cny = None if contrib_local is None else _r(contrib_local * k)
        win = None if cny is None or abs(cny) < 0.005 else cny > 0
        rl = realized.get(trade_key(t, i)) if t["side"] == "sell" else None
        scored.append({
            "key": trade_key(t, i), "date": t["date"], "code": t["code"],
            "name": names.get(t["code"], t["code"]), "side": t["side"],
            "shares": t["shares"], "price": _r(t["price"], 4), "price_now": px,
            "currency": currency_of(t["code"]), "fx": _r(fx, 4),
            "contribution_local": _r(contrib_local) if contrib_local is not None else None,
            "contribution_cny": cny if cny is not None else 0.0,
            "priced": px is not None, "win": win,
            "realized_local": _r(rl) if rl is not None else None,
            "realized_cny": _r(rl * k) if rl is not None else None,
            "note": t["note"],
        })

    priced = [x for x in scored if x["priced"]]
    buys = [x for x in priced if x["side"] == "buy"]
    sells = [x for x in priced if x["side"] == "sell"]
    realized_rows = [x for x in priced if x["side"] == "sell" and x["realized_cny"] is not None]
    r_wins = sum(1 for x in realized_rows if x["realized_cny"] > 0.005)
    r_losses = sum(1 for x in realized_rows if x["realized_cny"] < -0.005)

    by_code: dict[str, list] = {}
    by_month: dict[str, list] = {}
    for x in priced:
        by_code.setdefault(x["code"], []).append(x)
        by_month.setdefault(x["date"][:7], []).append(x)
    code_rows = [{"code": c, "name": names.get(c, c), **_score_bucket(v)}
                 for c, v in by_code.items()]
    month_rows = [{"month": m, **_score_bucket(v)} for m, v in sorted(by_month.items())]
    code_rows.sort(key=lambda r: -r["contribution_cny"])
    month_rows.sort(key=lambda r: r["month"])

    return {
        "since": since, "fx": _r(fx, 4), "trades": scored,
        "top_winners": sorted((x for x in priced if x["win"] is True),
                              key=lambda x: -x["contribution_cny"])[:5],
        "top_losers": sorted((x for x in priced if x["win"] is False),
                             key=lambda x: x["contribution_cny"])[:5],
        "win_rate": {"total": len(scored), "priced": len(priced),
                     **_score_bucket(priced),
                     "buy": _score_bucket(buys), "sell": _score_bucket(sells)},
        "realized": {"count": len(realized_rows), "wins": r_wins, "losses": r_losses,
                     "win_rate": _rate(r_wins, r_losses),
                     "pnl_cny": _r(sum(x["realized_cny"] for x in realized_rows))},
        "by_code": code_rows, "by_month": month_rows,
        "missing_prices": sorted(missing),
    }


# ---------------- 快照报告：躺平 vs 实际 ----------------

def build_report(snapshot: dict, trades_all: list[dict], prices: dict[str, float],
                 fx: float, names: dict | None = None) -> dict:
    """对照某个快照：不动 vs 实际，外加这一区间的操作胜率。"""
    names = names or {}
    date = snapshot["date"]
    base = {r["code"]: r for r in snapshot.get("positions", [])}
    rows = normalize_trades(trades_all)
    after = [t for t in rows if t["date"] > date]
    codes = sorted(set(base) | {t["code"] for t in after})
    missing = list(snapshot.get("missing_prices") or [])

    per_code, buckets = [], {"CNY": {}, "HKD": {}}
    for c in codes:
        b = base.get(c)
        s0 = int(b["shares"]) if b else 0
        p0 = b.get("price") if b else None
        p0 = _f(p0) if p0 not in (None, "") and _f(p0) > 0 else None
        px = prices.get(c)
        px = _f(px) if px not in (None, "") and _f(px) > 0 else None
        if px is None:
            missing.append(c)
        ts = [t for t in after if t["code"] == c]
        sn = s0 + sum(t["shares"] * (1 if t["side"] == "buy" else -1) for t in ts)
        cf = sum(t["shares"] * t["price"] * (1 if t["side"] == "sell" else -1) for t in ts)
        k = fx if is_hk(c) else 1.0
        ccy = currency_of(c)
        # 缺基准价/现价时用可得的那个代替，并标记 incomplete，绝不编 0 价
        v0 = (s0 * (p0 or 0.0))
        flat_end = (s0 * (px or 0.0)) if px is not None else 0.0
        real_end = (sn * (px or 0.0) + cf) if px is not None else 0.0
        item = {"code": c, "name": names.get(c, (b or {}).get("name", c)),
                "currency": ccy, "shares_base": s0, "shares_now": sn,
                "price_base": p0, "price_now": px,
                "baseline_cny": _r(v0 * k), "flat_end_cny": _r(flat_end * k),
                "real_end_cny": _r(real_end * k),
                "flat_gain_cny": _r((flat_end - v0) * k),
                "real_gain_cny": _r((real_end - v0) * k),
                "delta_cny": _r((real_end - flat_end) * k),
                # 基准日没持仓（区间内新建仓）本来就不需要基准价，不算数据不全
                "complete": (p0 is not None or s0 == 0) and px is not None}
        per_code.append(item)
        for key, val in (("baseline", v0), ("flat_end", flat_end), ("real_end", real_end)):
            buckets[ccy][key] = buckets[ccy].get(key, 0.0) + val

    def cny_total(key: str) -> float:
        return sum(buckets[ccy].get(key, 0.0) * (fx if ccy == "HKD" else 1.0)
                   for ccy in ("CNY", "HKD"))

    v0, flat, real = cny_total("baseline"), cny_total("flat_end"), cny_total("real_end")
    totals = {"baseline_cny": _r(v0), "flat_end_cny": _r(flat), "real_end_cny": _r(real),
              "flat_gain_cny": _r(flat - v0), "real_gain_cny": _r(real - v0),
              "flat_pct": _r((flat / v0 - 1) * 100) if v0 else None,
              "real_pct": _r((real / v0 - 1) * 100) if v0 else None,
              "delta_cny": _r(real - flat),
              "delta_pct_point": _r((real - flat) / v0 * 100) if v0 else None}
    by_ccy = {}
    for ccy, b in buckets.items():
        by_ccy[ccy] = {"baseline": _r(b.get("baseline", 0.0)),
                       "flat_end": _r(b.get("flat_end", 0.0)),
                       "real_end": _r(b.get("real_end", 0.0)),
                       "delta": _r(b.get("real_end", 0.0) - b.get("flat_end", 0.0))}

    score = score_trades(after, prices, fx, names, since=None, all_trades=rows)
    per_code.sort(key=lambda r: -abs(r["delta_cny"]))
    return {
        "snapshot": {k: snapshot.get(k) for k in
                     ("date", "label", "created_at", "fx", "baseline_value_cny", "missing_prices")},
        "fx": _r(fx, 4), "totals": totals, "by_currency": by_ccy,
        "by_code": per_code, "win_rate": score["win_rate"], "realized": score["realized"],
        "by_month": score["by_month"], "trade_count": len(after),
        "incomplete_count": sum(1 for r in per_code if not r["complete"]),
        "missing_prices": sorted(set(missing)),
        "winner_code": score["by_code"][0] if score["by_code"] else None,
        "loser_code": score["by_code"][-1] if score["by_code"] else None,
        "caveats": [
            "现价为最新收盘/实时快照，报告随行情变动；快照价格只在建快照时取一次。",
            "统一按当前 HKD/CNY 汇率折算两端，不含汇兑损益。",
            "未计手续费、印花税、分红；卖出所得现金按零收益持有。",
            "择时胜率以「不动」为对手盘，不等于券商 App 的已实现盈亏胜率（另列）。",
        ],
    }
