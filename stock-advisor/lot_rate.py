"""lot_rate.py —— 待申购新股的中签率估算（发行公告未公布前的预判）。

## 问题

网上申购要用的**中签率**，只有发行公告（或上市公告）才披露。akshare 的
`stock_xgsglb_em` 里 `中签率` 字段对**未上市的票是空的** —— 所以现在策略里
两只待申购新股只能给 `evaluate`（无法评估），没法算出期望收益。

## 数学：可以从历史反推

    中签率 = 回拨后网上发行量 / 有效申购总股数
    有效申购总股数 = 全市场总配号数 × 每配号股数
  => 总配号数 = 回拨后网上发行量 / (中签率 × 每配号股数)

「全市场总配号数」= 全体打新者的市值 ÷ 5000，是个**相对稳定的量**
（它反映的是有多少人在打新），所以拿历史新股反推它的中位数，
就能给未来的新股做估算。

**用长鑫科技校验过公式**（2026-10-01）：
    网上最终发行 38.5 亿股、中签率 0.47141739%、每配号 500 股
    反推总配号数 = 38.5亿 / (0.004714 × 500) = **16.33 亿个**
    公开披露的配号总数 = 16.34 亿个 ✅ 差 0.06%

## 两个必须踩过的坑（实测踩出来的）

**坑 1：`网上发行` 字段是回拨【前】初值，不能直接用。**

    实测比值中位 1220（`网上发行` ÷ `申购上限`），而交易所规则是 1000
    （申购上限 = 网上发行量 × 千分之一）。差的那部分就是回拨。
    所以正确口径是 **回拨后网上发行 ≈ 申购上限 × 1000**。

**坑 2：北交所（920/83/87/88 开头）反推出 1.7 万亿以上配号，离群 234~894 倍。**

    不是公式错，是北交所的配售规则与沪深不同（顶格申购不按市值配售，
    现金申购机制不同），同一公式套上去必然出荒谬值。
    **必须排除**，否则中位数被彻底带偏。

## 所以这里给的是「区间 + 置信度」，不是精确值

实测沪深/创业板/科创板反推值离散度仍有 169%（不同板的申购上限口径不完全一致），
所以：
- 给 **P25 ~ P75 区间**（而不是一个假装精确的点值）
- `confidence` 按样本量与离散度如实标注
- 估算值**不参与** `expected_profit` 的最终结论，除非置信度够高
- 发行公告一出就用真实值覆盖（`lot_rate` 字段非空时优先）

绝不用一个"看起来很准"的单点数字骗人 —— 打新期望收益对这个数很敏感，
但错误的精确比粗糙的区间更危险。
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import date
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "lot_rate_model.json"

# 每配号股数（1 个申购单位）
SPU = {"科创板": 500, "创业板": 500, "主板": 1000, "北交所": 100,
       "非科创板": 1000, "": 1000}
LOT_VALUE = 5000          # 每 5000 元日均市值 = 1 个配号

# 离群阈值：反推配号数偏离中位数这个倍数就剔除
OUTLIER_K = 3.0


def _is_bj(code: str) -> bool:
    c = str(code or "")
    return c.startswith(("92", "83", "87", "88", "43"))


def spu_of(board: str) -> int:
    return SPU.get(str(board or ""), 1000)


def implied_pairs(online_after_shares: float, lot_rate: float,
                  shares_per_lot: int) -> float | None:
    """单只新股反推的全市场总配号数。"""
    if not lot_rate or lot_rate <= 0 or online_after_shares <= 0:
        return None
    return online_after_shares / (lot_rate * shares_per_lot)


def backout_pairs(sample: dict) -> dict | None:
    """从一只**已上市**新股反推总配号数。排除北交所。"""
    code = str(sample.get("code") or "")
    if _is_bj(code):
        return None
    lr = sample.get("lot_rate")
    cap = sample.get("sub_cap")
    if not lr or not cap:
        return None
    try:
        lr, cap = float(lr), float(cap)
    except (TypeError, ValueError):
        return None
    if lr <= 0 or cap <= 0:
        return None
    # 回拨后网上发行 ≈ 申购上限 × 1000（千分之一规则，坑 1）
    online_after = cap * 1000 * 1e4        # 万股 -> 股
    pairs = implied_pairs(online_after, lr, spu_of(sample.get("board")))
    if not pairs:
        return None
    lst = ((sample.get("steps") or {}).get("上市日") or {}).get("date", "")
    return {"code": code, "name": sample.get("name"),
            "board": sample.get("board"), "pairs": pairs,
            "pairs_yi": pairs / 1e8, "list_date": lst[:10], "lot_rate": lr}


def fit_model(samples: list[dict], *, recent_n: int = 20,
              min_samples: int = 6) -> dict:
    """拟合「全市场总配号数」模型，返回中位数 + P25/P75 + 置信度。"""
    raw = [r for r in (backout_pairs(s) for s in (samples or [])) if r]
    raw = [r for r in raw if r["list_date"]]        # 只用已上市的
    raw.sort(key=lambda r: r["list_date"])
    raw = raw[-recent_n:] if raw else []

    if len(raw) < min_samples:
        return {"available": False,
                "why": f"可反推样本仅 {len(raw)} 只（需 ≥{min_samples}）",
                "n": len(raw),
                "note": "样本不足时**不给估算值** —— 宁可说算不出，"
                        "也不给一个没有依据的中签率"}

    vals = [r["pairs_yi"] for r in raw]
    med = statistics.median(vals)
    # 剔除离群（跨板口径不一致导致的极端值），再算中位数与分位数
    kept = [v for v in vals if med > 0 and v <= med * OUTLIER_K]
    dropped = len(vals) - len(kept)
    if len(kept) < min_samples:
        kept, dropped = vals, 0
    kept.sort()
    n = len(kept)
    p25 = kept[max(0, int(n * 0.25))]
    p75 = kept[min(n - 1, int(n * 0.75))]
    med2 = statistics.median(kept)
    spread = (p75 - p25) / med2 if med2 else 9

    # 置信度：样本越多、离散越小越高
    if n >= 12 and spread < 0.5:
        conf = "medium"
    elif n >= 8 and spread < 1.0:
        conf = "low"
    else:
        conf = "very_low"

    return {
        "available": True,
        "n": n, "dropped_outliers": dropped,
        "median_pairs_yi": round(med2, 1),
        "p25": round(p25, 1), "p75": round(p75, 1),
        "spread": round(spread, 2),
        "confidence": conf,
        "range_yi": [round(kept[0], 1), round(kept[-1], 1)],
        "note": ("总配号数 = 全市场打新者市值 ÷ 5000，"
                 "取最近 %d 只已上市新股反推的中位数；"
                 "P25~P75 区间反映口径差异" % n),
        "samples": [{"name": r["name"], "date": r["list_date"],
                     "pairs_yi": round(r["pairs_yi"], 1)}
                    for r in raw[-8:]],
    }


def estimate_lot_rate(new_stock: dict, model: dict | None = None,
                      *, conn=None) -> dict:
    """给一只待申购新股估中签率。

    返回 {available, estimate, low, high, confidence, basis}。
    **发行公告已公布时（lot_rate 非空）直接返回真实值**，不估算。

    估算法：
        中签率 = 回拨后网上发行 / (总配号数中位数 × 每配号股数)

    `回拨后网上发行` 用 `申购上限 × 1000` 推（坑 1）。若申购上限缺失，
    退而用 `网上发行 × 回拨系数`，回拨系数由同类历史样本反推；
    两者都没有就明说算不出。
    """
    code = str(new_stock.get("code") or "")
    is_bj = _is_bj(code)

    # ⚠️ 北交所检查必须放在最前面（2026-10-01 修）。
    # 原先顺序是「先看 lot_rate 真实值 → 再判北交所」，结果带中签率的北交所票
    # 全部走 actual 分支返回「实测值·置信度 high」，看起来很可信 ——
    # 但北交所的配号规模与沪深差 2~3 个数量级（实测反推 1.7 万亿 vs 71 亿），
    # 拿这个数去和沪深底仓比期望收益是错的。
    # 所以北交所一律单独标记，即使它有实测中签率。
    if is_bj:
        real_bj = new_stock.get("lot_rate")
        return {"available": False, "estimate": None,
                "source": "unsupported_market",
                "why": ("北交所（920/83/87/88）配售规则与沪深不同"
                        "（顶格申购不按沪深市值配售），本模型不适用"
                        + ("；该票实测中签率 %s%% 仅供参考，不能与沪深混算"
                           % (round(float(real_bj) * 100, 4)
                              if real_bj else "—"))),
                "actual_lot_rate": real_bj}

    real = new_stock.get("lot_rate")
    if real:
        try:
            r = float(real)
            if r > 0:
                return {"available": True, "estimate": r, "source": "actual",
                        "basis": "发行公告已披露（实测值）",
                        "confidence": "high"}
        except (TypeError, ValueError):
            pass

    model = model if model is not None else load_model(conn)
    if not model.get("available"):
        return {"available": False,
                "why": model.get("why", "总配号数模型不可用"),
                "estimate": None, "source": "none"}

    spu = spu_of(new_stock.get("board"))
    online_after = None
    src = ""
    cap = new_stock.get("sub_cap")
    if cap:
        try:
            online_after = float(cap) * 1000 * 1e4
            src = "申购上限×1000（千分之一规则）"
        except (TypeError, ValueError):
            pass
    if online_after is None:
        return {"available": False, "estimate": None, "source": "no_data",
                "why": "缺「申购上限」与「网上发行」，无法推回拨后网上发行量"}

    pairs_med = model["median_pairs_yi"] * 1e8
    if not pairs_med or pairs_med <= 0:
        return {"available": False, "estimate": None, "source": "bad_model",
                "why": "总配号数中位数异常"}

    est = online_after / (pairs_med * spu)
    low = online_after / (model["p75"] * 1e8 * spu)     # 配号多 → 中签率低
    high = online_after / (model["p25"] * 1e8 * spu)
    return {
        "available": True,
        "estimate": est, "low": low, "high": high,
        "source": "estimated",
        "confidence": model.get("confidence", "low"),
        "basis": (f"回拨后网上发行 {online_after/1e8:.2f} 亿股（{src}）"
                  f" ÷ 总配号数 {model['median_pairs_yi']:.0f} 亿"
                  f"（{model['n']} 只样本，P25~P75 {model['p25']}~{model['p75']} 亿）"
                  f" ÷ 每配号 {spu} 股"),
        "model_note": model.get("note"),
        "caveat": ("发行公告公布后会被真实值覆盖；"
                   "置信度 %s 时只作参考，别据此做大幅调仓"
                   % model.get("confidence", "low")),
    }


def expected_profit(lots: int, lot_rate_estimate: dict,
                    per_lot_profit: float | None) -> dict | None:
    """按估算中签率算期望收益。**置信度不够时不算**，只给区间。"""
    if not lot_rate_estimate.get("available") or not per_lot_profit:
        return None
    est = lot_rate_estimate.get("estimate")
    if not est:
        return None
    out = {"point": lots * est * per_lot_profit,
           "confidence": lot_rate_estimate.get("confidence")}
    if lot_rate_estimate.get("low") is not None:
        out["low"] = lots * lot_rate_estimate["low"] * per_lot_profit
        out["high"] = lots * lot_rate_estimate["high"] * per_lot_profit
    return out


# ==========================================================================
# 模型缓存
# ==========================================================================

def load_model(conn=None) -> dict:
    """读模型缓存；没有就用 conn 现算并缓存（同一天不重算）。"""
    try:
        c = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if date.today().isoformat() == c.get("date"):
            return c["model"]
    except (OSError, ValueError, KeyError):
        pass
    import ipo_calendar
    samples = ipo_calendar._load().get("items") or []
    m = fit_model(samples)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(
            {"date": date.today().isoformat(), "model": m},
            ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return m


def status(conn=None) -> dict:
    m = load_model(conn)
    return {"model": m, "cache_file": str(STATE_FILE)}


def digest(conn=None) -> str:
    """给 LLM 看的口径说明（不含估算数字 —— 数字由代码算）。"""
    m = load_model(conn)
    if not m.get("available"):
        return (f"## 中签率估算模型不可用\n\n"
                f"{m.get('why')}\n\n{m.get('note','')}\n\n"
                f"**没有中签率就没法算期望收益**，请只做定性判断"
                f"（这只票值不值得为它挪市值），不要假设一个中签率。")
    return ("## 中签率估算口径\n"
            f"- 模型样本 {m['n']} 只已上市新股（剔除离群 {m.get('dropped_outliers',0)} 只）\n"
            f"- 全市场总配号数中位 **{m['median_pairs_yi']:.0f} 亿个**"
            f"（P25~P75 {m['p25']}~{m['p75']} 亿，离散度 {m['spread']}）\n"
            f"- 置信度 **{m['confidence']}**\n"
            "- 公式：中签率 = 回拨后网上发行 ÷ (总配号数 × 每配号股数)\n"
            "- ⚠️ 北交所（920/83/87/88）不适用本模型，配售规则不同\n"
            "- ⚠️ 发行公告一出即用真实值覆盖估算；"
            "置信度 low/very_low 时区间很宽，别据此做大幅调仓")


if __name__ == "__main__":      # python lot_rate.py
    import json as _json
    import ipo_calendar
    ss = ipo_calendar._load().get("items") or []
    print(_json.dumps(fit_model(ss), ensure_ascii=False, indent=1))
    print(digest())
