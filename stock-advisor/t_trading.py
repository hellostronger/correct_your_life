# -*- coding: utf-8 -*-
"""做 T 引擎：机动仓高抛低吸，覆盖日内 / 周内 / 月内 / 年内四个周期。

为什么 A 股的「做 T」只能是这个形态
-----------------------------------
A 股 T+1：**当日买入的份额当日不可卖**。所以日内反复买卖在 A 股唯一可行的
形式是「**持仓不变的份额置换**」：

    底仓（永不动，拿住筹码吃趋势）
      + 机动仓（默认 30%，唯一允许日内买卖的部分）

高抛 = 卖掉机动仓（用的全是**昨日及更早**的份额，T+1 合规）；
低吸 = 把机动仓买回来。**总持仓不变**，赚的是价差。

如果反过来「先买后卖」，当日买入的那部分当日根本卖不掉 —— 这不是策略选择，
是交易所规则。港股 T+0 没有这个约束，但港股费用高得多
（实测同额往返 A 股 76 元 / 港股 720.70 元），所以同样的网格间距在港股
必须放宽，否则做 T 做得越多亏得越多。**费用差已经写进成本模型，不要手改。**

四层周期
--------
| 层 | 基准 | 数据 | 定位 |
|---|---|---|---|
| L1 日内 | 当日 VWAP + 分时高低 | 1 分钟分时/m5 | 主战场 |
| L2 周内 | 5 日均价 | 日 K | 波段 |
| L3 月内 | 20 日均价 | 日 K | 月线 |
| L4 年内 | 60 日均价 | 日 K | 底仓管理，只减不加 |

四层**共用同一个机动仓额度**，任一层触发都从机动仓扣 —— 不是四份仓位叠加。
所以同时满足四层条件也只会成交一次（避免重复下单把机动仓打穿）。

三条不可协商的纪律
------------------
1. **止损优先于做 T。** 主止损触发时做 T 必须让路，不允许「今天做 T 所以先不卖」。
   `run_t` 永远在 `check_stop_loss` **之后**被调用，且卖出的永远只是机动仓。
2. **不追跌补仓。** 高抛后若趋势转熊，补回门槛自动加倍（见 `_refill_threshold`），
   且有单日补回次数上限 —— 防止「越跌越买」把机动仓全变成死仓。
3. **成本门槛。** 触发幅度必须 ≥ `min_edge_pct`（默认 0.45% = 3 倍往返成本
   + 滑点）。低于这个数就是白给券商打工。

模块**不 import app**（app 的 import 就要 80 秒且会起守护线程），
需要什么由调用方通过 `deps` 传进来。
"""
from __future__ import annotations

import time
from datetime import datetime

import market_data as MD
import paper_trading as PT

# ---------------- 默认参数（配置缺失时的兜底，与 DEFAULTS 口径一致）----------------
T_DEFAULTS = {
    "enabled": False,            # 总量开关。默认关 —— 改了交易口径，不能默认生效。
    "t_ratio": 30,               # 机动仓占持仓比例 %
    "min_edge_pct": 0.45,        # 单次最小触发幅度 %（= 3 倍往返成本 + 滑点）
    "slippage_bps": 5,           # 单边滑点 bp（0.05%）。做 T 频繁，这个不能省。
    "max_t_per_day": 4,          # 单票单日做 T 次数上限
    "max_refill_bearish": 2,     # 趋势转熊时补回门槛的倍数
    "refill_max_per_day": 1,     # 单票单日补回次数上限（防止越跌越买）
    # 门槛相对「往返成本」的安全倍数（2026-10-11）。
    # 1 = 门槛正好等于成本 -> 实测 00136 港股走一遍净收益 +0.00 元，
    #     等于建议用户做一笔收益为零、却要承担卖飞/接早风险的交易。
    "safety_mult": 2.0,
    "layers": {                  # 每层周期（交易日）与最小间隔（小时）
        "L1_intraday": {"days": 0, "min_gap_hours": 0.0},
        "L2_week":     {"days": 5,   "min_gap_hours": 0.0},
        "L3_month":    {"days": 20,  "min_gap_hours": 0.0},
        "L4_year":     {"days": 60,  "min_gap_hours": 0.0},
    },
    "bearish_ma": "down",        # 均线排列判定：down=空头排列则加深门槛
}

# T 操作的标记前缀。用来区分「做 T 的成交」和「普通 LLM 决策的成交」，
# 因为做 T 的锚定价要沿着自己的成交链走，不能被普通买卖干扰。
T_TAG = "[做T]"


def _conf(raw: dict | None) -> dict:
    c = dict(T_DEFAULTS)
    c.update(raw or {})
    lay = dict(T_DEFAULTS["layers"])
    lay.update((raw or {}).get("layers") or {})
    c["layers"] = lay
    return c


def fnum(v, spec="%.2f", dash="—"):
    """所有数值的格式化出口。None/非数值 -> 破折号。

    AGENTS.md 记过一次真事故：15 只持仓跑完 90 分钟 LLM，最后写报告时
    `f"{r.get('price'):.2f}"` 遇到 None 抛 TypeError，成果全丢。
    渲染层不该有能力弄丢数据。
    """
    try:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return dash
        return spec % v
    except (TypeError, ValueError):
        return dash


# ---------------- 往返成本（决定 min_edge_pct，不该手填）----------------

def round_trip_cost_pct(code: str, conf: dict, fees_c: dict | None = None) -> float:
    """一次「卖→买回」往返的总成本，占成交额百分比。

    ⚠️ **必须按真实成交额算，不能按比例硬写** —— 因为最低佣金 5 元让小额度
    的实际费率远高于名义费率（实测 A 股 5000 元一笔：名义万2.5，实际万3.4）。
    用名义费率做门槛会让小账户「每笔都刚好不赚钱」。

    做法：用 10000 元名义额试算，得到费率，再算固定费用对实际额度的摊薄。
    这里返回的是**费率部分 + 按给定规模估算的固定费用**，
    固定费用部分由调用方按自己那笔的真实金额补（见 `cost_for_amount`）。
    """
    c = _conf(conf)
    fees = fees_c or {}
    probe = PT.trade_fees(code, "", "sell", 10000.0, fees)["total"] \
        + PT.trade_fees(code, "", "buy", 10000.0, fees)["total"]
    return probe / 10000.0 * 100.0


def execution_ticket(code: str, name: str, legs: str, price: float,
                     shares: int, thr_pct: float, conf: dict,
                     fees_c: dict | None = None, base_shares: int = 0,
                     bias_label: str = "", bias_why: str = "",
                     invalidate: str = "") -> dict:
    """把一次做T决策变成**可执行的双腿单**（纯函数）。

    为什么必须有：做T的本质是**一次往返**，只给一条腿用户没法执行 ——
    卖出去之后不知道该在哪接回，这正是「没闭环」在T上的体现。
    所以这里一次给全：卖出腿、买回腿、两腿价差、净收益、以及**不回落怎么办**。

    净收益用**真实费率实算**（trade_fees + 滑点），不是按名义费率估算。
    最低佣金 5 元对小额影响极大：实测 000158（机动仓 1267 元）
    往返手续费 10.66 元 + 滑点 1.27 元，而毛利只有 11.93 元 —— 净为零。
    这类单子**必须显示「不划算」而不是照样推荐**。
    """
    c = _conf(conf)
    mult = _safety_mult(c)
    slip_bps = float(c.get("slippage_bps") or 0)
    shares = int(shares or 0)
    price = float(price or 0)
    thr = float(thr_pct or 0)
    t = {"code": code, "name": name, "legs": legs, "shares": shares,
         "price": price, "threshold_pct": thr, "safety_mult": mult,
         "bias_label": bias_label, "bias_why": bias_why,
         "invalidate": invalidate, "base_shares": int(base_shares or 0),
         "cost_pct": None, "net": None, "buy_back": None, "sell_net_cash": None,
         "warn": ""}
    if shares < 1 or price <= 0:
        t["warn"] = "参数不足，算不出执行单"
        return t
    if thr <= 0:
        # hold 分支的 decide() 会提前 return，threshold 留 0。这里**不算**执行单
        # （没有可执行的双腿），但要让调用方能说清「差多少才够门槛」——
        # 否则页面上只会显示一句「参数不足」，对用户毫无意义。
        t["warn"] = ""
        return t
    value = price * shares
    cost_pct = cost_for_amount(code, value, c, fees_c)
    t["cost_pct"] = round(cost_pct, 4)
    sf = PT.trade_fees(code, name, "sell", value, fees_c)
    bf = PT.trade_fees(code, name, "buy", value, fees_c)
    fee_rt = sf["total"] + bf["total"]
    slip = value * 2 * slip_bps / 10000.0
    if legs == "t_sell":
        # 第1腿现在卖，第2腿等回调接回
        buy_price = price * (1 - thr / 100.0)
        gross = (price - buy_price) * shares
        net = gross - fee_rt - slip
        t.update({
            "sell": {"price": price, "shares": shares, "gross_cash": value,
                     "net_cash": round(value - sf["total"], 2),
                     "fee": round(sf["total"], 2)},
            "buy_back": {"price": round(buy_price, 6), "shares": shares,
                         "trigger_pct": -thr,
                         "note": "跌到该价以下才买回；不到就一直等，不追买"},
            "gross": round(gross, 2), "fees": round(fee_rt, 2),
            "slippage": round(slip, 2), "net": round(net, 2),
            "gap_pct": thr,
            "if_not_returning": (
                "持有 %d 股现金不追买；底仓 %d 股不动。机动仓上限 %s%% 不变，"
                "下次仍可做T" % (shares, int(base_shares or 0),
                                fnum(c.get("t_ratio") or 0, "%.0f"))),
        })
        if net <= 0 or (gross > 0 and net / gross < 0.25):
            # ⚠️ 判据是**比例**不是 `net <= 0`。安全倍数=1 时净收益是
            # 浮点意义上的 +0.0000001，`net <= 0` 抓不到，于是照样推荐一笔
            # 收益为零的交易（实测 00136 港股）。
            # 关系式：net/gross = 1 - cost/thr，所以 thr = cost×1 时比值为 0。
            t["warn"] = ("净收益仅占毛利 %.0f%%（%.2f 元）——门槛 %.3f%% 只比"
                         "往返成本 %.3f%% 高一点点，费用几乎吃光价差。"
                         "门槛应 ≥ 成本 × %.1f = %.3f%%" % (
                             (net / gross * 100) if gross > 0 else 0.0, net,
                             thr, cost_pct, mult, cost_pct * mult))
    else:
        # 已高抛，现在等接回
        t.update({
            "buy_back": {"price": round(price, 6), "shares": shares,
                         "trigger_pct": round((price / (price / (1 - thr / 100.0)) - 1) * 100
                                           if thr else 0.0, 4),
                         "note": "现价已到接回位，可执行买回"},
            "gross": None, "fees": round(fee_rt, 2), "slippage": round(slip, 2),
            "net": None,
            "if_not_returning": "继续持有现金等待，机动仓保持空仓；底仓不受影响",
        })
    return t


def _safety_mult(c: dict) -> float:
    """门槛相对往返成本的安全倍数。默认 2，最小 1。

    为什么必须有：**门槛 = 成本** 意味着这一笔 T 的净收益恰好为零。
    做完扣完双边手续费和滑点，毛利正好抵掉成本 —— 一分钱不赚，
    却承担了「卖飞 / 接早」的方向风险。实测 00136 港股就是这种情况。
    """
    try:
        m = float(c.get("safety_mult") or 2)
    except (TypeError, ValueError):
        m = 2.0
    return max(1.0, m)


def cost_for_amount(code: str, value: float, conf: dict,
                    fees_c: dict | None = None) -> float:
    """给定成交金额的一次往返成本（百分比）。这是做 T 判据真正该用的数。"""
    value = max(0.0, float(value or 0))
    if value <= 0:
        return 0.0
    fees = fees_c or {}
    tot = (PT.trade_fees(code, "", "sell", value, fees)["total"]
           + PT.trade_fees(code, "", "buy", value, fees)["total"])
    slip = 2.0 * float(_conf(conf).get("slippage_bps") or 0) / 100.0
    return tot / value * 100.0 + slip


# ---------------- 机动仓 ----------------

def lot_size(code: str) -> int:
    """申报单位。科创板最少 200 股且超出后 1 股递增；其余 100 股整数倍。"""
    return 1 if PT.is_star_market(code) else 100


def movable_shares(code: str, held: int, conf: dict) -> int:
    """机动仓股数。**永远向下取整到申报单位** —— 少做比做错安全。

    底仓 = held - movable，至少保留 70%。
    """
    held = int(held or 0)
    if held <= 0:
        return 0
    c = _conf(conf)
    lot = lot_size(code)
    raw = held * float(c.get("t_ratio") or 0) / 100.0
    n = int(raw // lot) * lot
    return max(0, min(n, held - lot if held > lot else 0))


# ---------------- 锚定价：沿着「做 T 自己的成交链」走 ----------------

def t_anchor(cur, code: str) -> dict | None:
    """这只票最近一次做 T 成交的价格与方向。

    从 `reasoning` 的 `[做T]` 标记反查，而不是新建状态表 —— 少一张表就少一处
    会与库不一致的地方。方向由 side 决定：最后一笔是 sell 就等买回，
    是 buy 就等下一次卖出。
    """
    cur.execute("""SELECT side, price, shares, trade_date, created_at
                   FROM sa_paper_trades
                   WHERE code=%s AND side IN ('buy','sell')
                     AND status <> 'skipped'
                     AND reasoning LIKE %s
                   ORDER BY id DESC LIMIT 1""", (code, T_TAG + "%"))
    r = cur.fetchone()
    if not r:
        return None
    if not isinstance(r, dict):
        cols = ("side", "price", "shares", "trade_date", "created_at")
        r = dict(zip(cols, r))
    try:
        return {"side": r["side"], "price": float(r["price"] or 0),
                "shares": int(r["shares"] or 0),
                "trade_date": str(r["trade_date"])[:10],
                "created_at": r["created_at"]}
    except (TypeError, ValueError):
        return None


def no_anchor_price(cur, code: str) -> float | None:
    """还没做过 T 时的首次锚定价 —— 用**持仓加权成本**。

    用成本而不是最新价：成本是「你愿意在这个价位不亏的位置」，从成本起算
    高抛才有意义。用最新价起算会让每次刷新都把锚点往前挪一点，永远触发不了。
    """
    cur.execute("""SELECT COALESCE(SUM(shares*price),0) / NULLIF(SUM(shares),0)
                   FROM sa_paper_trades
                   WHERE code=%s AND side IN ('buy','sell')
                     AND status <> 'skipped'""", (code,))
    r = cur.fetchone()
    v = (r[0] if not isinstance(r, dict) else r.get("cost"))
    return float(v) if v else None


# ---------------- 趋势过滤（决定补回门槛与次数上限）----------------

def trend_state(bars: list[dict]) -> str:
    """均线排列 -> 'up' / 'down' / 'flat'。

    用**日 K**，与 L2~L4 同源。注意这是日频口径，而 L1 是日内 —— 两者频率
    不同是事实，不要为了「一致」把日频指标硬套到分钟上。
    """
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < 20:
        return "flat"
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0 if len(closes) >= 60 else None
    last = closes[-1]
    if ma60 is None:
        return "up" if last > ma20 else "down"
    if last > ma20 > ma60:
        return "up"
    if last < ma20 < ma60:
        return "down"
    return "flat"


def _refill_threshold(c: dict, base_edge: float, trend: str) -> float:
    """趋势转熊时补回门槛加倍 —— 落实「觉得还会跌就先不补」。

    做法不是「禁止补回」（那会让高抛后永远不接回，机动仓永久空转），
    而是**把门槛抬高**：价格要跌得更多才接。下跌越深越容易接回，
    但不会在半山腰就接。
    """
    if trend == "down" and c.get("bearish_ma") == "down":
        return base_edge * max(1.0, float(c.get("max_refill_bearish") or 1))
    return base_edge


# ---------------- 周期判据 ----------------

def layer_anchors(bars_daily: list[dict], intraday: dict | None,
                  conf: dict) -> dict:
    """算四层各自的「参考基准价」。

    返回 {层名: {"anchor": 基准价, "now": 现价, "edge_pct": (现价/基准-1)*100,
                 "bars": 依据根数}}。edge_pct > 0 表示现价高于基准（该高抛）。
    """
    out = {}
    closes = [b["close"] for b in bars_daily if b.get("close")]
    now = None
    if intraday and intraday.get("bars"):
        last = intraday["bars"][-1]
        now = float(last.get("close") or 0) or None
    if now is None and closes:
        now = closes[-1]

    c = _conf(conf)
    for name, spec in (c.get("layers") or {}).items():
        try:
            days = int(spec.get("days") or 0)
        except (TypeError, ValueError):
            continue
        if days <= 0:
            # L1 日内：基准用**当日 VWAP**。用 VWAP 而不是开盘价，因为
            # 「今天大部分人成交在什么价位」才是日内高抛低吸的参照系。
            a = (intraday or {}).get("vwap")
        else:
            a = None
            if len(closes) >= max(days, 2):
                a = sum(closes[-days:]) / float(days)
        if not a or not now:
            continue
        out[name] = {"anchor": round(float(a), 4), "now": round(float(now), 4),
                     "edge_pct": round((float(now) / float(a) - 1) * 100, 4),
                     "bars": days if days > 0 else len((intraday or {}).get("bars") or [])}
    return out


# ---------------- 决策 ----------------

def decide(code: str, name: str, held: int, price: float, quote: dict,
           anchors: dict, trend: str, conf: dict, fees_c: dict | None = None,
           anchor_info: dict | None = None, today_t: list[str] | None = None,
           intraday: dict | None = None, min_bars: int = 6) -> dict:
    """对一只票给出做 T 决策（纯函数，不碰库、不成交）。

    返回 {"action": "t_sell"|"t_buy"|"hold", "shares": N, "why": str,
          "layer": 层名, "edge_pct": x, "threshold": y, "bias": ..., ...}

    **方向决定先做哪一步**（`t_bias`）：上行先买回、震荡先卖出、下行反弹就减
    且不接下跌途中的下跌。方向判错 → 步骤反了 → 就是做反T。这是本函数最重要
    的一条，也是「方向要对」的落点。

    其余硬约束，任何一条不满足就 hold —— **宁可不做，不可做错**：
      1. 必须是机动仓额度内（`movable_shares`）
      2. 触发幅度必须 >= 门槛（门槛含成本，且按方向浮动）
      3. 单日次数 / 补回次数上限
      4. 下行方向的买回**必须**先确认止跌
    """
    c = _conf(conf)
    bias = t_bias(trend, intraday, price)
    out = {"code": code, "name": name, "action": "hold", "shares": 0,
           "price": price, "why": "", "layer": "", "edge_pct": 0.0,
           "threshold": 0.0, "held": int(held or 0),
           "bias": bias["bias"], "bias_label": bias["label"],
           "bias_why": bias["why"], "sell_mult": bias["sell_mult"],
           "buy_mult": bias["buy_mult"]}
    if held <= 0:
        out["why"] = "无持仓"
        return out
    mv = movable_shares(code, held, c)
    out["movable"] = mv
    if mv <= 0:
        out["why"] = ("持仓 %d 股，按 %.0f%% 机动仓向下取整后不足 1 个申报单位"
                      "（%.0f 股），本票不做 T" % (
                          held, float(c.get("t_ratio") or 0),
                          held * float(c.get("t_ratio") or 0) / 100.0))
        return out

    today_t = today_t or []
    n_sell = today_t.count("t_sell")
    n_buy = today_t.count("t_buy")
    if n_sell >= int(c.get("max_t_per_day") or 4):
        out["why"] = f"今日做T次数 {n_sell} 已达上限 {c.get('max_t_per_day')}"
        return out

    # ---- 方向不明：什么都不做 ----
    # 方向说不清的时候硬选一个方向，就是「看起来在运行、其实在乱做」。
    if bias["bias"] == "flat":
        out["why"] = bias["why"]
        return out

    # ---- 买回优先：已高抛、待买回 -> 走「买回」分支 ----
    # 顺序很重要：**必须在 anchors 空判之前**。买回的锚是上次 T 成交价，
    # 与日 K/分时基准无关；反了的话「分时拿不到 -> 不买回」会让高抛出去的
    # 机动仓永远接不回来（2026-10-11 离线测试逮到）。
    if anchor_info and anchor_info.get("side") == "sell" and anchor_info.get("price"):
        base_edge = float(c.get("min_edge_pct") or 0.45) * float(bias["buy_mult"])
        thr = _refill_threshold(c, base_edge, trend)
        bp = float(anchor_info["price"])
        edge = (float(price) / bp - 1) * 100.0
        # 下行方向：**先要止跌确认，再谈价位**。反过来会在下跌途中一路接回。
        stab_ok, stab_why = (True, "")
        if bias["need_stab"]:
            stab_ok, stab_why = stabilizing((intraday or {}).get("bars") or [],
                                             n=min_bars)
            out["stabilizing"] = stab_ok
            out["stabilize_why"] = stab_why
            if not stab_ok and price > bp * (1 - thr / 100.0):
                out["why"] = ("下行方向，接回需先确认止跌：%s。价未到接回位 %s"
                              "（锚价 %s，门槛 %s%%）" % (
                                  stab_why, fnum(bp * (1 - thr / 100.0)),
                                  fnum(bp), fnum(thr)))
                out["threshold"] = round(thr, 4)
                out["edge_pct"] = round(edge, 4)
                out["anchor_price"] = bp
                return out
        need = bp * (1 - thr / 100.0)
        # 下行方向：**未确认止跌就一律不接**，不看价格到没到位。
        # ⚠️ 这里曾经写成 `if not stab_ok and price > need:` —— 于是「跌得够深」
        #    反而**绕过**了止跌确认。逻辑是反的：方向判成下行的票，恰恰是
        #    跌得最深、续跌概率最大的那一批。2026-10-11 端到端测试逮到。
        if bias["need_stab"] and not stab_ok:
            out["threshold"] = round(thr, 4)
            out["edge_pct"] = round(edge, 4)
            out["anchor_price"] = bp
            out["trigger_price"] = round(need, 4)
            reached = "" if price <= need else ("（价格已跌破接回位 %s）" % fnum(need))
            out["why"] = ("下行方向，未确认止跌，**不接回**%s：%s。"
                          "等止跌确认后再看接回位 %s（门槛 %s%%）" % (
                              reached, stab_why, fnum(need), fnum(thr)))
            return out
        if price <= need:
            if n_buy >= int(c.get("refill_max_per_day") or 1):
                out["why"] = (f"已跌破买回位 {fnum(need)}（锚价 {fnum(bp)}，"
                              f"门槛 {fnum(thr)}%），但今日补回 {n_buy} 次已达上限")
                return out
            cost = cost_for_amount(code, bp * mv, c, fees_c)
            floor = cost * _safety_mult(c)
            if thr < floor:
                # 门槛低于「成本 × 安全倍数」-> 这一笔做下来白折腾，改用下限兜底。
                # ⚠️ 原来是 `thr = cost`，安全边际恒为 0 —— 实测 00136 港股
                # (cost 0.8482%) 门槛就是 0.8482%，扣完双边费+滑点净收益 +0.00 元。
                # 系统在建议你做一笔收益为零的交易。见 plan-t-ticket-and-close-loop.md
                thr = floor
                need = bp * (1 - thr / 100.0)
            if price > need:
                out["why"] = f"门槛按真实成本上调到 {fnum(thr)}% 后未达"
                return out
            qty = _round_lot(code, mv)
            if qty <= 0:
                out["why"] = "机动仓不足 1 个申报单位"
                return out
            extra = ("，且已确认止跌（%s）" % stab_why) if stab_ok and bias["need_stab"] else ""
            out.update({"action": "t_buy", "shares": qty, "layer": "refill",
                        "edge_pct": round(edge, 4), "threshold": round(thr, 4),
                        "anchor_price": bp, "trigger_price": round(need, 4),
                        "why":
                        f"高抛后回落：锚价 {fnum(bp)} → 现价 {fnum(price)}"
                        f"（{fnum(edge)}%），门槛 {fnum(thr)}%"
                        f"（{bias['label']}方向 ×{bias['buy_mult']}，"
                        f"往返成本 {fnum(cost)}%）{extra}。机动仓 {qty} 股买回"})
            return out
        out["threshold"] = round(thr, 4)
        out["edge_pct"] = round(edge, 4)
        out["anchor_price"] = bp
        out["trigger_price"] = round(need, 4)
        out["why"] = (f"待买回但未到价：锚价 {fnum(bp)}，需跌到 {fnum(need)}"
                      f"（门槛 {fnum(thr)}%，{bias['label']}方向 ×{bias['buy_mult']}），"
                      f"现价 {fnum(price)}")
        # 结构化字段也要回填：why 里已经报了门槛，字段却是 0 会让任何读
        # threshold/edge_pct 的界面显示成 0 —— 「看起来有值、实际是错的」。
        out["threshold"] = round(thr, 4)
        out["edge_pct"] = round(edge, 4)
        out["anchor_price"] = bp
        return out

    # ---- 高抛：四层任一层现价高于基准足够多 ----
    if not anchors:
        out["why"] = "没有可用的周期基准（日内/日K 都不可得）"
        return out
    base_edge = float(c.get("min_edge_pct") or 0.45) * float(bias["sell_mult"])
    best = None
    for lname, a in anchors.items():
        if a["edge_pct"] <= 0:
            continue
        if best is None or a["edge_pct"] > best[1]["edge_pct"]:
            best = (lname, a)
    if not best:
        top = max(anchors.values(), key=lambda x: x["edge_pct"])
        out["why"] = (f"未高抛：最接近的基准 {fnum(top['anchor'])}，"
                      f"现价 {fnum(price)}（{fnum(top['edge_pct'])}%），"
                      f"门槛 {fnum(base_edge)}%")
        return out
    lname, a = best
    cost = cost_for_amount(code, price * mv, c, fees_c)
    # ⚠️ 关键修复（2026-10-11）：原来是 `max(base_edge, cost)`。
    #    min_edge_pct 是按「3 倍成本」定的，但 cost 一旦超过它（例如港股
    #    0.85%、小额账户 0.94%），max() 就把门槛压到**成本线上**，
    #    安全边际变成 0 —— 实测 00136 走一遍净收益恰好 +0.00 元。
    #    门槛必须比成本高出 safety_mult 倍，否则「做T」只是给券商打工。
    thr = max(base_edge, cost * _safety_mult(c))
    if a["edge_pct"] < thr:
        out["why"] = (f"{lname} 高出 {fnum(a['edge_pct'])}%，未达门槛 "
                      f"{fnum(thr)}%（{bias['label']}方向 ×{bias['sell_mult']}，"
                      f"= 往返成本 {fnum(cost)}% × {_safety_mult(c):g}）")
        return out
    qty = _round_lot(code, mv)
    if qty <= 0:
        out["why"] = "机动仓不足 1 个申报单位"
        return out
    out.update({"action": "t_sell", "shares": qty, "layer": lname,
                "edge_pct": a["edge_pct"], "threshold": round(thr, 4),
                "anchor_price": a["anchor"], "why":
                f"{lname} 基准 {fnum(a['anchor'])}，现价 {fnum(price)}"
                f"（+{fnum(a['edge_pct'])}%）≥ 门槛 {fnum(thr)}%"
                f"（{bias['label']}方向 ×{bias['sell_mult']}，含往返成本 {fnum(cost)}%），"
                f"机动仓 {qty} 股高抛"})
    return out


# ---------------- 方向判定（决定「先做哪一步」，这比「做不做」重要）----------------

def stabilizing(bars: list[dict], n: int = 6) -> tuple[bool, str]:
    """止跌确认：最近 n 根不再创新低，且价格重心上移。

    为什么必须有它：在下跌趋势里，「跌到阈值」**不是**买点，只是跌到了某个
    位置。价格从 11.5 一路跌到 11.0 时，11.0 会依次满足每一个更高的阈值 ——
    纯阈值判据会一路接回、越接越低。这正是「越跌越买」的机制来源。

    返回 (是否止跌, 说明)。**数据不足一律判 False** —— 宁可不做。
    """
    b = [x for x in (bars or []) if x.get("close") is not None]
    if len(b) < n + 1:
        return False, "分时数据不足 %d/%d 根，判不了止跌" % (len(b), n + 1)
    tail = b[-(n + 1):]
    lows = [x["low"] for x in tail if x.get("low") is not None]
    closes = [x["close"] for x in tail if x.get("close") is not None]
    if len(lows) < n + 1 or len(closes) < n + 1:
        return False, "分时数据不完整，判不了止跌"
    if lows[-1] <= min(lows[:-1]):
        return False, "仍在创新低（最新 %.4f ≤ 区间最低 %.4f）" % (lows[-1], min(lows[:-1]))
    recent = sum(closes[-3:]) / 3.0
    earlier = sum(closes[-6:-3]) / 3.0
    if recent <= earlier:
        return False, "重心未上移（近3根均价 %.4f ≤ 前3根 %.4f）" % (recent, earlier)
    return True, "止跌：未创新低且重心上移（%.4f → %.4f）" % (earlier, recent)


# 方向 → (高抛门槛倍数, 买回门槛倍数, 是否强制要止跌确认)
BIAS_RULES = {
    "bull":  (2.0, 0.7, False),
    "range": (1.0, 1.0, False),
    "bear":  (0.7, 2.0, True),
}
BIAS_LABEL = {
    "bull":  "上行", "range": "震荡", "bear": "下行", "flat": "方向不明",
}


def t_bias(trend: str, intraday: dict | None, price: float) -> dict:
    """判定做T方向。**这是「方向对不对」的落点。**

    只用均线排列（日频）+ 现价相对日内 VWAP（日频+日内）两个**已实测可得**的
    量，不引入无法验证的指标。四档：

    - **bull  上行**：多头排列且价在 VWAP 之上。→ 高抛门槛 ×2（**别急着卖**，
      上涨途中的第一次回落通常很浅）、买回门槛 ×0.7（**尽快接回**，顺势持仓）
    - **bear  下行**：空头排列且价在 VWAP 之下。→ 高抛门槛 ×0.7（反弹就减）、
      买回门槛 ×2 **且强制要止跌确认**（不接下跌途中的下跌）
    - **range 震荡**：双向都按原门槛（经典高抛低吸）
    - **flat  方向不明**：**什么都不做**。理由说不清的时候不动手，
      这比「总要选个方向」安全

    ⚠️ 这不是「预测涨跌」。它决定的是**先做哪一步**：同样是涨，
    上行时先买回、震荡时先卖出。方向判错 → 步骤反了 → 就是做反T。
    """
    it = intraday or {}
    vwap = it.get("vwap")
    above_vwap = (vwap and price and price > float(vwap))
    below_vwap = (vwap and price and price < float(vwap))
    if trend == "up" and above_vwap:
        b = "bull"
    elif trend == "down" and below_vwap:
        b = "bear"
    elif trend == "flat" and not (vwap and price):
        b = "flat"
    else:
        b = "range"
    sm, bm, need_stab = BIAS_RULES.get(b, BIAS_RULES["range"])
    why = {
        "bull": "均线多头排列且现价 %s 日内均价（%.4f vs %.4f）——顺势做：先买回、涨透再抛" % (
            "站上", price, float(vwap) if vwap else 0),
        "bear": "均线空头排列且现价 %s 日内均价（%.4f vs %.4f）——逆势做：反弹就减、跌透且止跌才接" % (
            "跌破", price, float(vwap) if vwap else 0),
        "range": "无明显单边（%s，%s VWAP %.4f）——经典高抛低吸：先卖后买" % (
            BIAS_LABEL.get(trend, trend),
            "站上" if above_vwap else ("跌破" if below_vwap else "缺"), float(vwap) if vwap else 0),
        "flat": "方向判不出来（均线排列 %s、%s VWAP）——**宁可不做**" % (
            BIAS_LABEL.get(trend, trend),
            "有" if vwap else "无"),
    }[b]
    return {"bias": b, "label": BIAS_LABEL.get(b, b), "sell_mult": sm,
            "buy_mult": bm, "need_stab": need_stab, "why": why,
            "vwap": vwap, "trend": trend}


def _settle_buy_rows_note(*_a, **_k):        # 占位，避免误 import
    return None


def _round_lot(code: str, n: int) -> int:
    lot = lot_size(code)
    if lot <= 1:
        return int(n)
    return int(n // lot) * lot


# ---------------- 扫描 + 成交 ----------------

def preview(deps: dict, conf: dict, max_codes: int = 0, today: str = "") -> dict:
    """**只看不做**：走与 scan_once 完全相同的判据，但一步都不成交。

    为什么必须与 scan_once 共用判据：若预览另写一套「估算」逻辑，
    预览显示「该卖」而真跑不卖、或反之 —— 那这个预览就是骗人的，
    比没有预览更危险。所以这里只调用 `_judge_one`，差别只在最后是否
    调 `_execute_decision`。
    """
    c = _conf(conf)
    c["enabled"] = True                      # 预览不受 enabled 影响（调用方已判过）
    get_conn = deps["get_conn"]
    em_kline_fn = deps.get("em_kline_fn")
    tx_symbol_fn = deps.get("tx_symbol_fn") or MD.tx_symbol
    today = today or datetime.now().strftime("%Y-%m-%d")
    fees_c = deps.get("conf", {}).get("fees")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT DISTINCT code FROM sa_paper_trades
                           WHERE side IN ('buy','sell') AND status <> 'skipped'""")
            codes = [r[0] for r in cur.fetchall()]
    except Exception as exc:              # noqa: BLE001
        return {"enabled": True, "acted": 0, "items": [],
                "error": "读持仓失败: %s" % str(exc)[:120]}
    items = []
    for code in (codes[:max_codes] if max_codes else codes):
        try:
            items.append(_judge_one(deps, c, code, today, em_kline_fn,
                                    tx_symbol_fn, fees_c))
        except Exception as exc:          # noqa: BLE001
            items.append({"code": code, "action": "error", "why": str(exc)[:140]})
    would = [i for i in items if i.get("action") in ("t_sell", "t_buy")]
    return {"enabled": True, "dry_run": True, "scanned": len(codes),
            "would_act": len(would), "acted": 0, "items": items}


def _judge_one(deps, c, code, today, em_kline_fn, tx_symbol_fn, fees_c) -> dict:
    """**纯判断**：读库取持仓与锚定价 -> 取行情 -> 四层判据 -> 决策。

    scan_once 与 preview 都调它。这是「预览与执行同源」的唯一保证点。
    """
    get_conn = deps["get_conn"]
    fetch_quotes = _quotes_fn(deps)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sa_paper_trades WHERE code=%s "
                    "AND status<>'skipped' ORDER BY id DESC LIMIT 1", (code,))
        row = cur.fetchone()
        name = (row[0] if row else "") or ""
        positions = PT._derive_paper_positions(cur)
        pos = positions.get(code) or {}
        held = int(pos.get("shares") or 0)
        if held <= 0:
            return {"code": code, "name": name, "action": "hold", "why": "无持仓"}
        anchor_info = t_anchor(cur, code)
        cur.execute("""SELECT side FROM sa_paper_trades
                       WHERE code=%s AND trade_date=%s AND side IN ('buy','sell')
                         AND status<>'skipped'""", (code, today))
        today_t = [r[0] for r in cur.fetchall()]
        # 连接马上要关，先把还要用的数取出来
        no_anchor = no_anchor_price(cur, code) if not anchor_info else None

    quote = None
    if fetch_quotes:
        quote = (fetch_quotes([code]) or {}).get(code)
    if not quote or not quote.get("price"):
        # 拿不到行情就不给判据。**绝不臆造价格** —— 造一个价格再据此下单，
        # 是这类系统最典型的「看起来在运行、其实在乱交易」。
        return {"code": code, "name": name, "action": "hold",
                "why": "无行情，跳过（不臆造价格）"}
    price = float(quote["price"])

    bars_daily = []
    if em_kline_fn:
        try:
            bars_daily = PT.fetch_kline_ohlcv(em_kline_fn, tx_symbol_fn(code), days=60)
        except Exception:                  # noqa: BLE001
            bars_daily = []
    intraday = None
    try:
        intraday = MD.fetch_intraday(code, "m5", 320)
    except Exception:                      # noqa: BLE001
        intraday = None

    anchors = layer_anchors(bars_daily, intraday, c)
    trend = trend_state(bars_daily)
    if not anchor_info and no_anchor:
        # 首次做 T：锚定在持仓成本（用成本而非最新价，理由见 no_anchor_price）
        anchors = {"L0_cost": {"anchor": round(no_anchor, 4), "now": round(price, 4),
                               "edge_pct": round((price / no_anchor - 1) * 100, 4),
                               "bars": 0}}
    dec = decide(code, name, held, price, quote, anchors, trend, c,
                 fees_c=fees_c, anchor_info=anchor_info, today_t=today_t,
                 intraday=intraday)
    dec["trend"] = trend
    dec["anchors"] = {k: {"anchor": v["anchor"], "edge_pct": v["edge_pct"]}
                      for k, v in anchors.items()}
    dec["intraday_src"] = (intraday or {}).get("source")
    dec["intraday_vwap"] = (intraday or {}).get("vwap")
    dec["_bars_daily"] = bars_daily
    dec["_quote"] = quote
    return dec


def _execute_one(deps, c, code, today, slot, dec, name) -> dict:
    """把 _judge_one 的决策**真实成交**（走 _execute_decision 的全部闸门）。"""
    if dec.get("action") not in ("t_sell", "t_buy"):
        return dec
    action = "sell" if dec["action"] == "t_sell" else "buy"
    decision = {"action": action, "confidence": 5, "target_value_pct": 1,
                "stop_loss_pct": 2, "expected_up_pct": 1,
                "reasoning": "%s%s" % (T_TAG, dec["why"])}
    # 子序号 #2：让做 T 与同轮的 LLM 决策各有独立 slot，避免撞幂等守卫/唯一索引
    tslot = PT.make_slot(slot, 2)
    executed, note = PT._execute_decision(
        deps, deps.get("conf") or c, {"code": code, "name": name or code},
        today, decision, dec["_quote"], slot=tslot,
        bars=dec.get("_bars_daily"), shares_override=dec["shares"])
    dec["executed"] = bool(executed)
    dec["note"] = note
    dec["slot"] = tslot
    dec.pop("_bars_daily", None)
    dec.pop("_quote", None)
    print(f"[t] {code} {dec['action']} {dec['shares']}股 | "
          f"{'成交' if executed else '未成交'} | {dec['why']} | {note}", flush=True)
    return dec


def _quotes_fn(deps: dict):
    """取行情函数的 deps 键。**两个名字都认**，且必须有 fallback。

    踩过的坑（2026-10-11，线上 500 → 修复后又静默失效）：
      - `paper_trading` 用的键是 `quote_fn`（`app._paper_deps`），
        我一开始写的是 `fetch_quotes` → 永远取不到 → 每一行都显示
        「取不到实时价格，不给建议」，**页面看着在运行、实际永远不做**。
      - 这正是 AGENTS.md 说的「静默降级比报错危险」：它不抛异常、不少一行、
        徽标数只是 0，看起来一切正常。
    所以这里两个键都试，且**没有就明确报出来**，不要默默返回空。
    """
    return deps.get("fetch_quotes") or deps.get("quote_fn")


def advisory(deps: dict, conf: dict, positions: dict, today: str = "",
             max_codes: int = 0) -> dict:
    """**实盘做 T 建议**。纯只读 —— 一个字节都不写，不碰任何账户表。

    与 `scan_once` 的区别只有一个，也只有一个：**不成交**。判据完全同源
    （同一个 `decide`），否则这份「建议」就是骗人的。

    positions 由调用方传入（app.py 的 `_derive_holdings()`，从 `sa_trades`
    流水推导）。**为什么不自己查 `sa_holdings`**：那张表已经废弃了 ——
    app.py:3220-3227 有明确注释，实盘持仓全部由 sa_trades 推导，
    `sa_holdings` 早已没有任何写入路径。

    锚定价：优先取**最近一次做 T 成交**（用户自己操作完要用「记一次做T」录入），
    没有则用持仓成本。用成本而不是最新价的理由同 `no_anchor_price`。

    返回里每条都带 `invalidate`（**什么信号说明这个方向错了**）——
    用户要自己动手，就必须知道自己的判断在什么时候作废，
    否则只能盲从，而盲从一个不会说「我可能错了」的建议比不做更危险。
    """
    c = _conf(conf)
    em_kline_fn = deps.get("em_kline_fn")
    tx_symbol_fn = deps.get("tx_symbol_fn") or MD.tx_symbol
    fetch_quotes = _quotes_fn(deps)
    today = today or datetime.now().strftime("%Y-%m-%d")
    fees_c = deps.get("conf", {}).get("fees")
    get_conn = deps["get_conn"]
    # 方向正确率要读 sa_market_kline，独立持有一个只读游标（advisory 主流程
    # 用的是 with 块，游标出了块就没了）
    try:
        with get_conn() as _c:
            cur_ro = _c.cursor()
    except Exception:                      # noqa: BLE001
        cur_ro = None

    codes = sorted([k for k, v in (positions or {}).items()
                    if int((v or {}).get("shares") or 0) > 0])
    if not codes:
        return {"items": [], "scanned": 0, "note": "实盘没有持仓（持仓来自 sa_trades 流水）"}
    quotes = {}
    fq = _quotes_fn(deps)
    if not fq:
        # 宁可整个端点报「配置没接对」，也不要返回 30 行「取不到价格」——
        # 后者看起来一切正常、实际永远不做（本模块已因这个键名踩过一次）。
        return {"items": [], "scanned": 0, "would_act": 0,
                "error": "deps 里没有行情函数（缺 fetch_quotes / quote_fn），"
                         "无法给建议 —— 这是接线问题，不是没有信号",
                "read_only": True}
    try:
        quotes = fq(codes) or {}
    except Exception as exc:              # noqa: BLE001
        return {"items": [], "scanned": 0, "would_act": 0,
                "error": "行情获取失败：%s" % str(exc)[:160],
                "read_only": True}

    items = []
    for code in (codes[:max_codes] if max_codes else codes):
        p = positions.get(code) or {}
        name = p.get("name") or ""
        held = int(p.get("shares") or 0)
        cost = float(p.get("cost") or p.get("avg_cost") or 0)
        it = {"code": code, "name": name, "shares": held, "cost": cost,
              "action": "hold", "shares_suggest": 0, "why": "",
              "invalidate": "", "action_label": "不做"}
        q = quotes.get(code) or {}
        price = q.get("price")
        if not price:
            it["why"] = "取不到实时价格，不给建议（不用陈旧价替代）"
            it["invalidate"] = "—"
            items.append(it)
            continue
        price = float(price)
        it["price"] = price
        it["pnl_pct"] = round((price / cost - 1) * 100, 3) if cost else None

        anchor_info, today_t = None, []
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                anchor_info = real_anchor(cur, code)
                cur.execute("""SELECT side FROM sa_trades
                               WHERE code=%s AND trade_date=%s
                                 AND note LIKE %s""", (code, today, T_TAG + "%"))
                today_t = [r[0] for r in cur.fetchall()]
        except Exception as exc:          # noqa: BLE001
            it["why"] = "读做T台账失败，不给建议：%s" % str(exc)[:100]
            it["invalidate"] = "—"
            items.append(it)
            continue

        bars_daily = []
        if em_kline_fn:
            try:
                bars_daily = PT.fetch_kline_ohlcv(em_kline_fn, tx_symbol_fn(code), days=60)
            except Exception:              # noqa: BLE001
                bars_daily = []
        intraday = None
        try:
            intraday = MD.fetch_intraday(code, "m5", 320)
        except Exception:                  # noqa: BLE001
            intraday = None

        anchors = layer_anchors(bars_daily, intraday, c)
        trend = trend_state(bars_daily)
        if not anchor_info and cost:
            anchors = {"L0_cost": {"anchor": round(cost, 4), "now": round(price, 4),
                                   "edge_pct": round((price / cost - 1) * 100, 4),
                                   "bars": 0}}
        dec = decide(code, name, held, price, q, anchors, trend, c,
                     fees_c=fees_c, anchor_info=anchor_info, today_t=today_t,
                     intraday=intraday)
        it.update({
            "action": dec["action"], "shares_suggest": dec.get("shares", 0),
            "why": dec.get("why", ""), "bias": dec.get("bias"),
            "bias_label": dec.get("bias_label"), "bias_why": dec.get("bias_why", ""),
            "edge_pct": dec.get("edge_pct"), "threshold": dec.get("threshold"),
            "anchor_price": dec.get("anchor_price"),
            "layer": dec.get("layer", ""), "movable": dec.get("movable", 0),
            "base_shares": max(0, held - int(dec.get("movable") or 0)),
            "pending": bool(anchor_info and anchor_info.get("side") == "sell"),
            "stabilizing": dec.get("stabilizing"),
            "anchors": dec.get("anchors") or {
                k: {"anchor": v["anchor"], "edge_pct": v["edge_pct"]}
                for k, v in anchors.items()},
            "intraday_src": (intraday or {}).get("source"),
            "intraday_vwap": (intraday or {}).get("vwap"),
        })
        it["action_label"] = {"t_sell": "高抛（先卖）", "t_buy": "低吸（买回）",
                              "hold": "不做"}.get(it["action"], "不做")
        it["invalidate"] = _invalidate(dec, it["bias"], price, (intraday or {}).get("vwap"))
        # 执行单：**两条腿一次给全**。hold 状态也给「差多少才够门槛 / 为什么不做」，
        # 否则用户只看到一句「不做」，无法判断是「快了」还是「差很远」。
        it["ticket"] = _build_ticket(it, dec, c, fees_c, price)
        items.append(it)

        # 方向对错与盈亏**分开报**（借鉴 TradingAgents performance.py 的教训）
        if cur_ro is not None and it.get("pending") and it.get("anchor_price"):
            it["direction_check"] = t_direction_result(
                cur_ro, code, "t_sell", float(it["anchor_price"]),
                int(c.get("max_t_per_day") or 3))
    would = [i for i in items if i["action"] in ("t_sell", "t_buy")]
    da = direction_accuracy(cur_ro) if cur_ro is not None else {
        "hits": 0, "total": 0, "rate": None, "note": "无读连接"}
    return {"items": items, "scanned": len(items), "would_act": len(would),
            "read_only": True,
            "direction_accuracy": da,
            "note": "实盘建议由你本人手动执行，本接口只读不写、不会下任何单"}


def t_direction_result(cur, code: str, leg: str, anchor: float,
                       hold_days: int = 3) -> dict:
    """事后判定**一次做T的方向对不对**，与「赚没赚钱」严格分开。

    借鉴 TradingAgents-astock `performance.py` 的一条血泪教训（该文件
    `GroupStats` 上方注释原文）：

        「衡量判断准不准的是 direction_accuracy。
          v0.5.2 曾把前者叫『胜率』，导致给 Sell 之后股价下跌（**判断正确**）
          被记成失败。」

    做T 尤其要分清：
      - **方向对错**：高抛后价格真的跌了 / 低吸后价格真的涨了
      - **盈亏**：这一轮实际赚了多少（受持仓大小、手续费、时机影响）

    两者可以完全背离 —— 方向判对了但因为接回早了半根K线，仍然亏钱；
    方向判错了但恰好碰上大盘反弹，反而赚了。**只统计盈亏会把方向对��的
    决策算成失败，然后照着错的方向继续调参。**

    `leg='t_sell'` → 方向对了 = 后续价格**跌破** anchor
    `leg='t_buy'`  → 方向对了 = 后续价格**涨过** anchor
    数据不足返回 `ok=False`，**不猜**。
    """
    out = {"ok": False, "leg": leg, "anchor": anchor, "verdict": "",
           "ret_pct": None, "note": "数据不足，判定不了方向"}
    if not anchor or float(anchor) <= 0:
        return out
    anchor = float(anchor)
    try:
        cur.execute("""SELECT close FROM sa_market_kline
                       WHERE code=%s AND close IS NOT NULL
                       ORDER BY trade_date DESC LIMIT %s""",
                    (code, max(int(hold_days or 1), 1) + 1))
        rows = [r[0] for r in cur.fetchall()]
    except Exception as exc:              # noqa: BLE001
        out["note"] = "读K线失败：%s" % str(exc)[:80]
        return out
    if len(rows) < 2:
        return out
    latest = float(rows[0])
    out["ret_pct"] = round((latest / anchor - 1) * 100, 4)
    out["ok"] = True
    if leg == "t_sell":
        out["verdict"] = "对" if latest < anchor else "错"
        out["note"] = ("高抛 %s 之后价格%s %s（%s%%）" % (
            fnum(anchor), "跌破" if latest < anchor else "涨回",
            fnum(latest), fnum(out["ret_pct"])))
    elif leg == "t_buy":
        out["verdict"] = "对" if latest > anchor else "错"
        out["note"] = ("低吸 %s 之后价格%s %s（%s%%）" % (
            fnum(anchor), "涨过" if latest > anchor else "未涨回",
            fnum(latest), fnum(out["ret_pct"])))
    else:
        out["note"] = "未知腿类型 %r" % leg
    return out


def direction_accuracy(cur, hold_days: int = 3) -> dict:
    """全部做T腿的**方向正确率**（Hold/未成交的腿不计）。

    与盈亏统计**分开报**。返回 {"hits","total","rate"}。
    """
    hits = total = 0
    try:
        cur.execute("""SELECT code, side, price, trade_date FROM sa_paper_trades
                       WHERE reasoning LIKE %s AND side IN ('buy','sell')
                       ORDER BY trade_date""", (T_TAG + "%",))
        rows = cur.fetchall()
    except Exception:                      # noqa: BLE001
        return {"hits": 0, "total": 0, "rate": None,
                "note": "读做T流水失败"}
    for code, side, px, _d in rows:
        leg = "t_sell" if side == "sell" else "t_buy"
        r = t_direction_result(cur, code, leg, float(px or 0), hold_days)
        if not r.get("ok"):
            continue
        total += 1
        if r["verdict"] == "对":
            hits += 1
    return {"hits": hits, "total": total,
            "rate": round(hits / total * 100, 2) if total else None,
            "note": "方向正确率 = 卖出后跌/买入后涨的占比；与盈亏分开统计"}


def _build_ticket(it: dict, dec: dict, c: dict, fees_c, price: float) -> dict:
    """三种状态都给执行单：可做 / 待接回 / 不可做（并说清差多少）。"""
    mv = int(it.get("movable") or 0)
    thr = float(dec.get("threshold") or 0)
    base_shares = int(it.get("base_shares") or 0)
    args = (it.get("code"), it.get("name"), dec.get("action"), price, mv, thr, c,
            fees_c, base_shares, dec.get("bias_label", ""), dec.get("bias_why", ""),
            it.get("invalidate", ""))
    t = execution_ticket(*args)
    if dec.get("action") in ("t_sell", "t_buy"):
        t["status"] = "ready"
        return t
    # 不可做：给出「差多少」，否则用户无法判断该等还是该放弃
    t["status"] = "blocked"
    edge = float(dec.get("edge_pct") or 0)
    if mv < 1:
        t["blocked_reason"] = (
            "机动仓为 0：持仓 %d 股 × %s%% = %d 股，向下取整后不足 1 个申报单位"
            % (it.get("shares") or 0, fnum(c.get("t_ratio") or 0, "%.0f"),
               int((it.get("shares") or 0) * float(c.get("t_ratio") or 0) / 100.0)))
        t["blocked_hint"] = ("仓位太小做不了T。要么不动，要么等加仓到机动仓 ≥ 1 手"
                             "（当前需 ≥ %d 股）"
                             % _need_shares_for_t(it.get("code") or "000000",
                                                  c.get("t_ratio") or 30))
        return t
    if not it.get("price"):
        t["blocked_reason"] = "取不到实时价格，不给建议（不用陈旧价替代）"
        return t
    # ⚠️ hold 时 decide() 提前 return，threshold 是 0 —— 必须**按同一套公式重算**
    #    「如果要做，需要的门槛是多少」，否则单子上只有一句「不做」，
    #    用户无法判断是「快了」还是「差很远」（2026-10-11 实测 29/30 条如此）。
    need_thr = _would_threshold(it, dec, c, fees_c, price)
    t["threshold_pct"] = round(need_thr, 4)
    t["cost_pct"] = round(cost_for_amount(it.get("code") or "", price * mv, c, fees_c), 4)
    ap = dec.get("anchor_price")
    layer = dec.get("layer") or ""
    if ap and layer:
        gap = (price / float(ap) - 1) * 100.0
        t["blocked_reason"] = (
            "现价较 %s 基准 %s 低 %.2f%%（是往下不是往上）—— 方向不对"
            % (layer, fnum(ap), abs(gap)) if gap < 0 else
            "偏离 %s%% < 需 %s%%（= 往返成本 %s%% × %.1f）"
            % (fnum(edge), fnum(need_thr), fnum(t["cost_pct"]), _safety_mult(c)))
        if gap > 0:
            t["trigger_price"] = round(float(ap) * (1 + need_thr / 100.0), 4)
            t["blocked_hint"] = "涨到 %s 才够门槛（还差 %.2f%%）" % (
                t["trigger_price"], need_thr - edge)
        else:
            t["blocked_hint"] = "这是往基准方向跌，不构成高抛条件"
    elif dec.get("bias") == "flat":
        t["blocked_reason"] = "方向判不出（均线排列与 VWAP 都不明确）——宁可不做"
        t["blocked_hint"] = "等均线排列明确、且价格站上/跌破 VWAP 后再判"
    elif dec.get("bias") == "bear" and dec.get("stabilizing") is False:
        t["blocked_reason"] = "下行方向且未确认止跌 —— 不接下跌途中的下跌"
        t["blocked_hint"] = "等不再创新低且重心上移再考虑（见止跌判据）"
    else:
        t["blocked_reason"] = dec.get("why") or "不满足做T条件"
        t["blocked_hint"] = "门槛 %s%%（= 往返成本 %s%% × %.1f）" % (
            fnum(need_thr), fnum(t["cost_pct"]), _safety_mult(c))
    return t


def _would_threshold(it: dict, dec: dict, c: dict, fees_c, price: float) -> float:
    """按 decide() 的**同一套公式**重算门槛（hold 分支没设 threshold 时用）。

    必须同源：两处各写一遍公式，迟早漂移 —— 漂移的后果是页面说「需 1.70%」
    而引擎实际用 0.85%，用户永远不知道自己差多少。
    """
    code = it.get("code") or ""
    mv = int(it.get("movable") or 0)
    mult = float(dec.get("sell_mult") or 1.0)
    base = float(c.get("min_edge_pct") or 0.45) * mult
    if mv >= 1 and price:
        cost = cost_for_amount(code, price * mv, c, fees_c)
        return max(base, cost * _safety_mult(c))
    return base


def _need_shares_for_t(code: str, t_ratio: float) -> int:
    """机动仓要够 1 个申报单位，底仓至少 1 个申报单位，总持仓至少多少股。"""
    lot = 1 if PT.is_star_market(code) else 100
    return int(2 * lot / max(float(t_ratio or 30) / 100.0, 1e-9))


def _invalidate(dec: dict, bias, price: float, vwap) -> str:
    """**什么信号说明这个方向错了。** 用户手动操作，这段比结论更重要。"""
    if not bias:
        return "方向判不出时，任何操作都该重新评估"
    up = "跌破 VWAP %.4f 且均线转为空头排列" % float(vwap) if vwap else "均线转为空头排列"
    dn = "站上 VWAP %.4f 且均线转为多头排列" % float(vwap) if vwap else "均线转为多头排列"
    if dec.get("action") == "t_sell":
        return ("若价格继续放量上行（%s），说明不该抛 —— 卖在启动位是最贵的错；"
                "此时底仓 %.0f 股仍持有，不是空仓" % (
                    dn, float(dec.get("held") or 0) - float(dec.get("movable") or 0)))
    if dec.get("action") == "t_buy":
        return ("若价格不跌反涨（%s），说明抛早了、接回价定高了 —— "
                "宁可晚接也不要接在半山腰" % up)
    if bias == "bear":
        return "若出现放量企稳+%s，可重评（当前按下行处理：反弹减、不接下跌途中的下跌）" % up
    if bias == "bull":
        return "若%s，当前按上行处理（先接回、涨透再抛）可能过早减仓" % up
    return "价格方向明确后再动手；此刻震荡，任一方向都不划算"


def real_anchor(cur, code: str) -> dict | None:
    """实盘最近一次做 T 成交（用户用「记一次做T」录入，note 前缀 `[做T]`）。

    没有记录就返回 None —— 此时用持仓成本当锚。
    """
    cur.execute("""SELECT side, price, shares, trade_date
                   FROM sa_trades
                   WHERE code=%s AND note LIKE %s
                   ORDER BY trade_date DESC, id DESC LIMIT 1""", (code, T_TAG + "%"))
    r = cur.fetchone()
    if not r:
        return None
    if not isinstance(r, dict):
        r = dict(zip(("side", "price", "shares", "trade_date"), r))
    try:
        return {"side": r["side"], "price": float(r["price"] or 0),
                "shares": int(r["shares"] or 0),
                "trade_date": str(r["trade_date"])[:10]}
    except (TypeError, ValueError):
        return None


def scan_once(deps: dict, conf: dict, slot: str = "", today: str = "",
              max_codes: int = 0) -> dict:
    """扫一遍持仓，做 T 决策并**真实成交**。

    必须在 check_stop_loss **之后**调用 —— 止损优先是硬纪律，见模块头。
    """
    c = _conf(conf)
    if not c.get("enabled"):
        return {"enabled": False, "acted": 0, "items": []}
    get_conn = deps["get_conn"]
    em_kline_fn = deps.get("em_kline_fn")
    tx_symbol_fn = deps.get("tx_symbol_fn") or MD.tx_symbol
    today = today or datetime.now().strftime("%Y-%m-%d")
    fees_c = deps.get("conf", {}).get("fees")

    acted, items = 0, []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT DISTINCT code FROM sa_paper_trades
                           WHERE side IN ('buy','sell') AND status <> 'skipped'""")
            codes = [r[0] for r in cur.fetchall()]
    except Exception as exc:              # noqa: BLE001
        return {"enabled": True, "acted": 0, "items": [],
                "error": "读持仓失败: %s" % str(exc)[:120]}

    for code in (codes[:max_codes] if max_codes else codes):
        dec = {"code": code, "action": "error", "why": ""}
        try:
            dec = _judge_one(deps, c, code, today, em_kline_fn, tx_symbol_fn, fees_c)
            if dec.get("action") in ("t_sell", "t_buy"):
                dec = _execute_one(deps, c, code, today, slot, dec,
                                   dec.get("name", ""))
            if dec.get("action") in ("t_sell", "t_buy") and dec.get("executed"):
                acted += 1
        except Exception as exc:          # noqa: BLE001
            # 单只票失败绝不能带走整轮 —— 15 只票里 1 只取不到分时，
            # 其余 14 只的做 T 判据仍然有效。
            dec = {"code": code, "action": "error", "why": str(exc)[:140]}
            print(f"[t] {code} 扫描异常: {exc}", flush=True)
        dec.pop("_bars_daily", None)
        dec.pop("_quote", None)
        items.append(dec)
    return {"enabled": True, "acted": acted, "items": items, "scanned": len(codes)}

    # ---- 真实成交：复用 _execute_decision 的全部资金/T+1/时段闸门 ----
    action = "sell" if dec["action"] == "t_sell" else "buy"
    decision = {"action": action, "confidence": 5,
                "target_value_pct": 1, "stop_loss_pct": 2,
                "expected_up_pct": 1,
                "reasoning": "%s%s" % (T_TAG, dec["why"])}
    stock = {"code": code, "name": name or code}
    executed, note = PT._execute_decision(
        deps, deps.get("conf") or c, stock, today, decision, quote,
        slot=PT.make_slot(slot, 2), bars=bars_daily,
        shares_override=dec["shares"])
    dec["executed"] = bool(executed)
    dec["note"] = note
    dec["slot"] = PT.make_slot(slot, 2)
    if executed:
        print(f"[t] {code} {dec['action']} {dec['shares']}股 | {dec['why']} | {note}",
              flush=True)
    else:
        print(f"[t] {code} {dec['action']} 未成交 | {dec['why']} | {note}", flush=True)
    return dec


def status(deps: dict, conf: dict) -> dict:
    """给前端看的当前状态：每只持仓的机动仓、锚定价、各层偏离。"""
    c = _conf(conf)
    if not c.get("enabled"):
        return {"enabled": False, "items": []}
    get_conn = deps["get_conn"]
    out = []
    with get_conn() as conn:
        cur = conn.cursor()
        positions = PT._derive_paper_positions(cur)
        for code, p in sorted(positions.items()):
            mv = movable_shares(code, int(p.get("shares") or 0), c)
            a = t_anchor(cur, code)
            out.append({
                "code": code, "name": p.get("name", ""),
                "shares": int(p.get("shares") or 0), "cost": p.get("cost"),
                "movable": mv, "base_shares": int(p.get("shares") or 0) - mv,
                "t_ratio": c.get("t_ratio"),
                "anchor": a, "pending": (a or {}).get("side") == "sell",
            })
    return {"enabled": True, "items": out,
            "min_edge_pct": c.get("min_edge_pct"),
            "max_t_per_day": c.get("max_t_per_day")}