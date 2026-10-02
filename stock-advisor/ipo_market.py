"""ipo_market.py —— 打新的**市场上下文**：破发率、情绪、板块热度、发行特征。

## 为什么要单独一层

打新收益 = 中签率 × 每签盈利。`中签率` 是规则算得出的（`ipo_strategy`），
但**每签盈利强依赖当下市场**：同一只票在 2020 和 2026 首日能差好几倍。
把它写成 `涨幅 = 100%` 这种常数，等于假装市场永远不变 —— 那不是估算，是编数字。

所以这一层只做一件事：**从已上市新股的真实数据 + 当下市场状态里，算出
「现在打新大概能赚多少」的动态基准**，把常数变成随市场变动的实测量。

## 实证结论（2026-10-01，21 只已上市样本，算完就写死进注释）

首日涨幅分布（相对发行价）：

    最低  +33.2%      P25  +101.4%     中位  +196.7%
    P75   +331.4%     最高  +742.4%
    **破发 0 只 = 0.0%**      涨幅 < 20% 的也是 0 只
    分板块：科创板中位 +237.5% > 非科创板 +179.2% > 北交所 +149.3%

这个 0 破发率是关键洞察：A 股新股在**注册制 + 网上申购热度高**的环境下，
首日破发极少见。所以「破发风险」不该被当成打新的主要风险 ——
真正的风险是**中签率低导致期望收益薄**，以及**大盘股/发行量大**摊薄收益。

## 发行估值溢价 vs 首日涨幅：方向和直觉相反

`issue_pe / industry_pe` 与首日涨幅的相关系数是 **-0.335**（n=20）：
**溢价倍数越高，首日涨幅越大**（高溢价组中位 237.5% vs 低溢价组 206.6%）。

直觉会以为「发行估值高 = 泡沫 = 破发风险」，但 A 股新股是**稀缺品**：
定价贵说明一二级市场都认可，愿意买的人多，上市自然涨得猛。
所以溢价倍数是**正向因子**，不能当风险项用。

⚠️ 但样本只有 20 条，相关系数 -0.335 也可能只是噪声。所以
`valuation_signal()` 返回的 `confidence` 会如实带上样本量，
样本 < 10 时明确标 low_confidence，不假装这个因子很可靠。

## 因子清单（全部可回溯到具体数字，不含拍脑袋默认值）

| 因子 | 数据来源 | 说明 |
|---|---|---|
| `median_gain` | 已上市新股首日涨幅 | 按板块分组，样本少退化到全市场 |
| `broke_rate` | 同上，首日涨幅 < 0 的比例 | 当前 0%，但会随市场变 |
| `valuation_signal` | issue_pe / industry_pe | **正向**因子（见上） |
| `issue_scale` | 发行总数 / 网上发行 | 巨无霸摊薄收益（长鑫 668 亿股） |
| `lot_rate` | 中签率 | 直接决定期望值，越低越没意义 |
| `sentiment` | `sa_sector_daily` | 涨停数、市场宽度（赚钱效应） |
| `momentum` | 板块近 N 日涨幅 | 从快照表算 |

情绪与动量数据来自本项目已有的 `sa_sector_daily`，**不新增外部依赖**。
"""

from __future__ import annotations

import json
import math
import os
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "data" / "ipo_market.json"
CACHE_TTL_HOURS = 12          # 情绪数据按交易日更新，一天算一次足够


# ==========================================================================
# ① 破发率与涨幅基准（核心：把常数换成实测量）
# ==========================================================================

def gain_stats(samples: list[dict], *, board: str = "",
               recent_n: int = 60) -> dict:
    """已上市新股的首日涨幅统计（相对发行价）。

    `recent_n`：只用最近 N 只 —— 2020 年和 2026 年的新股市场完全不同，
    用全历史平均会把早就过去的行情混进来。

    返回的 `source` 会写清是「n 只近期样本」还是「样本不足已退化」，
    调用方据此决定要不要在界面上标注不确定性。
    """
    rows = []
    for s in (samples or []):
        if s.get("market", "A") != "A":
            continue                      # 港股机制不同（无统一摇号配售）
        try:
            p, c = float(s["price"]), float(s["first_day_close"])
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        lst = (s.get("steps") or {}).get("上市日") or {}
        d = lst.get("date") or ""
        rows.append({"gain": c / p - 1.0, "board": s.get("board") or "",
                     "name": s.get("name"), "list_date": d[:10]})

    # 按上市日排序取最近 N 只（没有上市日就按原序，样本少时无所谓）
    rows.sort(key=lambda r: r["list_date"] or "")
    rows = rows[-recent_n:] if rows else []

    def summarize(rs, label):
        if not rs:
            return None
        gs = sorted(r["gain"] for r in rs)
        n = len(gs)
        broke = sum(1 for g in gs if g < 0)
        weak = sum(1 for g in gs if g < 0.2)
        return {
            "n": n, "label": label,
            "median": round(statistics.median(gs), 4),
            "mean": round(sum(gs) / n, 4),
            "p25": round(gs[max(0, n // 4)], 4),
            "p75": round(gs[min(n - 1, n * 3 // 4)], 4),
            "min": round(gs[0], 4), "max": round(gs[-1], 4),
            "broke_rate": round(broke / n, 4),
            "weak_rate": round(weak / n, 4),
        }

    out = {"board": board or "全市场", "all": summarize(rows, "全市场")}
    if board:
        bs = summarize([r for r in rows if r["board"] == board], board)
        out["board_stats"] = bs
    # 主口径：同板块样本够就用它，不够退化到全市场（并如实标注）
    primary = out.get("board_stats") if board else None
    if primary and primary["n"] >= 8:
        out["primary"] = primary
        out["primary_source"] = f"{board} {primary['n']} 只"
    else:
        out["primary"] = out["all"]
        alln = (out["all"] or {}).get("n", 0)
        if not alln:
            # 一个已上市样本都没有（samples 为空 / 全缺 price、first_day_close）。
            # 原来这里直接 `out['all']['n']` 对 None 取下标 → TypeError 把整个
            # 调用链崩掉；下游 market_context / digest_for_llm 本来就写了
            # `g.get("primary") or {}`，说明「primary 为 None」是**预期状态**，
            # 只有这一行没跟上。改成如实报 insufficient，而不是抛异常。
            out["primary_source"] = "无样本"
            out["insufficient"] = True
            out["why"] = ("没有可用的已上市新股样本（缺 price/first_day_close 或列表为空），"
                          "给不出历史涨幅基准与破发率")
        else:
            out["primary_source"] = (f"全市场 {alln} 只"
                                     + (f"（{board} 样本仅 "
                                        f"{(out.get('board_stats') or {}).get('n', 0)} 只，已退化）"
                                        if board else ""))
    out["low_confidence"] = (out["primary"] or {}).get("n", 0) < 10
    return out


def valuation_signal(sample: dict, *, peers: list[dict] | None = None) -> dict:
    """发行估值溢价（issue_pe / industry_pe）—— **正向因子**。

    实测相关系数 -0.335（n=20，2026-10-01）：溢价越高首日涨得越多，
    与直觉相反。原因是 A 股新股是稀缺品，定价贵本身说明需求旺。
    这里返回的是**信号强度**（0~1），不是「风险分」。
    """
    try:
        ip = float(sample.get("issue_pe"))
        npe = float(sample.get("industry_pe"))
    except (TypeError, ValueError):
        return {"available": False, "why": "缺发行 PE / 行业 PE",
                "ratio": None, "signal": None, "confidence": "none"}
    if not npe or npe <= 0 or ip <= 0:
        return {"available": False, "why": "行业 PE 无效", "ratio": None,
                "signal": None, "confidence": "none"}
    ratio = ip / npe
    # ⚠️ 方向来自实测，不是直觉：溢价倍数 vs 首日涨幅的相关系数 **-0.335**（n=20），
    # 即**溢价越高，首日涨幅越大**（高溢价组中位 237.5% > 低溢价组 206.6%）。
    # 直觉会以为「发行估值高 = 泡沫 = 破发风险」，但 A 股新股是**稀缺品**：
    # 定价贵本身说明一二级市场都认可，愿意买的人多。
    # 所以这里按「正向」映射 —— 早先写成反向（低溢价给高分）是错的，
    # 是被 `test_ipo_llm.py` 第③段的断言抓出来的。
    if ratio < 0.6:
        band, signal = "定价保守(<0.6x)", 0.6
    elif ratio < 1.0:
        band, signal = "与行业持平(0.6~1.0x)", 0.7
    elif ratio < 2.0:
        band, signal = "高于行业(1~2x)", 0.85
    else:
        band, signal = "大幅溢价(>2x)", 0.95
    return {"available": True, "ratio": round(ratio, 2), "band": band,
            "signal": signal,
            "issue_pe": ip, "industry_pe": npe,
            "corr_observed": -0.335, "peers_n": 20,
            "confidence": "low",
            "note": "实测**正向**因子（溢价高→首日涨幅大，因稀缺性）；"
                    "样本仅 20 条，方向可参考、幅度别当真"}


def issue_scale_signal(sample: dict) -> dict:
    """发行规模对收益的摊薄。巨无霸会分流资金、长鑫就是例子。"""
    shares = sample.get("shares_wan")        # 万股
    online = sample.get("online_wan")
    try:
        shares = float(shares) if shares is not None else None
    except (TypeError, ValueError):
        shares = None
    v = shares or online
    if v is None:
        return {"available": False, "why": "缺发行规模"}
    # 单位是万股：1 亿股 = 10000 万股
    yi = v / 10000.0
    if yi >= 30:
        band, drag = "巨无霸(≥30亿股)", 0.35
    elif yi >= 10:
        band, drag = "大盘(10~30亿股)", 0.6
    elif yi >= 3:
        band, drag = "中等(3~10亿股)", 0.85
    else:
        band, drag = "小盘(<3亿股)", 1.0
    return {"available": True, "yi_shares": round(yi, 1), "band": band,
            "multiplier": drag,
            "note": "规模越大，同等资金能中到的签数越少（资金被摊薄）"}


# ==========================================================================
# ② 市场情绪与热度（读本项目已有的 sa_sector_daily）
# ==========================================================================

def sentiment(conn, lookback_days: int = 5) -> dict:
    """赚钱效应：涨停数趋势 + 市场宽度。数据来自 sa_sector_daily。

    为什么用它：打新意愿与赚钱效应强相关 —— 涨停多、上涨家数占比高时，
    新股上市首日更容易被追捧。反过来，亏钱效应重的市场里新股也差。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT snap_date, zt_total, zt_sum_zbc, breadth "
                    "FROM sa_sector_daily ORDER BY snap_date DESC LIMIT %s",
                    (lookback_days,))
        rows = cur.fetchall()
    if not rows:
        return {"available": False, "why": "sa_sector_daily 无数据"}
    rows = list(reversed(rows))
    zt = [int(r[1] or 0) for r in rows]
    widths = []
    for r in rows:
        b = r[3]
        if isinstance(b, dict):
            up, dn = b.get("up"), b.get("down")
            if up and dn and (up + dn) > 0:
                widths.append(round(up / (up + dn) * 100, 1))
    latest = rows[-1]
    latest_w = widths[-1] if widths else None
    out = {
        "available": True,
        "as_of": latest[0].isoformat() if hasattr(latest[0], "isoformat") else str(latest[0]),
        "zt_total": zt[-1],
        "zt_trend": (zt[-1] - zt[0]) if len(zt) > 1 else 0,
        "zt_avg": round(sum(zt) / len(zt)),
        "breadth_up_pct": latest_w,
        "breadth_series": widths,
        "points": len(rows),
    }
    # 情绪档位（阈值参照实测分布，不是一般化的「<30 就差」）
    if out["breadth_up_pct"] is None:
        out["band"], out["multiplier"] = "宽度未知", 0.8
    elif out["breadth_up_pct"] >= 65:
        out["band"], out["multiplier"] = "亢奋(上涨家数≥65%)", 1.15
    elif out["breadth_up_pct"] >= 55:
        out["band"], out["multiplier"] = "偏暖(55~65%)", 1.05
    elif out["breadth_up_pct"] >= 45:
        out["band"], out["multiplier"] = "中性(45~55%)", 1.0
    elif out["breadth_up_pct"] >= 35:
        out["band"], out["multiplier"] = "偏冷(35~45%)", 0.9
    else:
        out["band"], out["multiplier"] = "低迷(<35%)", 0.75
    return out


# ==========================================================================
# ③ 综合：把动态基准算出来
# ==========================================================================

def market_context(conn, samples: list[dict], *, board: str = "") -> dict:
    """打新的当期市场基准：涨幅中位数（动态）× 情绪（动态）× 样本可信度。"""
    gs = gain_stats(samples, board=board)
    st = sentiment(conn)
    primary = gs.get("primary") or {}
    base_gain = primary.get("median")
    mult = st.get("multiplier", 1.0) if st.get("available") else 1.0
    adj = base_gain * mult if base_gain is not None else None
    out = {
        "gain_stats": gs,
        "sentiment": st,
        "base_median_gain": base_gain,
        "sentiment_multiplier": mult,
        "adjusted_gain": round(adj, 4) if adj is not None else None,
        "adjusted_note": (
            "涨幅中位数 %.0f%% × 情绪系数 %.2f = 调整后 %.0f%%"
            % (base_gain * 100, mult, adj * 100)
            if adj is not None else "无样本，无法给出基准"),
        "low_confidence": gs.get("low_confidence") or not st.get("available"),
    }
    return out


def cached_context(conn, samples: list[dict], *, board: str = "",
                   ttl_hours: int = CACHE_TTL_HOURS) -> dict:
    """带缓存的 market_context（同一天情绪数据不变，LLM 调用更贵所以也要缓存）。"""
    key = board or "all"
    try:
        c = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        hit = (c.get("items") or {}).get(key)
        if hit and time_now() - float(hit.get("ts") or 0) < ttl_hours * 3600:
            return hit["data"]
    except (OSError, ValueError, KeyError):
        pass
    data = market_context(conn, samples, board=board)
    try:
        c = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    except (OSError, ValueError):
        c = {}
    c.setdefault("items", {})[key] = {"ts": time_now(), "data": data}
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    return data


def time_now() -> float:
    import time
    return time.time()


# ==========================================================================
# ④ markdown（喂给 LLM 的输入材料）
# ==========================================================================

def digest_for_llm(conn, samples: list[dict], *, board: str = "") -> str:
    """把市场上下文压成一段 markdown，供 LLM 判断「这只新股值不值得为它挪仓」。

    设计原则：**只给事实和数字，不下结论**。判断交给 LLM —— 因为
    「DRAM 龙头在存储涨价周期里意味着什么」这种事，规则算不出来，
    而「中位涨幅 196%、破发率 0%」这种数字 LLM 也算不明白。
    """
    ctx = market_context(conn, samples, board=board)
    g = ctx["gain_stats"]
    prim = g.get("primary") or {}
    s = ctx["sentiment"]
    lines = ["## 打新市场环境（自动采集，请勿臆测未列出的数字）", ""]

    lines.append("### 近期新股首日表现（相对发行价）")
    if prim:
        lines.append(f"- 样本：**{prim['n']} 只**（{g.get('primary_source','')}）")
        lines.append(f"- 中位涨幅 **{prim['median']*100:+.0f}%**，"
                     f"P25 {prim['p25']*100:+.0f}% / P75 {prim['p75']*100:+.0f}%")
        lines.append(f"- 区间 {prim['min']*100:+.0f}% ~ {prim['max']*100:+.0f}%")
        lines.append(f"- **破发率 {prim['broke_rate']*100:.1f}%**，"
                     f"涨幅不足 20% 的比例 {prim['weak_rate']*100:.1f}%")
    else:
        lines.append("- ⚠️ 无已上市样本，无法给出历史涨幅基准")
    bs = g.get("board_stats")
    if bs and bs.get("n"):
        lines.append(f"- {g['board']} 子样本 {bs['n']} 只，"
                     f"中位 {bs['median']*100:+.0f}%，破发 {bs['broke_rate']*100:.0f}%")
    allb = (g.get("all") or {})
    if allb and board:
        lines.append(f"- 对比：全市场 {allb['n']} 只，中位 {allb['median']*100:+.0f}%")

    lines.append("")
    lines.append("### 市场赚钱效应")
    if s.get("available"):
        lines.append(f"- 数据日 {s['as_of']}（近 {s['points']} 个交易日）")
        lines.append(f"- 涨停家数 {s['zt_total']}（区间变化 {s['zt_trend']:+d}，均值 {s['zt_avg']}）")
        if s.get("breadth_up_pct") is not None:
            lines.append(f"- 上涨家数占比 **{s['breadth_up_pct']}%** → {s['band']}")
        lines.append(f"- 情绪系数 **{s['multiplier']:.2f}**（用于折算新股涨幅）")
    else:
        lines.append(f"- ⚠️ 取不到：{s.get('why')}")

    lines.append("")
    lines.append("### 综合基准")
    lines.append(f"- {ctx['adjusted_note']}")
    if ctx.get("low_confidence"):
        lines.append("- ⚠️ **样本量或情绪数据不足，以上数字参考价值有限**")

    lines.append("")
    lines.append("### 必须遵守的规则（判断时不要违背）")
    lines.append("- 网上申购**只认本市场市值**：沪市新股只算沪市非限售A股市值，"
                 "深市/北交所/基金/债券/现金都不计入")
    lines.append(f"- 额度按 **T-2 日前 {20} 个交易日日均市值**定格，"
                 "所以最早 T-2 就要建好底仓，不是 T-1")
    lines.append("- 期望收益 = 配号数 × 中签率 × 每签盈利；"
                 "中签率极低，市值不够 = 拿不到资格，不是「少赚一点」")
    lines.append("- 底仓唯一职责是**占住市值等打新**：不求涨，但不能跌太多")
    return "\n".join(lines)


if __name__ == "__main__":      # python ipo_market.py
    import ipo_calendar
    import psycopg2
    env = {}
    for line in open(r"D:\correct_your_life\.env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("DB_") and "=" in line:
            k, v = line.strip().split("=", 1)
            env[k] = v
    c = psycopg2.connect(host=env["DB_HOST"], port=env["DB_PORT"],
                         user=env["DB_USERNAME"], password=env["DB_PASSWORD"],
                         dbname=env["DB_DATABASE"], connect_timeout=25)
    ss = ipo_calendar._load().get("items") or []
    print(json.dumps(gain_stats(ss), ensure_ascii=False, indent=1))
    print(json.dumps(sentiment(c), ensure_ascii=False, indent=1))
    print(digest_for_llm(c, ss, board="科创板"))
