"""因子计算 worker：由 Vibe-Trading 的 venv 执行，进程边界隔离上游依赖。

设计约束（与 integrations/ 的接入原则一致）：
- 只有这个脚本 import 上游 ``src.factors``；主应用不 import、不 vendor。
- 输入/输出都是 JSON + CSV，主应用只认 ``integrations/contracts/factors.json``。
- 上游算子沿用 registry 的 ``compute``，不重写公式；这里只做横截面 z、
  样本内 rank IC、按主题聚合 —— 都是「怎么用算子」，不是「算子是什么」。

用 Vibe venv 跑::

    D:\\vibe-trading\\.venv\\Scripts\\python.exe factor_worker.py job.json

job.json::

    {
      "panel": "panel.csv",          # long: date,code,open,high,low,close,volume,amount
      "out": "factor_out.json",
      "universe": "equity_cn",       # 或 equity_hk
      "focus_codes": ["600519"],     # 只回这些票的聚合结果（横截面仍用全样本）
      "amount_multiplier": 100,      # A 股 volume=手 -> 股；港股填 1
      "ic_horizon": 1,
      "ic_min_days": 120,
      "ic_min_abs": 0.015,
      "ic_min_ir": 0.15,
      "z_clip": 3.0,
      "max_alphas": 0
    }

输出见 ``integrations/contracts/factors.json``。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

PRICE_COLS = ("open", "high", "low", "close", "volume", "amount")
MIN_IC_CODES = 5          # 与上游 factor_analysis_core._MIN_VALID_PER_DATE 一致


def _upstream_version() -> str:
    try:
        from importlib.metadata import version
        for name in ("vibe-trading-ai", "vibe_trading", "vibe-trading"):
            try:
                return version(name)
            except Exception:                     # noqa: BLE001
                continue
    except Exception:                             # noqa: BLE001
        pass
    return "unknown"


def _load_panel(path: str, universe: str, amount_mult: float):
    """long CSV -> {col: wide DataFrame(index=date, columns=code)}。"""
    df = pd.read_csv(path, dtype={"code": str}, parse_dates=["trade_date"])
    if "trade_date" in df.columns:
        df = df.rename(columns={"trade_date": "date"})
    if df.empty:
        raise ValueError("panel 为空")
    df = df[df["code"].astype(str).str.len().between(3, 6)]
    for col in PRICE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = np.nan
    # 成交额缺失时按 volume 口径补。A 股 volume 单位是手（*100 股），
    # 港股是股（*1）；只在**同一 universe 内**比较，所以倍数不影响横截面。
    if df["amount"].isna().all():
        df["amount"] = df["close"] * df["volume"] * float(amount_mult)
    df = df.sort_values(["date", "code"])

    panel = {}
    for col in PRICE_COLS:
        wide = df.pivot(index="date", columns="code", values=col).astype(float)
        wide = wide.sort_index()
        if wide.notna().any().any():
            panel[col] = wide
    if "close" not in panel:
        raise ValueError("panel 里没有 close")
    return panel, df


def _select_alphas(reg, universe: str, panel: dict, n_days: int, max_alphas: int = 0):
    selected, skipped = [], []
    for aid in reg.list(universe=universe):
        try:
            meta = reg.get(aid).meta
        except Exception as exc:                  # noqa: BLE001
            skipped.append({"id": aid, "why": "meta: %s" % str(exc)[:80]})
            continue
        cols = list(meta.get("columns_required") or [])
        if any(c.startswith(("fund:", "val:")) for c in cols):
            skipped.append({"id": aid, "why": "需要财报/估值字段（当前数据面未提供）"})
            continue
        if meta.get("requires_sector"):
            skipped.append({"id": aid, "why": "需要行业分类"})
            continue
        missing = [c for c in cols if c not in panel]
        if missing:
            skipped.append({"id": aid, "why": "缺列 %s" % ",".join(missing)})
            continue
        if int(meta.get("min_warmup_bars") or 0) > n_days:
            skipped.append({"id": aid, "why": "预热 %s 根 > 面板 %s 根"
                            % (meta.get("min_warmup_bars"), n_days)})
            continue
        selected.append(aid)
    if max_alphas and len(selected) > max_alphas:
        skipped.extend({"id": a, "why": "超过 max_alphas"} for a in selected[max_alphas:])
        selected = selected[:max_alphas]
    return selected, skipped


def _alpha_meta(reg, aid: str) -> dict:
    alpha = reg.get(aid)
    m = alpha.meta
    return {
        "id": aid,
        "zoo": str(getattr(alpha, "zoo", "") or aid.split("_", 1)[0]),
        "nickname": str(m.get("nickname") or "")[:200],
        "themes": list(m.get("theme") or []),
        "columns": list(m.get("columns_required") or []),
        "notes": str(m.get("notes") or "")[:400],
    }


def _ic_stats(out: pd.DataFrame, fwd_ret: pd.DataFrame) -> dict:
    """样本内 rank IC（逐日横截面 Spearman 的均值/IR）。

    只用因子与次日收益**同时存在**的格子；一天少于 5 只票就丢弃。
    这是「这批票上的历史方向」，不是上游 bench 的 csi300 结论。
    """
    common_dates = out.index.intersection(fwd_ret.index)
    common_codes = out.columns.intersection(fwd_ret.columns)
    if len(common_dates) == 0 or len(common_codes) == 0:
        return {"ic_mean": None, "ic_ir": None, "ic_days": 0}
    f = out.loc[common_dates, common_codes]
    r = fwd_ret.loc[common_dates, common_codes]
    mask = f.notna() & r.notna()
    n = mask.sum(axis=1)
    fr = f.where(mask).rank(axis=1, method="average")
    rr = r.where(mask).rank(axis=1, method="average")
    ic = fr.corrwith(rr, axis=1, method="pearson")
    ic = ic[n >= MIN_IC_CODES].replace([np.inf, -np.inf], np.nan).dropna()
    if ic.empty:
        return {"ic_mean": None, "ic_ir": None, "ic_days": 0}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if len(ic) > 1 else 0.0
    ir = float(mean / std) if std > 0 else None
    if not np.isfinite(mean):
        return {"ic_mean": None, "ic_ir": None, "ic_days": 0}
    return {"ic_mean": round(mean, 6),
            "ic_ir": (round(ir, 4) if ir is not None and np.isfinite(ir) else None),
            "ic_days": int(len(ic))}


def _aggregate(selected, metas, zmap, icmap, min_days, min_abs, min_ir, clip,
               strong_ratio=0.0, medium_ratio=0.0):
    """按主题/方向聚合成每只票的暴露。

    方向不硬编码：用面板自身的**样本内 rank IC** 定符号，且要求 |IC|、|IR|、
    天数同时过门槛；没过门槛的算子在 ``top`` 里只展示暴露，不参与合成。

    置信度：A 股用绝对阈值 80/40（约 350 个算子）。港股可算算子只有约 160 个，
    用绝对数会把所有票都压成 weak，所以传入 ``strong_ratio`` / ``medium_ratio``
    时改按「有效算子 / 已算出算子」的比例判定（默认 20% / 10%）。
    """
    sign = {}
    for aid in selected:
        s = icmap.get(aid) or {}
        m, ir, n = s.get("ic_mean"), s.get("ic_ir"), s.get("ic_days") or 0
        if (m is not None and abs(m) >= min_abs and n >= min_days
                and ir is not None and abs(ir) >= min_ir):
            sign[aid] = 1.0 if m > 0 else -1.0

    out = {}
    for code in zmap:
        themes = {}
        vals = []
        for aid, z in zmap[code].items():
            sgn = sign.get(aid)
            if sgn is None:
                continue
            v = max(-clip, min(clip, z * sgn))
            vals.append(v)
            for t in metas[aid]["themes"]:
                themes.setdefault(t, []).append(v)
        if not vals:
            out[code] = {"composite": None, "n_used": 0,
                         "theme_scores": {}, "confidence": "none"}
            continue
        comp = float(np.mean(vals))
        n_used = len(vals)
        n_alphas = len(zmap[code])
        if strong_ratio > 0:
            strong_min = max(1, int(round(n_alphas * strong_ratio)))
            medium_min = max(1, int(round(n_alphas * medium_ratio)))
        else:
            strong_min, medium_min = 80, 40
        conf = "strong" if n_used >= strong_min else (
            "medium" if n_used >= medium_min else "weak")
        out[code] = {
            "composite": round(comp, 4),
            "n_used": n_used,
            "confidence": conf,
            "theme_scores": {
                t: {"z": round(float(np.mean(v)), 4), "n": len(v)}
                for t, v in sorted(themes.items())
            },
        }
    return out, sign


def run(job: dict) -> dict:
    t0 = time.time()
    panel_path = str(job["panel"])
    universe = str(job.get("universe") or "equity_cn")
    focus = [str(c) for c in (job.get("focus_codes") or [])]
    amount_mult = float(job.get("amount_multiplier", 100 if universe == "equity_cn" else 1))
    horizon = max(1, int(job.get("ic_horizon") or 1))
    min_days = max(5, int(job.get("ic_min_days") or 120))
    min_abs = abs(float(job.get("ic_min_abs") or 0.015))
    min_ir = abs(float(job.get("ic_min_ir") or 0.15))
    clip = float(job.get("z_clip") or 3.0)
    max_alphas = max(0, int(job.get("max_alphas") or 0))
    # 港股面板小、算子通过率低，用「已用算子数 / 可用算子数」的**比例**定档；
    # A 股沿用绝对阈值（80/40）。传 0 表示走绝对阈值。
    strong_ratio = max(0.0, float(job.get("strong_ratio") or 0))
    medium_ratio = max(0.0, float(job.get("medium_ratio") or 0))

    from src.factors.registry import get_default_registry
    reg = get_default_registry()
    panel, df = _load_panel(panel_path, universe, amount_mult)
    close = panel["close"]
    n_days = len(close)
    selected, skipped = _select_alphas(reg, universe, panel, n_days, max_alphas)
    metas = {aid: _alpha_meta(reg, aid) for aid in selected}

    as_of = job.get("as_of")
    last_date = pd.Timestamp(as_of) if as_of else close.index[-1]
    if last_date not in close.index:
        last_date = close.index[-1]

    # 次日收益（t -> t+horizon），只用于样本内 IC
    fwd_ret = close.shift(-horizon) / close - 1.0

    alpha_rows, errors = [], []
    zmap, pctmap, icmap = {}, {}, {}
    last_vals = {}
    for i, aid in enumerate(selected):
        try:
            out = reg.compute(aid, panel)
            if not isinstance(out, pd.DataFrame) or out.empty:
                raise ValueError("空输出")
            out = out.reindex(index=close.index, columns=close.columns)
            out = out.replace([np.inf, -np.inf], np.nan)
            ic = _ic_stats(out, fwd_ret)
            icmap[aid] = ic
            row = out.loc[last_date].dropna()
            row = row[np.isfinite(row.to_numpy(dtype=float))]
            coverage = float(len(row)) / max(1, len(close.columns))
            alpha_rows.append({**metas[aid], **ic,
                               "coverage": round(coverage, 4), "error": ""})
            if row.empty:
                continue
            for code, raw in row.items():
                last_vals.setdefault(aid, {})[str(code)] = float(raw)
        except Exception as exc:                  # noqa: BLE001
            msg = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            errors.append({"id": aid, "error": msg})
            alpha_rows.append({**metas[aid], "ic_mean": None, "ic_ir": None,
                               "ic_days": 0, "coverage": 0.0, "error": msg})
        if (i + 1) % 50 == 0:
            print("[factor-worker] %d/%d %.0fs" % (i + 1, len(selected),
                                                   time.time() - t0),
                  flush=True)

    # 横截面 z / 分位：**同一算子在所有票之间**标准化（不是一只票跨算子）。
    # 每个算子只在最后一行取值；这只决定展示口径，不改变算子本身输出。
    for aid, vals in last_vals.items():
        codes = list(vals.keys())
        arr = np.array(list(vals.values()), dtype=float)
        n = len(arr)
        if n >= MIN_IC_CODES:
            mu, sd = float(arr.mean()), float(arr.std(ddof=1))
        else:
            mu, sd = float(arr.mean()), 0.0
        order = pd.Series(arr).rank(pct=True).to_numpy() * 100.0
        for code, raw, p in zip(codes, arr, order):
            z = 0.0 if sd <= 1e-12 else (raw - mu) / sd
            zmap.setdefault(code, {})[aid] = float(z)
            pctmap.setdefault(code, {})[aid] = float(p)

    agg, sign = _aggregate(selected, metas, zmap, icmap,
                           min_days, min_abs, min_ir, clip,
                           strong_ratio=strong_ratio, medium_ratio=medium_ratio)

    # 所有面板票都给聚合结果（策略标的可能不在自选里）；top 只给关注票，
    # 控制 JSON/落库体积。任何票都能从 sa_factor_code 查到。
    focus_set = set(focus)
    codes_out = {}
    for code, a in agg.items():
        if code in focus_set:
            zs = zmap.get(code) or {}
            # 先列「方向可信」的算子，再按 |z|；极端 z 只截断展示，不参与合成
            top = sorted(zs.items(),
                         key=lambda kv: (kv[0] not in sign, -abs(kv[1])))[:8]
            a["top"] = [{
                "id": aid,
                "nickname": metas[aid]["nickname"],
                "themes": metas[aid]["themes"],
                "z": round(max(-10.0, min(10.0, z)), 3),
                "pct": round(pctmap.get(code, {}).get(aid, 0.0), 1),
                "ic_mean": icmap.get(aid, {}).get("ic_mean"),
                "ic_used": aid in sign,
            } for aid, z in top]
            a["n_alphas"] = len(zs)
        else:
            a["top"] = []
            a["n_alphas"] = len(zmap.get(code) or {})
        codes_out[code] = a
    # 关注票在面板里没有日线（港股/停牌/未同步）也显式返回，不能静默消失
    for code in focus:
        if code in codes_out:
            continue
        codes_out[code] = {
            "composite": None, "n_used": 0, "n_alphas": 0,
            "theme_scores": {}, "confidence": "none", "top": [],
            "missing": "面板里没有这只票的日线",
        }

    return {
        "ok": True,
        "upstream_version": _upstream_version(),
        "universe": universe,
        "as_of": str(last_date.date()),
        "panel": {"codes": int(len(close.columns)), "days": int(n_days),
                  "start": str(close.index[0].date()),
                  "end": str(close.index[-1].date()),
                  "rows": int(len(df))},
        "params": {"ic_horizon": horizon, "ic_min_days": min_days,
                   "ic_min_abs": min_abs, "ic_min_ir": min_ir, "z_clip": clip,
                   "amount_multiplier": amount_mult},
        "alphas": alpha_rows,
        "codes": codes_out,
        "skipped": skipped[:400],
        "errors": errors,
        "elapsed_s": round(time.time() - t0, 1),
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: factor_worker.py job.json", file=sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                             # noqa: BLE001
        pass
    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    try:
        res = run(job)
    except Exception as exc:                      # noqa: BLE001
        traceback.print_exc()
        res = {"ok": False,
               "error": "%s: %s" % (type(exc).__name__, str(exc)[:600])}
    out = job.get("out")
    text = json.dumps(res, ensure_ascii=False, allow_nan=False)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
