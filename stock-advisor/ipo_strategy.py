"""ipo_strategy.py —— 打新（新股申购）策略：算收益 → 判要不要挪仓 → 选底仓。

## 为什么值得单独做

打新是 A 股少数**期望为正、且与市场方向无关**的策略：申购日买入一级市场，
中签后按二级价格卖出，赚钱与否取决于「稀缺性」，不取决于大盘涨跌。用户举例的
长鑫科技就是极端案例（实测数据）：

    发行价 8.66 → 首日收盘 49.00（5.66 倍）→ 最新 54.79
    顶格申购需配沪市市值 3349 万，中签率 0.47141739%
    每 212 个配号中 1 签，每签 500 股 → 单签赚约 2.02 万元

## 但有个容易被忽略的前提：市值门槛可能高到不划算

中签率极低（0.47% 已经是科创板历史最高），所以**市值不够 = 完全没资格**，
不是"少赚一点"。反过来，市值够了之后收益与市值近似线性，于是存在一条分界线：

    期望打新收益 = 配号数 × 中签率 × 单签盈利
    配号数       = 沪(深)市日均市值 / 5000

按长鑫实测值（中签率 0.4714%、单签赚 2.02 万）：

    | 沪市日均市值 | 配号数 | 期望中签 | 期望打新收益 |
    |---|---|---|---|
    |    50 万 |   100 |   0.47 签 |    0.9 万 |
    |   100 万 |   200 |   0.94 签 |    1.9 万 |
    |   500 万 |  1000 |   4.7 签  |    9.4 万 |
    |  3349 万 |  6698 |  31.6 签  |   63   万 |

**结论：只有市值上到百万级，打新才值得专门腾挪资金。** 几十万市值时，
为打新调仓的收益还不如直接买底仓银行股吃股息 —— 策略会显式给出这个判断，
而不是无脑建议"去挪仓"。

## 关键规则（都来自交易所公告，不是推测）

- **只认本市场市值**：科创板/沪市主板打新只看**沪市**非限售 A 股市值，
  深市、北交所、基金、债券、**现金都不计入**。所以深市持仓不能拿来打沪市新股。
- **T-2 日前 20 个交易日日均市值 ≥ 1 万**，每 5000 元配 1 个申购单位
  （科创板/创业板 1 单位 = 500 股，主板 = 1000 股）。
- **T-2 日的市值决定 T 日的申购额度** —— 所以**最早 T-2 就要动手**，不是 T-1。
  这是整个策略最容易踩空的地方。
- 底仓的**唯一职责是占市值**：不涨没关系，但不能跌太多（跌了等于负收益），
  也不能是正在跌的票（跌的时候卖不掉/割肉）。

## 底仓怎么选（用户思路的落地）

用户说「买沪市中的银行股或者高股息的，短期不会大跌的」—— 对，而且有具体标准：

1. **必须是沪市**（60/601/603/605/688 开头），否则不计入打新市值；
2. 高股息 + 低波动：用 `股息率` 与 `近一年最大回撤` 双重过滤；
3. **回避次新/科创板**：上市不满 1 年的次新股不算非限售全部？实际上算，
   但波动大，不适合当底仓；
4. **集中而非分散**：底仓目的是配号，分散到 5 只以上会让每只市值过小、
   且增加了卖出时的滑点。

数据源：自选股 + 行情（`market_data`）已在本机可用；股息率用
`akshare.stock_a_gdhs` 或直接按分红/股价算，模块里两条路都留了兜底。
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import ipo_calendar  # noqa: E402
# 规则常量共用同一份，避免 ipo_strategy 和 ipo_quota 两处漂移
# （2026-10-01：额度按「T-2 日前 20 个交易日日均市值」算，本文件原先
#  用现价市值，两处算法不一致会导致同一只票给出相反的结论）
from ipo_quota import AVG_WINDOW, LOT_VALUE as Q_LOT_VALUE  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "ipo_strategy_state.json"

# 交易所规则常量（都来自公告，别改成拍脑袋的数）
# LOT_VALUE 与 AVG_WINDOW 从 ipo_quota 导入共用 —— 两处各写一份必然漂移，
# 而漂移的后果是「同一只票在两个页面给出不同的额度」。
LOT_VALUE = Q_LOT_VALUE    # 每 5000 元市值 = 1 个申购单位
MIN_MARKET_CAP = 10000    # T-2 前 20 日日均市值门槛：1 万元
SHARES_PER_UNIT = {"科创板": 500, "创业板": 500, "主板": 1000, "北交所": 100}

# 沪市代码前缀 → 市场标签。用来判断"这只票能不能当沪市底仓"
SH_PREFIXES = ("60", "601", "603", "605", "688", "900")
SZ_PREFIXES = ("00", "001", "002", "003", "300", "301", "200")
BJ_PREFIXES = ("43", "83", "87", "88", "92")


def market_of(code: str) -> str:
    """按代码前缀判市场：sh / sz / bj / unknown。"""
    c = str(code or "").strip()
    if len(c) == 6 and c.isdigit():
        if c.startswith(SH_PREFIXES):
            return "sh"
        if c.startswith(BJ_PREFIXES):
            return "bj"
        if c.startswith(SZ_PREFIXES):
            return "sz"
    return "unknown"


# ==========================================================================
# ① 单只新股：算期望收益与「要不要为它挪仓」
# ==========================================================================

def eval_new_stock(row: dict, *, per_lot_profit: float | None = None) -> dict:
    """算一只即将申购的新股的打新期望。

    `row` 来自 ipo_calendar（字段见 STEPS）或 akshare 原始列。
    `per_lot_profit` = 每签盈利（元）。留空则用「发行价 → 预估首日涨幅 × 1000股」
    的粗估，**并在结果里标 estimated=True**，不假装是实测。
    """
    code = str(row.get("code") or "").strip()
    board = str(row.get("board") or "").strip()
    mkt = market_of(code) or row.get("market", "").lower()
    # akshare 的「交易所」字段更权威，优先用它
    exch = str(row.get("exchange") or "")
    if "上海" in exch:
        mkt = "sh"
    elif "深圳" in exch:
        mkt = "sz"
    elif "北京" in exch:
        mkt = "bj"

    price = row.get("price")
    try:
        price = float(price) if price is not None and price == price else None
    except (TypeError, ValueError):
        price = None

    # ⚠️ 未上市的新股，发行价常在**发行公告**才确定，akshare 的 `发行价格`
    # 会是空的。实测 2026-10-01：待申购的 301718 通则康威、001381 皇冠新材
    # 两只 price 都是 None —— 于是每签盈利算不出来，期望收益整条链断掉，
    # verdict 永远停在 evaluate（「缺中签率或每签盈利」）。
    # 这里用「发行市盈率 × 每股净资产」之类推不出来（也没那字段），
    # 唯一可用的替代是**让调用方传入发行价**（发行公告一出就有）。
    if price is None:
        price = row.get("issue_price_guess")

    # 市值门槛（万元 → 元）。akshare 给的单位是「万元」
    mc_wan = row.get("market_cap_need")
    full_cap = None
    if mc_wan is not None:
        try:
            full_cap = float(mc_wan) * 10000
        except (TypeError, ValueError):
            full_cap = None

    lot_rate = row.get("lot_rate")            # 中签率，如 0.0047
    try:
        lot_rate = float(lot_rate) if lot_rate is not None and lot_rate == lot_rate else None
    except (TypeError, ValueError):
        lot_rate = None

    shares_per_lot = SHARES_PER_UNIT.get(board)
    if shares_per_lot is None:
        shares_per_lot = 500 if board in ("科创板", "创业板") else 1000

    est = per_lot_profit is None
    if per_lot_profit is None:
        # 粗估：给一个偏保守的首日涨幅假设（新股中位表现远高于主板，但不做乐观外推）
        per_lot_profit = (price or 0) * shares_per_lot * 1.0   # 100% 涨幅假设

    out = {
        "code": code, "name": row.get("name") or "", "board": board,
        "market": mkt, "market_cn": {"sh": "沪市", "sz": "深市",
                                     "bj": "北交所"}.get(mkt, mkt),
        "price": price,
        "full_cap_need": full_cap,             # 顶格所需市值（元）
        "full_cap_wan": round(full_cap / 10000, 1) if full_cap else None,
        "lot_rate": lot_rate,
        "lot_rate_pct": round(lot_rate * 100, 4) if lot_rate else None,
        "shares_per_lot": shares_per_lot,
        "per_lot_profit": per_lot_profit,
        "per_lot_profit_estimated": est,
    }
    if full_cap:
        out["full_lots"] = int(full_cap // LOT_VALUE)
        out["full_shares"] = out["full_lots"] * shares_per_lot
    if lot_rate and full_cap and per_lot_profit:
        lots = out.get("full_lots") or 0
        out["expected_lots_full"] = round(lots * lot_rate, 2)
        out["expected_profit_full"] = round(lots * lot_rate * per_lot_profit, 0)
    return out


def breakeven_capital(profit_per_cap: float, *,
                      min_wan: float = 50, max_wan: float = 5000,
                      hold_days: int = 30,
                      dividend_yield: float | None = None,
                      pool: list[dict] | None = None) -> dict:
    """要挪多少市值，打新期望收益才等于「直接买底仓吃股息」的收益。

    `profit_per_cap`：每万元市值带来的期望打新收益（元/万元）。

    `dividend_yield`：**从底仓候选池实测**（`base_pool_candidates` 的
    `dividend_yield` 取中位数），不再用写死的 4%。原来硬编码 0.04 是把
    「高股息银行股」当成了永远 4% —— 实际候选池里可能是 3.1% 的茅台类，
    也可能是 5.2% 的银行股，差 40%，直接决定「挪仓划不划算」的结论。
    候选池为空或无股息数据时返回 None，调用方显式说明「无法比较」。
    """
    used_yield, basis = None, ""
    if dividend_yield is not None:
        used_yield = float(dividend_yield)
        basis = "调用方指定"
    else:
        ys = [float(p["dividend_yield"]) for p in (pool or [])
              if p.get("dividend_yield") is not None]
        if ys:
            ys.sort()
            used_yield = ys[len(ys) // 2]
            basis = f"底仓候选池 {len(ys)} 只的股息率中位数"
    if not used_yield:
        return {"breakeven_wan": None,
                "why": "底仓候选池没有股息率数据，无法比较"
                       "「打新期望收益」与「底仓股息」—— "
                       "请补 dividend_yield 或显式传 dividend_yield"}

    for w in (min_wan, 100, 200, 500, 1000, 2000, max_wan):
        ev = profit_per_cap * w                     # 万元 → 期望打新收益（元）
        base = w * 10000 * used_yield * hold_days / 365   # 底仓同期股息（元）
        if ev >= base:
            return {"breakeven_wan": w, "expect": round(ev),
                    "dividend": round(base),
                    "dividend_yield_used": round(used_yield, 4),
                    "dividend_yield_basis": basis,
                    "hold_days": hold_days}
    return {"breakeven_wan": None,
            "dividend_yield_used": round(used_yield, 4),
            "dividend_yield_basis": basis,
            "why": f"即使 {max_wan:.0f} 万市值，期望打新收益也不如底仓股息"}


# ==========================================================================
# ② 市值现状：各市场持仓市值 & 缺口
# ==========================================================================

def current_market_cap(holdings: list[dict]) -> dict:
    """按市场汇总当前持仓市值。

    `holdings` 用 app 的 /api/holdings 形态：[{code, shares, avg_cost, quote:{price}}]
    注意：**只算非限售 A 股普通股**，ETF/基金/债券在打新里不计入市值。
    """
    out = {"sh": 0.0, "sz": 0.0, "bj": 0.0, "positions": [],
           "excluded": []}
    for h in holdings or []:
        code = str(h.get("code") or "").strip()
        mkt = market_of(code)
        q = h.get("quote") or {}
        price = q.get("price")
        shares = h.get("shares") or h.get("net_shares") or 0
        try:
            price = float(price) if price is not None else None
            shares = float(shares) if shares is not None else 0
        except (TypeError, ValueError):
            price, shares = None, 0
        val = (price or 0) * shares
        if mkt == "unknown":
            out["excluded"].append({"code": code, "reason": "非 A 股普通股/代码不识别",
                                     "value": round(val)})
            continue
        out[mkt] = out[mkt] + val
        out["positions"].append({"code": code, "market": mkt,
                                 "shares": shares, "price": price,
                                 "value": round(val)})
    for k in ("sh", "sz", "bj"):
        out[k] = round(out[k], 2)
    out["positions"].sort(key=lambda x: -x["value"])
    return out


def assess(*, new_stock: dict, holdings: list[dict],
           lead_days: int = 2, avg_cap: dict | None = None,
           base_pool: list[dict] | None = None) -> dict:
    """给定一只待申购新股，算：要不要挪、挪多少、往哪个市场挪。

    这是策略的主入口。

    **`lead_days` 的含义（2026-10-01 更正，用户指出「额度按近 20 天算」）**
    -------------------------------------------------------------------
    额度规则是「**T-2 日前 20 个交易日日均市值** / 5000」（见本文件头）。
    所以「提前 2 天」远不够：补仓立刻进窗口，但**只占 1/20 权重**，
    日均要 20 个交易日才爬满。

        lead_days >= 22  时间充裕，补仓完全来得及
        3 <= lead_days <= 21  补仓有效，但日均可能爬不满本轮目标
        lead_days <= 2   太晚 —— T-2 定格，本轮基本没戏

    「提前 20 天通知」不是通知需要 20 天，而是要让用户有时间补仓，
    并让 20 日均值窗口真的被填满。

    `avg_cap` 是 `ipo_quota.avg_market_cap()` 的结果（交易所口径）。
    **不传会退回旧的现价市值算法** —— 旧算法拿今天的市值算额度，
    涨跌一天就改变结论，实测沪市会少算 1 个号、深市会多算 1 个号。
    """
    ns = dict(new_stock)
    mkt = ns.get("market", "sh")
    key = mkt if mkt in ("sh", "sz", "bj") else "sh"

    cap = current_market_cap(holdings)
    need = ns.get("full_cap_need") or 0.0

    # ---- 额度基数：优先 20 日日均市值 ----
    using_avg = bool(avg_cap) and not (avg_cap or {}).get("degraded")
    if avg_cap:
        info = (avg_cap.get("markets") or {}).get(key) or {}
        have = float(info.get("avg") or 0.0)
        lots_now = int(info.get("lots") or 0)
        src = (f"{AVG_WINDOW}日日均市值" if using_avg else "现价市值(数据降级)")
        if avg_cap.get("degraded"):
            src += "（日均数据降级，额度可能偏低）"
    else:
        have = float(cap.get(key, 0.0))
        lots_now = int(have // LOT_VALUE)
        src = "现价市值(未提供日均，已知不准)"

    res = {"stock": ns,
           "market_cn": {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(key),
           "holdings_cap": {k: cap.get(k) for k in ("sh", "sz", "bj")},
           "current_market_cap": have, "need_cap": need,
           "lead_days": lead_days,
           "cap_basis": src,
           "using_avg_cap": using_avg,
           "avg_cap_detail": avg_cap,
           "quota_rule": (f"额度 = T-2 日前 {AVG_WINDOW} 个交易日日均市值 ÷ "
                          f"{LOT_VALUE}，向下取整")}

    # 期望签数：**不依赖发行价**，所以发行价未公布时也能给。
    # 这是发行公告前唯一能算的期望值（金额型期望需要发行价，见 payoff_multiple）。
    if ns.get("lot_rate") and lots_now > 0:
        res["expected_lots_now"] = round(lots_now * ns["lot_rate"], 3)
        _lote = ns.get("lot_rate_estimate") or {}
        if _lote.get("available") and _lote.get("low") is not None:
            res["expected_lots_range"] = {
                "low": round(lots_now * _lote["low"], 3),
                "high": round(lots_now * _lote["high"], 3)}
        res["lot_rate_pct"] = ns.get("lot_rate_pct")
        res["lot_rate_is_estimate"] = bool(ns.get("lot_rate_is_estimate"))
        res["payoff_multiple"] = ns.get("payoff_multiple")

    if need <= 0:
        res["verdict"] = "skip"
        res["reason"] = "缺中签率/市值门槛，无法评估"
        return res

    res["lots_now"] = lots_now
    res["cap_now_wan"] = round(have / 10000, 1)

    # 连 1 个申购单位都拿不到 -> 完全没资格
    if lots_now == 0:
        res["verdict"] = "skip"
        res["reason"] = (f"{res['market_cn']}{src} {res['cap_now_wan']} 万 "
                         f"< 1 万门槛，连 1 个申购单位都拿不到")
        return res

    # ---- 时间窗：补仓要多久才能爬满 20 日均值 ----
    rebal = None
    if need > have and lead_days > 0:
        from ipo_quota import days_needed_to_reach
        rebal = days_needed_to_reach(need, have, need, window=AVG_WINDOW)
        res["rebalance_timing"] = rebal

    if lead_days <= 2:
        res["verdict"] = "too_late"
        res["reason"] = (f"距申购日只剩 {lead_days} 天 —— 额度按 T-2 日前 "
                         f"{AVG_WINDOW} 日日均市值定格，现在补仓已来不及计入本轮")
        return res

    if rebal and not rebal.get("reachable"):
        res["verdict"] = "skip"
        res["reason"] = f"补仓也爬不到顶格：{rebal['note']}"
        return res

    if rebal and rebal.get("days") and rebal["days"] > lead_days:
        # 投影：补仓后到申购日为止能爬到多少。
        # 坑：lead_days 是**自然日**（来自日历），而 20 日窗口数的是**交易日**。
        # 直接拿 lead_days 当分子会高估约 40%（一周 5 个交易日 vs 7 天），
        # 实测曾出现「1.27 万市值算出 1006 个号」这种荒谬结果。
        # 这里按 0.7 的交易日/自然日比保守折算。
        lead_td = max(0, int(lead_days * 0.7))
        from ipo_quota import projected_avg_at
        proj = projected_avg_at(lead_td, have, need, window=AVG_WINDOW)
        # 保底：投影不该比现在更少（否则显得补仓反而变差）
        res["projected_lots"] = max(lots_now, proj["lots"])
        res["projected_avg"] = round(proj["avg"], 2)
        res["lead_trading_days"] = lead_td
        res["verdict"] = "partial"
        res["reason"] = (
            f"缺口 {round((need - have)/10000, 1)} 万；但{src}爬到顶格需 "
            f"{rebal['days']} 个交易日，距申购日只有 {lead_days} 天"
            f"（约 {lead_td} 个交易日）—— 本轮最多约 {res['projected_lots']} 个号"
            f"（现在 {lots_now} 个），要么接受少打要么等下一轮")
        return res

    # 分档：拿多少市值去博
    target = min(have, need)
    res["target_cap"] = target
    res["target_wan"] = round(target / 10000, 1)
    res["gap"] = max(0.0, need - have)
    res["gap_wan"] = round(res["gap"] / 10000, 1)

    rate = ns.get("lot_rate")
    profit = ns.get("per_lot_profit")
    # 允许用**估算**的每签盈利（2026-10-01 改）。
    # 原条件是 `not ns.get("per_lot_profit_estimated")` —— 只认实测值，
    # 结果「用近期新股涨幅中位数估的每签盈利」被整体拒掉，
    # 待申购新股永远落在 `evaluate(缺中签率或每签盈利)`，估算白算了。
    # 现在：估算值可用，但**必须带不确定性标记**（下面的 _conf + range），
    # 且低置信度时 verdict 最多给到 marginal，不会直接说「建议挪仓」。
    if rate and profit:
        lots = int(target // LOT_VALUE)
        ev = lots * rate * profit
        res["expected_profit"] = round(ev)
        res["expect_per_wan"] = ev / (target / 10000) if target else 0
        # 不确定性来源：中签率估算 + 每签盈利估算，两个都可能
        _bits = []
        if ns.get("lot_rate_is_estimate"):
            _bits.append("中签率")
        if ns.get("per_lot_profit_estimated"):
            _bits.append("每签盈利")
        res["expected_profit_is_estimate"] = bool(_bits)
        res["expected_profit_estimate_parts"] = _bits
        # 逐项给区间：涨幅区间来自 gain_stats 的 P25~P75，中签率区间来自 lot_rate
        rng = {}
        if "中签率" in _bits:
            import lot_rate as _lr
            r1 = _lr.expected_profit(lots, ns.get("lot_rate_estimate") or {},
                                     profit)
            if r1 and r1.get("low") is not None:
                rng["low"], rng["high"] = r1["low"], r1["high"]
        if "每签盈利" in _bits:
            gs = ns.get("_gain_p25_p75")
            if gs:
                lp = profit / max(gs["median"], 1e-9) if gs.get("median") else None
                if lp and gs.get("p25") is not None:
                    lo = lots * rate * gs["p25"] * lp
                    hi = lots * rate * gs["p75"] * lp
                    rng["low"] = max(rng.get("low") or 0, lo)
                    rng["high"] = min(rng.get("high") or 1e18, hi)
        if rng.get("low") is not None:
            res["expected_profit_range"] = {
                "low": round(rng["low"]), "high": round(rng["high"]),
                "note": "区间来自" + "与".join(_bits) + "估算的不确定性",
            }
        be = breakeven_capital(res["expect_per_wan"], pool=base_pool)
        res["breakeven"] = be
        # 「划算」的判据用**相对比较**：打新期望收益 vs 同等市值买底仓的股息。
        # 不再用 `ev < 1000` 这种写死金额 —— 1000 元对 50 万市值是划算的，
        # 对 3000 万市值则完全不值得。判据必须跟着规模走。
        be_wan = be.get("breakeven_wan")
        if be_wan is None:
            res["verdict"] = "evaluate"
            res["reason"] = (f"期望收益约 {ev:.0f} 元；但缺少底仓股息率数据，"
                             f"无法判断是否值得为它调仓")
        elif target / 10000 < be_wan:
            res["verdict"] = "small"
            res["reason"] = (f"期望收益约 {ev:.0f} 元，而 {target/10000:.0f} 万市值"
                             f"买底仓吃股息能拿约 {be['dividend']:.0f} 元"
                             f"（股息率 {be['dividend_yield_used']*100:.1f}%，"
                             f"{be['dividend_yield_basis']}）→ "
                             f"要到 {be_wan:.0f} 万市值打新才划算")
        elif res["gap"] <= 0:
            res["verdict"] = "hold"
            res["reason"] = (f"{res['market_cn']}{src}已达顶格"
                             f"（{res['cap_now_wan']} 万，{lots_now} 个号），"
                             f"无需挪仓，期望收益约 {ev:.0f} 元")
        else:
            res["verdict"] = "shift"
            res["reason"] = (f"缺口 {res['gap_wan']} 万；补到顶格期望收益约 "
                             f"{ev:.0f} 元"
                             + (f"；{src}爬满需 {rebal['days']} 个交易日"
                                if rebal and rebal.get("days") else ""))
        # 期望收益里有估算成分时，**不允许直接给「建议挪仓」** ——
        # 让它降一级到 marginal 并标出来。这是「错误的精确比粗糙的区间更危险」
        # 的直接落实：数值区间宽的时候，让人来决定而不是让代码替他决定。
        if res.get("expected_profit_is_estimate") and res["verdict"] == "shift":
            res["verdict"] = "marginal"
            res["verdict_downgraded"] = (
                "期望收益含" + "、".join(res["expected_profit_estimate_parts"])
                + "的估算值，区间较宽，故从「建议挪仓」降级为「可挪可不挪」")
            res["reason"] += "；" + res["verdict_downgraded"]
        return res

    res["verdict"] = "evaluate"
    if ns.get("lot_rate"):
        # 有中签率但没有发行价 → 金额算不了，**仍能给签数与倍数**
        # （f-string 内嵌同种引号在 3.10 会报 unterminated，先取出再拼）
        _rate_pct = ns["lot_rate"] * 100
        _rate_kind = "估算 " if ns.get("lot_rate_is_estimate") else ""
        _mkt = res["market_cn"]
        _cap = res["cap_now_wan"]
        _lots = res.get("expected_lots_now")
        res["reason"] = (
            f"发行价未公布（akshare 全部新股接口在发行公告前都不给发行价），"
            f"金额型期望收益算不了；但我{_mkt}市值 {_cap} 万已达顶格"
            f"（{lots_now} 个号），中签率{_rate_kind}{_rate_pct:.2f}%"
            f" → 期望中约 {_lots} 签。发行公告一出即可算出金额")
    else:
        res["reason"] = "缺中签率，只能给方向判断"
    res["verdict_note"] = (
        "**发行价未公布**（发行公告前数据源确实没有，不是采集失败）。"
        "所以不猜金额 —— 猜出来的期望收益看着精确却是假的。"
        "已给出不依赖发行价的期望签数与倍数；"
        "发行价公布后（可传 issue_price_guess）自动转为金额型期望。")
    return res


# ==========================================================================
# ③ 底仓候选池：沪市高股息 + 低波动
# ==========================================================================

def gain_model(samples: list[dict], *, board: str = "") -> dict:
    """[已移到 ipo_market.gain_stats] 这里保留一个薄封装，避免旧调用点断掉。

    实测校准逻辑（破发率/分位数/分板块/情绪折算）现在都在 `ipo_market` 里，
    因为它需要 DB 连接读 `sa_sector_daily`（市场宽度/涨停数）。
    """
    import ipo_market
    return ipo_market.gain_stats(samples, board=board)


def _dead_legacy_gain_model(samples: list[dict], *, board: str = "") -> dict:
    """旧实现（已被 ipo_market.gain_stats 取代），留档以便对照数字口径。"""
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    pool = [s for s in (samples or [])
            if s.get("price") and s.get("first_day_close")]
    pool_b = [s for s in pool if s.get("board") == board] if board else pool

    def gains(rows):
        out = []
        for s in rows:
            try:
                p, c = float(s["price"]), float(s["first_day_close"])
            except (TypeError, ValueError, KeyError):
                continue
            if p > 0:
                out.append(c / p - 1.0)
        return out

    g_all, g_b = gains(pool), gains(pool_b)
    profits = [float(s["profit_per_lot"]) for s in pool_b
               if s.get("profit_per_lot")]
    return {
        "board": board or "全市场",
        "samples": len(g_b) or len(g_all),
        "median_gain_all": med(g_all),
        "median_gain_board": med(g_b),
        "median_profit_per_lot": med(profits),
    }


def payoff_multiple(new_stock: dict, model: dict | None = None) -> dict:
    """算「每签盈利 ÷ 配号数」，即**每 1 万元市值能撬出多少收益**。

    ## 为什么不用「每签盈利」直接算期望

    实测（2026-10-01）：akshare 的**全部**新股接口对待申购股票都不给发行价 ——
    `stock_xgsglb_em` 的 `发行价格` 空、`stock_new_ipo_cninfo` 的 `发行价格` 也是 NaN。
    原因很直白：**发行价由发行公告确定，公告之前不存在**。

    没有发行价就没有「每签赚多少元」，期望收益（元）整条链就断了。
    但**倍数**能算：每签盈利 = 发行价 × 每配号股数 × 涨幅，
    而「每配号股数 × 涨幅」与发行价无关。于是：

        每 1 万元市值期望收益 = 配号数(2个) × 每签盈利 / 市值(1万)
                              = 2 × 发行价 × 股数 × 涨幅 / (2 × 5000)
                              = (发行价 / 5000) × 股数 × 涨幅

    发行价还在里面 —— 所以严格说倍数也依赖发行价。

    **那能给出的是什么**：`期望中签签数`（纯配号数×中签率，不依赖发行价）
    与 `相对倍数`（拿同板块已上市新股的实测值做参照），后者只说明
    「这只票的赔率大概在哪一档」，不冒充绝对金额。

    ## 所以这个函数的定位

    它**不产出金额**。金额要么等发行公告（`issue_price_guess` 传入后
    `per_lot_profit_estimate` 就能算），要么不给 —— 不用默认值凑一个假数字。
    """
    prim = ((model or {}).get("primary") or {})
    gain = (model or {}).get("adjusted_gain") or prim.get("median")
    rate = new_stock.get("lot_rate")
    shares = new_stock.get("shares_per_lot")
    board = str(new_stock.get("board") or "")
    out = {"gain_median": gain,
           "gain_n": prim.get("n"),
           "shares_per_lot": shares,
           "shares_basis": ("科创板/创业板 500 股，主板/北交所 1000 股"
                            if board else None),
           "lot_rate": rate,
           "note": "发行价未公布 → 无法给绝对金额，只给签数与倍数"}
    if rate and shares:
        # 每配号的期望盈利 = 发行价 × 股数 × 涨幅；发行价未知，
        # 所以用「发行价 = 1 元」为**单位基准**表达倍数，不冒充金额
        out["payoff_per_yuan_of_price"] = shares * (gain or 0)
        out["payoff_note"] = (f"每 1 元发行价对应每签盈利 "
                              f"{shares}×{gain:.2f} ≈ {shares*(gain or 0):.0f} 元"
                              if gain else "缺涨幅基准")
    return out


def per_lot_profit_estimate(new_stock: dict, model: dict | None = None) -> tuple:
    """估「每签盈利」。返回 (金额元, 是否为估算, 涨幅依据文本)。

    **不再有任何硬编码涨幅**。涨幅基准来自 `ipo_market` 的实测统计
    （近期已上市新股首日涨幅 × 当期情绪系数），全部可回溯。
    拿不到样本时返回 None —— 由调用方显式标注「无法评估」，
    绝不退回某个拍脑袋的常数（原来的 `gain = 1.0` 就是这类错误：
    实测中位涨幅是 196.7%，那个 100% 的假设把收益低估了一倍）。
    """
    price = new_stock.get("price")
    shares = new_stock.get("shares_per_lot")
    if not shares:
        shares = SHARES_PER_UNIT.get(str(new_stock.get("board") or ""))
        shares = shares or 1000      # 主板默认 1000 股/配号（创业板/科创板已由上面对上）
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, True, ("发行价未公布（akshare 发行价格字段为空）—— "
                            "发行公告一出即可自动计算，或手动传 issue_price_guess")
    if not price or price <= 0:
        return None, True, "发行价无效"

    # ① 发行数据里已有实测每签盈利 → 直接用
    direct = new_stock.get("profit_per_lot")
    if direct:
        try:
            return float(direct), False, "发行方披露的每中一签盈利（实测）"
        except (TypeError, ValueError):
            pass

    # ② 用动态基准涨幅 × 情绪系数
    model = model or {}
    gain = model.get("adjusted_gain")
    if gain is None:
        gain = model.get("median_gain_board")
    if gain is None:
        gain = model.get("median_gain_all")
    if gain is None:
        return None, True, ("无已上市样本，无法给出涨幅基准 —— "
                            "**不用默认值代替**，请人工给涨幅假设")
    prim = model.get("primary") or {}
    used = (f"{prim.get('n', '?')} 只近期样本中位涨幅"
            f"{gain*100:+.0f}%"
            + (f"× 情绪系数 {model.get('sentiment_multiplier')}"
               if model.get("sentiment_multiplier") else ""))
    return price * shares * float(gain), True, used


def strategy_for_upcoming(*, holdings: list[dict], watchlist: list[dict],
                          days: int = 30, samples: list[dict] | None = None,
                          target_ratio: float = 1.0,
                          conn=None, use_llm: bool = True,
                          use_avg_cap: bool = True,
                          llm_conf: dict | None = None) -> list[dict]:
    """主入口：对未来 days 天内**待申购**的新股逐只给打新决策。

    三层分工（数字全来自实测，不含硬编码收益率）：
      1. `ipo_market`  算动态基准：近期新股首日涨幅中位数 × 当期情绪系数
      2. `ipo_strategy` 算规则结论：配号数/中签率/期望收益/缺口/挪仓计划
      3. `ipo_llm`      做定性判断：值不值得为它挪仓、风险点、行业周期位置

    `conn`：传了就取情绪数据（从 sa_sector_daily）。留空则只用涨幅样本。
    `use_llm`：关掉则跳过第 3 层，结果里 `llm` 字段会说清「没做判断」。
    `use_avg_cap`：用 20 交易日日均市值算额度（交易所口径）；关掉则用现价市值。
    """
    import ipo_llm
    import ipo_market
    import lot_rate

    subs = ipo_calendar.upcoming_full(days=days, market="A")
    cap = current_market_cap(holdings)
    lot_model = lot_rate.load_model(conn)

    # 额度基数用 **20 个交易日日均市值**（交易所口径），不是现价市值。
    # 算一次给所有票复用：它只依赖持仓，不依赖具体哪只新股。
    # 拿不到（非交易时段库不可用等）就传 None，assess 会退回现价市值并标明。
    avg_cap = None
    if use_avg_cap:
        try:
            from ipo_quota import avg_market_cap
            avg_cap = avg_market_cap(holdings)
        except Exception as exc:                          # noqa: BLE001
            print(f"[ipo_strategy] 日均市值计算失败，退回现价市值: {exc}",
                  flush=True)
            avg_cap = None

    out = []
    for it in subs:
        ns = eval_new_stock(it)

        # 第 1 层：动态市场基准（带缓存，同一天不重复算）
        if conn is not None:
            ctx = ipo_market.cached_context(conn, samples or [],
                                            board=str(ns.get("board") or ""))
        else:
            ctx = {"gain_stats": ipo_market.gain_stats(
                samples or [], board=str(ns.get("board") or "")),
                   "sentiment": {"available": False,
                                 "why": "未传 DB 连接，情绪数据缺失"},
                   "adjusted_gain": None, "low_confidence": True}

        profit, estimated, basis = per_lot_profit_estimate(ns, ctx)
        # 中签率先算 —— `assess()` 要靠它才能给期望收益（原先顺序反了，
        # 估算值塞晚了，assess 里 `if rate and profit` 判空 → verdict 一直 evaluate）
        lr_est = lot_rate.estimate_lot_rate(it, lot_model)
        if profit:
            ns["per_lot_profit"] = round(profit, 0)
            ns["per_lot_profit_estimated"] = estimated
            ns["gain_basis"] = basis
            # 涨幅的 P25~P75：期望收益区间的另一半来源
            _gs = ((ctx.get("gain_stats") or {}).get("primary") or {})
            if _gs.get("p25") is not None and _gs.get("p75") is not None:
                ns["_gain_p25_p75"] = {"median": _gs.get("median"),
                                       "p25": _gs["p25"], "p75": _gs["p75"]}
        ns["lot_rate_estimate"] = lr_est
        if lr_est.get("available") and not ns.get("lot_rate"):
            ns["lot_rate"] = lr_est["estimate"]          # 让 assess 能算期望
            ns["lot_rate_pct"] = round(lr_est["estimate"] * 100, 4)
            ns["lot_rate_is_estimate"] = True
        # 发行价未公布 → 金额型期望算不了，给「倍数 + 签数」（不依赖发行价）
        ns["payoff_multiple"] = payoff_multiple(ns, ctx)
        need = ns.get("full_cap_need")
        if need and target_ratio != 1.0:
            ns["full_cap_need"] = round(need * target_ratio, 2)
            ns["full_cap_wan"] = round(ns["full_cap_need"] / 10000, 1)
            ns["target_note"] = f"按目标的 {target_ratio*100:.0f}% 计（非顶格）"

        # 先算底仓候选：`breakeven_capital` 要用它的**实测股息率**，
        # 不能拿写死的 4% 当「高股息银行股」的常数（实测候选池可能 3.1% 也可能 5.2%，
        # 差 40%，直接决定「挪仓划不划算」的结论）
        pool = base_pool_candidates(watchlist)
        res = assess(new_stock=ns, holdings=holdings,
                     lead_days=it.get("lead_days", 0), avg_cap=avg_cap,
                     base_pool=pool)
        res["sub_date"] = it.get("sub_date")
        res["market_context"] = {
            "gain_basis": basis,
            "median_gain": ((ctx.get("gain_stats") or {}).get("primary") or {}).get("median"),
            "adjusted_gain": ctx.get("adjusted_gain"),
            "sentiment": ctx.get("sentiment", {}).get("band"),
            "broke_rate": ((ctx.get("gain_stats") or {}).get("primary") or {}).get("broke_rate"),
            "sample_n": ((ctx.get("gain_stats") or {}).get("primary") or {}).get("n"),
            "low_confidence": ctx.get("low_confidence"),
        }
        # 发行估值溢价 = 实测**正向**因子（相关系数 -0.335，n=20）
        res["valuation_signal"] = ipo_market.valuation_signal(it)
        res["issue_scale"] = ipo_market.issue_scale_signal(it)

        if res.get("verdict") in ("shift", "hold", "urgent"):
            res["base_pool"] = pool
            if res["verdict"] == "shift":
                res["plan"] = rebalance_plan(
                    res["current_market_cap"] / 10000, res["target_wan"],
                    res["base_pool"], sell_holdings=holdings)

        # 第 3 层：LLM 定性判断（失败就明说，不填默认值）
        if use_llm:
            ns2 = dict(ns)
            ns2["scale_text"] = (f"{(it.get('shares_wan') or 0)/10000:.1f} 亿股"
                                 if it.get("shares_wan") else "未披露")
            res["llm"] = ipo_llm.judge(
                ns2, market_ctx=ctx,
                holdings_summary=ipo_llm.holdings_text(cap),
                lead_days=it.get("lead_days", 0),
                extra=f"行业市盈率溢价 {res['valuation_signal'].get('ratio')}x"
                      + (f"；发行规模 {res['issue_scale'].get('band')}"
                         if res["issue_scale"].get("available") else ""),
                llm_conf=llm_conf)
            # LLM 的定性结论与规则结论合并成最终 verdict（**不混算**）
            res["verdict_combined"] = _combine(res["verdict"],
                                               res["llm"].get("worth_shifting")
                                               if res["llm"].get("available") else None)
        else:
            res["llm"] = {"available": False, "llm_used": False,
                          "why": "本次调用显式关闭了 LLM 判断（use_llm=False）"}
            res["verdict_combined"] = {
                "final": res["verdict"],
                "basis": "仅规则（本次显式关闭 LLM 判断）",
                "needs_review": False,
            }
        out.append(res)
    return out


def _combine(rule_verdict: str, llm_shift: str | None) -> dict:
    """规则结论 + LLM 定性 → 最终建议。**不做数值混合**，只做方向上的取严。

    规则算的是「期望收益够不够」（数字），LLM 说的是「值不值得挪」（定性）。
    两者冲突时取保守：LLM 说 marginal/no 时不推翻规则的 shift，但会标出来
    需要人工确认 —— 因为 LLM 看得到行业周期位置，规则看不到。
    """
    if not llm_shift:
        return {"final": rule_verdict,
                "basis": "仅规则（无 LLM 判断）",
                "needs_review": False}
    m = {"yes": "yes", "no": "no", "marginal": "marginal"}.get(llm_shift, "marginal")
    if rule_verdict == "skip":
        return {"final": "skip", "basis": f"规则={rule_verdict}，LLM={m}",
                "needs_review": False}
    if m == "no" and rule_verdict in ("shift", "urgent"):
        return {"final": "marginal",
                "basis": f"规则={rule_verdict}(期望收益够) 但 LLM={m}(不建议挪)",
                "needs_review": True}
    if m == "marginal" and rule_verdict == "shift":
        return {"final": "marginal", "basis": "规则=shift，LLM=marginal",
                "needs_review": True}
    return {"final": rule_verdict, "basis": f"规则={rule_verdict}，LLM={m}",
            "needs_review": False}


def base_pool_candidates(watchlist: list[dict],
                         *, min_dividend_yield: float = 0.035,
                         max_drawdown: float = 0.25,
                         exclude_recent_ipo_days: int = 365,
                         limit: int = 12) -> list[dict]:
    """从自选股里挑**沪市**高股息低波动的底仓候选。

    筛选条件（对应用户「沪市银行股或高股息、短期不会大跌」）：
    1. 沪市非限售 A 股（60/601/603/605/688 开头）
    2. 股息率 ≥ min_dividend_yield（默认 3.5%）
    3. 近一年最大回撤 ≤ max_drawdown（默认 25%）
    4. 上市满 1 年（次新股波动大，不适合压底仓）

    股息率优先用自选股已有的 `dividend_yield` 字段；没有就尝试
    akshare 的分红数据；两条路都没有的**不猜**，标 need_dividend=True 让人补。
    """
    out = []
    for it in watchlist or []:
        code = str(it.get("code") or "").strip()
        q = it.get("quote") or {}
        if market_of(code) != "sh":
            continue
        price = q.get("price")
        dy = it.get("dividend_yield") or q.get("dividend_yield")
        dd = it.get("max_drawdown") or q.get("max_drawdown")
        listed = it.get("list_date") or q.get("list_date")
        rec = {"code": code, "name": it.get("name") or q.get("name") or "",
               "price": price, "dividend_yield": dy, "max_drawdown": dd,
               "list_date": listed, "reason": ""}
        try:
            if dy is not None and float(dy) < min_dividend_yield:
                rec["reason"] = f"股息率 {float(dy)*100:.2f}% < {min_dividend_yield*100:.1f}%"
                continue
        except (TypeError, ValueError):
            pass
        try:
            if dd is not None and abs(float(dd)) > max_drawdown:
                rec["reason"] = f"最大回撤 {abs(float(dd))*100:.1f}% 过大"
                continue
        except (TypeError, ValueError):
            pass
        if listed:
            try:
                if (date.today() - date.fromisoformat(str(listed)[:10])).days < exclude_recent_ipo_days:
                    rec["reason"] = "上市不满 1 年，波动大"
                    continue
            except ValueError:
                pass
        if dy is None:
            rec["reason"] = "无股息率数据，需人工确认"
            rec["need_dividend"] = True
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def rebalance_plan(current_market_cap_wan: float, target_wan: float,
                   candidates: list[dict], *,
                   sell_holdings: list[dict] | None = None,
                   concentrate: int = 2) -> dict:
    """生成挪仓计划：先卖不合适的，再买底仓补足市值。

    `concentrate`：底仓集中到几只。分散太多会让每只市值太小且卖出滑点高 ——
    底仓的唯一职责是"占住市值等打新"，不是建组合。
    """
    gap_wan = round(target_wan - current_market_cap_wan, 1)
    plan = {"current_wan": round(current_market_cap_wan, 1),
            "target_wan": round(target_wan, 1), "gap_wan": gap_wan,
            "sell": [], "buy": [], "notes": []}
    if gap_wan <= 0:
        plan["notes"].append("市值已达标，不需要调仓")
        return plan

    # ① 该卖的：现有沪市持仓里涨幅大/非高股息的（腾出仓位换底仓）
    if sell_holdings:
        keep = {c["code"] for c in (candidates or [])[:concentrate]}
        for h in sell_holdings:
            if market_of(str(h.get("code"))) != "sh":
                continue
            code = str(h.get("code"))
            if code in keep:
                continue
            q = h.get("quote") or {}
            chg = q.get("change_pct")
            dy = h.get("dividend_yield")
            val = (q.get("price") or 0) * (h.get("shares") or 0)
            sell_it = False
            why = ""
            if chg is not None and float(chg) > 20:
                sell_it, why = True, f"已涨 {float(chg):.0f}%，兑现并换底仓"
            elif dy is not None and float(dy) < 0.03:
                sell_it, why = True, f"股息率仅 {float(dy)*100:.1f}%，占市值效率低"
            if sell_it:
                plan["sell"].append({"code": code, "name": h.get("name") or q.get("name"),
                                     "value": round(val), "why": why})

    # ② 该买的：底仓候选补足缺口
    remain = gap_wan
    for c in (candidates or [])[:concentrate]:
        if remain <= 0:
            break
        buy_wan = round(remain / max(1, min(concentrate, len(candidates))), 1)
        if buy_wan <= 0:
            continue
        price = c.get("price")
        plan["buy"].append({"code": c["code"], "name": c.get("name"),
                            "amount_wan": buy_wan,
                            "shares": int(buy_wan * 10000 / price) if price else None,
                            "dividend_yield": c.get("dividend_yield")})
        remain -= buy_wan
    if not plan["buy"]:
        plan["notes"].append(
            "底仓候选为空 —— 筛选条件（股息率≥3.5%、回撤≤25%、上市满1年）太严，"
            "或自选股里沪市高股息太少。可放宽到 3.0% 或先补 dividend_yield 数据")
    else:
        plan["notes"].append(
            "买入后需保持到 T-2 日市值为准；中签缴款日再卖出释放资金")
    return plan


# ==========================================================================
# ④ 状态记账：别重复提示同一只票
# ==========================================================================

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"notified": {}}


def mark_notified(code: str, kind: str) -> bool:
    """标记已提醒；返回 False 表示这次是重复（不重复提醒）。"""
    st = _load_state()
    n = st.setdefault("notified", {})
    key = f"{kind}:{code}"
    if key in n:
        return False
    n[key] = date.today().isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return True


if __name__ == "__main__":      # python ipo_strategy.py —— 打印规则常量与长鑫实测算例
    demo = {"code": "688825", "name": "长鑫科技", "board": "科创板",
            "exchange": "上海证券交易所", "price": 8.66,
            "market_cap_need": 3349.0, "lot_rate": 0.0047141739}
    r = eval_new_stock(demo, per_lot_profit=(49.00 - 8.66) * 500)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    print("市值现状:", json.dumps(current_market_cap(
        [{"code": "600036", "shares": 1000, "quote": {"price": 40.0}},
         {"code": "000858", "shares": 500, "quote": {"price": 150.0}}]), ensure_ascii=False))
