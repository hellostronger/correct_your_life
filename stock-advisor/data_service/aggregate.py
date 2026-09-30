"""聚合层：把多源板块数据合并成一份，并做跨厂商交叉校验 + 轮动评分。

跨厂商校验为什么重要
--------------------
同花顺、开盘红、新浪是三套完全独立的系统。若它们对同一板块的涨跌幅一致，
说明数据可信；若打架，说明某一侧异常或当日行情已变 —— 这在单一数据源时
是发现不了的（也正是原实现静默停更 9 天却无人知晓的根因）。

轮动评分公式沿用原 sector.py，保证改造前后可比：
    score = 涨幅分(0-40) + 资金分(0-30) + 涨停分(0-20) + 动量分(0-10)
"""
from __future__ import annotations

import collections
from datetime import date, datetime, timedelta

from . import normalize as N
from .sources import KPH, METER, SINA, THS, ZT, SourceError


def _pct_rank(values: list[float], v: float | None) -> float:
    """v 在 values 中的百分位（0-1）。原 sector.py._pct_rank 同款。"""
    if v is None or not values:
        return 0.0
    below = sum(1 for x in values if x < v)
    return below / len(values)


def _momentum(history: list[dict]) -> float | None:
    """N 日累计涨幅，**真复利**（末/初 - 1），返回百分数。

    原 sector.py 用各段收益率简单相加（注释写了"简单求和近似复利"）。
    连续 3 天各 +10% 时简单和给 30%，真实是 33.1% —— 板块级别差 3 个百分点
    足以改变排名，所以这里改成复利。
    """
    closes = [h["close"] for h in history
              if h.get("close") is not None and h.get("date")]
    if len(closes) < 4:
        return None
    win = closes[-4:]                      # 4 个点 = 3 段
    if not win[0]:
        return None
    return round((win[-1] / win[0] - 1) * 100, 3)


class Snapshot:
    """一次采集的结果。"""

    def __init__(self) -> None:
        self.collected_at = datetime.now().isoformat(timespec="seconds")
        self.trade_date = date.today().isoformat()
        self.boards: list[dict] = []
        self.zt: dict = {}
        self.sources_used: list[str] = []
        self.sources_failed: list[dict] = []
        self.degraded: bool = False
        self.notes: list[str] = []
        self.cross_check: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "collected_at": self.collected_at,
            "trade_date": self.trade_date,
            "board_count": len(self.boards),
            "boards": self.boards,
            "zt": self.zt,
            "sources_used": self.sources_used,
            "sources_failed": self.sources_failed,
            "degraded": self.degraded,
            "notes": self.notes,
            "meter": METER.snapshot(),
        }
        if self.cross_check:
            out["cross_check"] = self.cross_check
        return out


def collect(include_aux: bool = True, include_concept: bool = True,
            include_zt: bool = True, include_sina: bool = True) -> Snapshot:
    """采集一轮。任一源失败都**不抛异常**，而是降级并记录 —— 绝不静默。"""
    snap = Snapshot()

    # ---- 主源：同花顺 ----
    ths_rows: list[dict] = []
    try:
        ths_rows = THS.fetch(include_concept=include_concept)
        snap.sources_used.append("ths")
    except SourceError as exc:
        snap.sources_failed.append({"source": "ths", "error": str(exc)})
        snap.degraded = True
        snap.notes.append(f"同花顺源不可用：{exc}")

    # ---- 辅助源：开盘红 / 新浪 ----
    aux_rows: list[dict] = []
    if include_aux:
        targets = [("kph", KPH.fetch)]
        if include_sina:
            targets.append(("sina", SINA.fetch))
        for src, fn in targets:
            try:
                aux_rows.extend(fn())
                snap.sources_used.append(src)
            except SourceError as exc:
                snap.sources_failed.append({"source": src, "error": str(exc)})
                snap.degraded = True
                snap.notes.append(f"{src} 源不可用：{exc}")

    # ---- 涨停池 ----
    if include_zt:
        try:
            snap.zt = ZT.fetch()
            snap.sources_used.append("ztpool")
        except SourceError as exc:
            snap.sources_failed.append({"source": "ztpool", "error": str(exc)})
            snap.degraded = True
            snap.notes.append(f"涨停池不可用：{exc}")

    snap.boards = _merge(ths_rows, aux_rows, snap)
    return snap


def _merge(ths_rows: list[dict], aux_rows: list[dict],
           snap: Snapshot) -> list[dict]:
    """合并主源(同花顺)与辅助源(开盘红/新浪)。

    优先级：同花顺 > 开盘红 > 新浪
      - 同花顺：口径最全、涨跌幅精度最高
      - 开盘红：多 turnover_rate，另有 42 地域 / 149 题材是同花顺没有的
      - 新浪：申万口径，多 154 个证监会行业；涨跌幅只有 2 位小数，
        精度 ±0.5pp，只用来补空字段，**永不覆盖主源**
    """
    out: list[dict] = list(ths_rows)
    idx = {r["name"]: r for r in out}

    filled = 0
    added = 0
    for r in aux_rows:
        base = idx.get(r["name"])
        if base is None:
            idx[r["name"]] = r
            out.append(r)
            added += 1
            continue
        for f in ("turnover", "turnover_rate", "main_inflow", "pct",
                  "lead_stock", "lead_stock_pct"):
            if base.get(f) in (None, "") and r.get(f) not in (None, ""):
                if f == "pct" and r.get("pct_low_precision"):
                    continue        # 精度太低，不进主源字段
                base[f] = r[f]
                base.setdefault("filled_by", []).append(f"{r['source']}:{f}")
                filled += 1

    if filled:
        snap.notes.append(f"辅助源补齐 {filled} 个空字段")
    if added:
        snap.notes.append(f"辅助源新增 {added} 个主源没有的板块")

    # ---- 跨厂商交叉校验：只看涨跌幅 ----
    # 不用资金流方向做阈值，实测原因：
    #   涨跌幅 同花顺 vs 开盘红 中位差 0.029pp / 最大 0.668pp -> 高度一致
    #   资金流 同号率仅 65.6%，因为同花顺「净流入」是主力大单口径、
    #         开盘红 buy+sell 是全主动买卖 —— 定义差异不是数据错误
    by_vendor: dict[str, dict] = {}
    for r in ths_rows + aux_rows:
        by_vendor.setdefault(r["source"], {})[r["name"]] = r
    checks = []
    vs = sorted(by_vendor)
    for i, v1 in enumerate(vs):
        for v2 in vs[i + 1:]:
            a, b = by_vendor[v1], by_vendor[v2]
            common = sorted(set(a) & set(b))
            diffs = sorted(abs(a[n]["pct"] - b[n]["pct"]) for n in common
                           if a[n].get("pct") is not None
                           and b[n].get("pct") is not None)
            if not diffs:
                continue
            checks.append({
                "pair": [v1, v2], "common": len(common),
                "median_pp": round(diffs[len(diffs) // 2], 4),
                "p90_pp": round(diffs[int(len(diffs) * 0.9)], 4),
                "max_pp": round(diffs[-1], 4),
                "within_1pp_pct": round(
                    sum(1 for d in diffs if d <= 1.0) / len(diffs) * 100, 1),
            })
    if checks:
        snap.cross_check = {"metric": "pct_change", "pairs": checks}
        worst = max(c["p90_pp"] for c in checks)
        if worst > 2.0:
            snap.notes.append(
                f"跨厂商涨跌幅 p90 偏差 {worst:.2f}pp 超过 2pp 阈值，请人工核对")

    out = _dedupe(out, snap)
    return out


def _dedupe(rows: list[dict], snap: Snapshot) -> list[dict]:
    """同名板块去重。实测 969 个里有重名。

    重名来源：新浪「证监会行业」和同花顺「行业」大量同名（电子、医药制造业…），
    开盘红「题材」和新浪「概念」也有重叠。不去重的话同一块会在评分榜出现
    两次，存库时互相覆盖，前端表格也重复。

    保留优先级：主源 > 有 code > 字段最全。合并时把被丢弃行的非空字段补进
    存活行（与 _merge 的「只补空」策略一致），并记 provenance 便于排查。
    """
    if not rows:
        return rows
    by_name: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        nm = r["name"]
        if nm not in by_name:
            by_name[nm] = r
            order.append(nm)
            continue
        keep, drop = by_name[nm], r
        # 决定谁活下来：主源优先，其次有 code 的，再次字段多的
        def rank(x):
            return (
                0 if x.get("source") == "ths" else 1,
                0 if (x.get("code") or "").strip() else 1,
                -sum(1 for f in ("pct", "turnover", "turnover_rate",
                                 "main_inflow", "up_count", "down_count",
                                 "lead_stock")
                     if x.get(f) not in (None, "")),
            )
        if rank(drop) < rank(keep):
            keep, drop = drop, keep
            by_name[nm] = keep
        # 补空字段
        for f in ("code", "pct", "turnover", "turnover_rate", "main_inflow",
                  "up_count", "down_count", "lead_stock", "lead_stock_pct",
                  "n_stocks", "amplitude", "market_cap"):
            if keep.get(f) in (None, "") and drop.get(f) not in (None, ""):
                keep[f] = drop[f]
                keep.setdefault("merged_from", []).append(
                    f"{drop.get('source')}:{f}")
        keep.setdefault("kinds", [keep.get("kind")])
        if drop.get("kind") and drop["kind"] not in keep["kinds"]:
            keep["kinds"].append(drop["kind"])

    out = [by_name[nm] for nm in order]
    dropped = len(rows) - len(out)
    if dropped:
        snap.notes.append(
            f"合并后去掉 {dropped} 个重名板块"
            f"（同一板块在多个源都出现，实测 969 个里有重名）")
    return out


def score_boards(boards: list[dict], zt: dict | None = None,
                 momentum: dict[str, float] | None = None) -> list[dict]:
    """按原 sector.py 公式算轮动评分，输出按分数降序。"""
    zt_by_board = (zt or {}).get("by_board") or {}
    momentum = momentum or {}

    all_pct = [b["pct"] for b in boards if b.get("pct") is not None]
    pos_flow = [b["main_inflow"] for b in boards
                if b.get("main_inflow") is not None and b["main_inflow"] > 0]

    for b in boards:
        s_pct = _pct_rank(all_pct, b.get("pct")) * 40
        if b.get("main_inflow") is not None and b["main_inflow"] > 0 and pos_flow:
            s_flow = _pct_rank(pos_flow, b["main_inflow"]) * 30
        else:
            s_flow = 0.0
        s_zt = min(zt_by_board.get(b["name"], 0) * 4, 20)
        m3 = momentum.get(b["name"])
        s_mom = min(m3 * 2, 10) if (m3 is not None and m3 > 0) else 0.0
        b["score"] = round(s_pct + s_flow + s_zt + s_mom, 1)
        b["score_parts"] = {"pct": round(s_pct, 1), "flow": round(s_flow, 1),
                            "zt": s_zt, "mom": round(s_mom, 1)}
        b["pct_rank"] = (round(_pct_rank(all_pct, b.get("pct")) * 100)
                         if b.get("pct") is not None else None)
        b["zt_count"] = zt_by_board.get(b["name"], 0)
    boards.sort(key=lambda x: x["score"], reverse=True)
    return boards


def build_momentum(board_names: list[str], days: int = 30,
                   limit: int = 0) -> dict[str, float]:
    """批量取板块指数历史算动量。d.10jqka 子域有日预算，limit 必填。"""
    if limit:
        board_names = board_names[:limit]
    out: dict[str, float] = {}
    for nm in board_names:
        try:
            hist = THS.index_history(nm, days=days)
        except SourceError:
            break                      # 预算用尽，别再打了
        m = _momentum(hist)
        if m is not None:
            out[nm] = m
    return out


def health() -> dict:
    return {
        "version": __import__("data_service").__version__,
        "sources": [s.health() for s in (THS, KPH, SINA, ZT)],
        "meter": METER.snapshot(),
    }
