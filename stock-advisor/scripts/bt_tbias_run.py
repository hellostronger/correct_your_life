# -*- coding: utf-8 -*-
"""t_bias 判定规则回测：旧规则（bear 需空头排列 且 收盘<日内均价）
vs 新规则（bear = 空头排列），对同一段历史各模拟一套做T建议，
比较净收益 / 接刀质量 / 躲跌暴露。

镜像 decide() 的门槛链（重要，别自造判据）：
- 机动仓 = 底仓 1000 × t_ratio 30% = 300 股（100 股整手）
- 高抛：四层基准（当日均价代理 + 5/20/60 日均线，**含当日**——与线上
  layer_anchors 口径一致），edge = (price/anchor-1)*100 > 0 取最大层，
  门槛 = max(min_edge 0.45% × sell_mult, 往返成本 × safety_mult 2.0)
- 待买回：门槛 = max(min_edge 0.45% × buy_mult, ...)，**趋势 down 再 ×2**
  （_refill_threshold bearish_ma=down），再与成本×2 兜底取大；
  bear（新规则）需先过 stabilizing（日K 上跑同一函数，n=6）
- 每次 t_sell 1 次 / 补回 1 次每日上限（日粒度天然满足）

口径与局限（报告里必须如实写）：
- 日收盘价成交（真实系统盘中成交）；当日均价代理 = 成交额/成交量（原始口径）
- 分类用的也是当日均价代理 —— 轻微当日信息，新旧规则同享，公平
- 沪深300 用**当前成分**：幸存者偏差（20 年前名单≠今天名单），两规则同享
- 不复权价：除权日跳空两规则同享；不模拟涨跌停无法成交
- 成本：佣金万2.5（最低5元）×2 + 印花税 0.05%（卖出）+ 滑点 0.05% ×2

跑法：python scripts/bt_tbias_run.py  （读 data/bt_tbias/*.csv）
"""
import glob
import json
import os
import sys
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                     # noqa: E402
import t_trading as TT                                  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data", "bt_tbias")
OUT = os.path.join(HERE, "data", "bt_tbias_results.jsonl")

MIN_EDGE = 0.45
SAFETY = 2.0
BASE_SHARES = 1000
T_RATIO = 30            # %
C_MIN_BARS = 60         # 需要 60 日均线


def rt_cost_pct(price: float) -> float:
    """A股往返成本%（金额 300 股 × 价位量级，佣金最低 5 元按 1.5 万额度假定）。"""
    v = price * 300
    fee = max(v * 2.5e-4, 5.0)
    return (fee * 2 + v * 5e-4 + v * 1e-3) / v * 100    # 佣金×2+印花+滑点×2


def bias_old(trend: str, vwap, price: float) -> str:
    """旧规则：bear 需空头排列 且 收盘<日内均价；缺均价时 down 落 range。"""
    above = vwap is not None and price > vwap
    below = vwap is not None and price < vwap
    if trend == "up" and above:
        return "bull"
    if trend == "down" and below:
        return "bear"
    if trend == "flat" and vwap is None:
        return "flat"
    return "range"


def bias_new(trend: str, vwap, price: float) -> str:
    """新规则（2026-11-08 已实施）：bear/bull 只看排列；flat+缺均价才 flat。"""
    if trend == "up":
        return "bull"
    if trend == "down":
        return "bear"
    return "flat" if vwap is None else "range"


RULES = {"old": bias_old, "new": bias_new}


def simulate(closes, vwap, lows, rule):
    """单规则做T模拟。返回统计 dict。锚/均线含当日（与 layer_anchors 一致）。"""
    mults = {"bull": (2.0, 0.7, False), "range": (1.0, 1.0, False),
             "bear": (0.7, 2.0, True)}
    movable = 0            # 当前在外（未持有）的机动股数；0=机动仓在手
    anchor = None          # 上次高抛价
    cash = 0.0             # T 往返累计净额（含双边成本）
    rounds = sells = refills = 0
    knives = 0
    refill_prices = []     # (index, price) 供接刀判定
    down_days = range_on_down_days = 0
    hide_days = 0          # 下行趋势中机动仓在现金里的天数（躲跌暴露）
    n = len(closes)
    for i in range(C_MIN_BARS, n):
        price = closes[i]
        ma20 = sum(closes[i - 19:i + 1]) / 20.0
        ma60 = sum(closes[i - 59:i + 1]) / 60.0
        trend = ("up" if price > ma20 > ma60
                 else "down" if price < ma20 < ma60 else "flat")
        vw = vwap[i]
        b = rule(trend, vw, price)
        if trend == "down":
            down_days += 1
            if b == "range":
                range_on_down_days += 1
            if movable == 300:
                hide_days += 1
        sm, bm, need_stab = mults.get(b, mults["range"])

        if movable == 0:
            # 高抛：四层取最大正偏离层（L1=当日均价代理，L2/3/4=5/20/60日）
            anchors = [("L1", vw), ("L2", sum(closes[i - 4:i + 1]) / 5.0),
                       ("L3", sum(closes[i - 19:i + 1]) / 20.0),
                       ("L4", sum(closes[i - 59:i + 1]) / 60.0)]
            best = max(((ln, (price / a - 1) * 100) for ln, a in anchors
                        if a and a > 0), key=lambda x: x[1], default=None)
            thr = max(MIN_EDGE * sm, rt_cost_pct(price) * SAFETY)
            if best and best[1] >= thr:
                cash += price * 300          # 卖出入账（成本在回合完成时结算）
                anchor = price
                movable = 300
                sells += 1
        else:
            # 待买回（移动平均法下底仓成本不变；T 只看往返价差）
            base = MIN_EDGE * bm
            if trend == "down":
                base *= 2.0                              # _refill_threshold
            thr = max(base, rt_cost_pct(price) * SAFETY)
            need = anchor * (1 - thr / 100.0)
            ok = True
            if need_stab:
                ok, _ = TT.stabilizing(
                    [{"close": closes[j], "low": lows[j]}
                     for j in range(max(0, i - 6), i + 1)], n=6)
            if ok and price <= need:
                # 成本兜底后门槛可能上调
                thr = max(thr, rt_cost_pct(price) * SAFETY)
                need = anchor * (1 - thr / 100.0)
                if price <= need:
                    # 回合完成：卖出腿已在 anchor 记账，此处结买入腿 + 全程成本
                    cash -= price * 300
                    mid = (anchor + price) / 2.0
                    cash -= mid * 300 * rt_cost_pct(mid) / 100.0
                    rounds += 1
                    refills += 1
                    refill_prices.append((i, price))
                    movable = 0
                    anchor = None
    # 结尾仍未接回的按最后收盘虚拟结算（不算盈利，只算敞口）
    for idx, rp in refill_prices:
        j = min(idx + 5, n - 1)
        if closes[j] < rp * 0.97:
            knives += 1
    open_leg = anchor is not None
    unrealized = ((anchor or 0) - closes[-1]) * 300 if open_leg else 0.0
    return {"rounds": rounds, "sells": sells, "refills": refills,
            "knives": knives, "down_days": down_days,
            "range_on_down": range_on_down_days, "hide_days": hide_days,
            "cash": round(cash, 2), "unrealized_open": round(unrealized, 2)}


def main():
    files = sorted(glob.glob(os.path.join(DATA, "*.csv")))
    meta = {m["code"]: m["name"] for m in json.load(
        open(os.path.join(DATA, "_cons.json"), encoding="utf-8"))}
    print(f"成分股 CSV {len(files)} 份", flush=True)
    out = open(OUT, "w", encoding="utf-8")
    agg = {r: {"rounds": 0, "knives": 0, "cash": 0.0, "unrealized": 0.0,
               "down_days": 0, "range_on_down": 0, "hide_days": 0,
               "per_stock_cash": [], "knife_rates": []}
           for r in RULES}
    skipped = 0
    for k, f in enumerate(files):
        code = os.path.basename(f)[:-4]
        try:
            df = pd.read_csv(f)
            if len(df) < C_MIN_BARS + 10:
                skipped += 1
                continue
            closes = df["收盘"].astype(float).tolist()
            lows = df["最低"].astype(float).tolist()
            amt = df["成交额"].astype(float).tolist()
            vol = df["成交量"].astype(float).tolist()
            # ⚠️ 成交量单位是「手」（1 手 = 100 股）—— 不乘 100 会把均价
            # 高估 100 倍，旧规则的「收盘<均价」条件恒真，两规则完全同结果
            # （首跑烟测实测）。验算：600519 2026-11-06 成交额 61.995e8 /
            # (42848 手×100) = 1447 元 ≈ 收盘 1453 ✓
            vwap = [a / (v * 100) if v and v > 0 and a and a > 0 else None
                    for a, v in zip(amt, vol)]
        except Exception as exc:                        # noqa: BLE001
            print(f"skip {code}: {exc}", flush=True)
            skipped += 1
            continue
        row = {"code": code, "name": meta.get(code, ""), "bars": len(closes)}
        for rname, rule in RULES.items():
            s = simulate(closes, vwap, lows, rule)
            row[rname] = s
            a = agg[rname]
            a["rounds"] += s["rounds"]
            a["knives"] += s["knives"]
            a["cash"] += s["cash"]
            a["unrealized"] += s["unrealized_open"]
            a["down_days"] += s["down_days"]
            a["range_on_down"] += s["range_on_down"]
            a["hide_days"] += s["hide_days"]
            if s["rounds"] >= 3:
                a["per_stock_cash"].append(s["cash"])
                a["knife_rates"].append(s["knives"] / max(1, s["rounds"]))
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        if k % 25 == 0:
            print(f"[{k}/{len(files)}] {code}", flush=True)
    out.close()
    summary = {}
    for r, a in agg.items():
        n_stocks = max(1, len(a["per_stock_cash"]))
        summary[r] = {
            "rounds": a["rounds"], "knives": a["knives"],
            "knife_rate": round(a["knives"] / max(1, a["rounds"]), 4),
            "cash_total": round(a["cash"], 0),
            "unrealized_open": round(a["unrealized"], 0),
            "cash_median_per_stock": round(median(a["per_stock_cash"]), 0) if a["per_stock_cash"] else None,
            "down_days": a["down_days"],
            "range_on_down": a["range_on_down"],
            "range_on_down_pct": round(a["range_on_down"] / max(1, a["down_days"]) * 100, 2),
            "hide_days": a["hide_days"],
            "n_stocks_with_trades": n_stocks,
        }
    json.dump(summary, open(os.path.join(HERE, "data", "bt_tbias_summary.json"),
                            "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"ALL DONE（skipped {skipped}）", flush=True)


if __name__ == "__main__":
    main()
