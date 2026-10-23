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

import json
import math
import time
import uuid
from datetime import datetime, timedelta

import psycopg2.extras

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
    # 判断落库（2026-10-18 用户要求）：每轮 advisory 的全部判断（含 hold/不做）
    # 写入 sa_t_advisory_log，让「为什么其他股票不适合做T」可回看。
    # 这是观测不是交易口径，所以默认开 —— 不改任何决策内容。
    "log_enabled": True,
    # 趋势减仓 / 提前抛（2026-10-22 用户授权新增）。
    # **只给建议、不成交**：`scan_once` / `decide` 一个字节都没动，卖出仍由
    # 用户本人手动执行。为什么要有：做 T 是「持仓不变的份额置换」，趋势走坏时
    # 它无能为力 —— 智谱 / MINIMAX 那种一路下行的票，要的是**先减一部分仓位**，
    # 而不是等反弹做 T。默认关：它会建议真实减仓，属交易口径变更。
    "trend_reduce": {
        "enabled": False,        # 总开关（默认关）
        "confirm_days": 2,       # 连续多少个交易日满足「收盘<MA20 且 MA20<MA60」
        "reduce_pct": 33,        # 建议减掉现有持仓的百分比
        "cooldown_days": 10,     # 两次减仓建议之间至少间隔的交易日
        "rearm_days": 5,         # 连续收盘站回 MA20 多少天后重新武装
        "max_reductions": 2,     # 同一段下跌最多建议减几次
        "fresh_days": 2,         # 信号必须落在最近多少个交易日内才算有效
        "hard_stop_pct": 10,     # 新近跌破成本 x% -> 硬止损建议
        "deep_dd_pct": 25,       # 浮亏超过 x% 不再催卖，只提示（避免卖在坑里）
        "lookback_days": 180,    # 回放窗口（交易日）
    },
}

# T 操作的标记前缀。用来区分「做 T 的成交」和「普通 LLM 决策的成交」，
# 因为做 T 的锚定价要沿着自己的成交链走，不能被普通买卖干扰。
T_TAG = "[做T]"

# 动作枚举。趋势减仓是**单向卖出**（不设接回价），与做 T 的往返是两回事，
# 所以单独分组 —— 日志统计、推送、页面徽标都按这个分组走，
# 任何「只要 t_sell/t_buy」的地方都必须同时问一句趋势动作算不算（漏了会静默少计）。
T_ACTIONS = ("t_sell", "t_buy")
TREND_ACTIONS = ("hard_stop", "trend_reduce")
ACT_ACTIONS = T_ACTIONS + TREND_ACTIONS
# trend_wait 只提示不催卖（深亏时割肉容易卖在坑里），不算可执行动作。
TREND_LABELS = {"hard_stop": "止损（卖可卖份额）",
                "trend_reduce": "趋势减仓（单向卖出）",
                "trend_wait": "深亏提示（只提示不催卖）"}


def _conf(raw: dict | None) -> dict:
    c = dict(T_DEFAULTS)
    c.update(raw or {})
    lay = dict(T_DEFAULTS["layers"])
    lay.update((raw or {}).get("layers") or {})
    c["layers"] = lay
    # 嵌套段必须显式深合并：config.yaml 里只写 trend_reduce.enabled 时，
    # 浅 update 会让其余阈值全部丢失（与 mp.auth 那次是同一类坑）。
    tr = dict(T_DEFAULTS["trend_reduce"])
    sub = (raw or {}).get("trend_reduce")
    if isinstance(sub, dict):
        tr.update(sub)
    c["trend_reduce"] = tr
    return c


def fnum(v, spec="%.2f", dash="—"):
    """所有数值的格式化出口。None/非数值/NaN/Inf -> 破折号。

    AGENTS.md 记过一次真事故：15 只持仓跑完 90 分钟 LLM，最后写报告时
    `f"{r.get('price'):.2f}"` 遇到 None 抛 TypeError，成果全丢。
    渲染层不该有能力弄丢数据。

    ⚠️ `math.isfinite` 这道闸**不是**多余的。`"%.2f" % float("nan")` 不抛
       异常，直接返回字符串 `"nan"` —— 于是 `why` 文案里会出现「现价 nan」，
       而 except 分支永远拦不到它（2026-10-12 离线断言逮到）。Inf 同理
       会输出 `inf`。这两类值必须在**格式化之前**拒掉。
    """
    try:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return dash
        if not math.isfinite(v):          # NaN / ±Inf：宁可显示破折号，也不显示 nan
            return dash
        return spec % v
    except (TypeError, ValueError):
        return dash


# 价格专用：4 位小数，与前端 tPx 同精度。
# ⚠️ 价格和百分比**不能**共用 fnum 的默认 2 位 —— 1.515 会被拼成「现价 1.52」，
#    用户照着挂单挂在错位置（与 index.html 的 tPx 注释是同一个坑，2026-10-12 用户报）。
#    百分比 2 位是够的（门槛/成本本来就是 0.85% 这个量级），所以默认不动，
#    只有价格走 FMT_PX。
FMT_PX = "%.4f"


def fpx(v, dash="—"):
    """价格出口：4 位小数。语义 = 「这是钱的单价」，调用方别拿它格式化百分比。"""
    return fnum(v, FMT_PX, dash)


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


def _ma_series(vals: list[float], n: int) -> list[float | None]:
    """简单移动平均，前 n-1 个为 None（不够样本就不给值，不拿部分样本冒充）。"""
    out: list[float | None] = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _clean_daily_bars(bars: list[dict]) -> list[dict]:
    """日 K 归一：只留 {date, close}，按日期升序去重（同一天取最后一条）。

    akshare / 腾讯 / 101 服务返回的 date 可能是 datetime / date / str，
    close 可能是 Decimal 或字符串。**不归一就排序**会出现「10-9 排在 10-10 后面」
    这种字符串序问题，MA 全错且不报错。
    """
    seen: dict[str, float] = {}
    for b in (bars or []):
        if not isinstance(b, dict):
            continue
        d = str(b.get("date") or "")[:10]
        try:
            close = float(b.get("close"))
        except (TypeError, ValueError):
            continue
        if len(d) != 10 or not math.isfinite(close) or close <= 0:
            continue
        seen[d] = close
    return [{"date": d, "close": seen[d]} for d in sorted(seen)]


def _trend_shares(code: str, held: int, reduce_pct: float,
                  sellable: int | None, lot: int) -> tuple[int, int]:
    """(建议股数, 名义股数)。减仓按整手向下取整，且至少保留 1 手。

    取整不是格式问题：A 股买入/卖出申报单位是 100 股（科创板 1 股起），
    建议 66 股这种数在券商端根本下不出去。名义股数单独返回，是为了在
    「不足 1 手」时能说清「本来想减多少」，而不是只显示一个 0。
    """
    held = max(0, int(held or 0))
    lot = max(1, int(lot or 1))
    raw = int(held * max(0.0, float(reduce_pct or 0)) / 100.0)
    if held < lot or raw < lot:
        return 0, raw
    n = int(raw // lot) * lot
    n = min(n, held - lot)                # 底仓至少留 1 手
    if sellable is not None:
        cap = max(0, min(int(sellable), held))
        n = min(n, cap)
        n = int(n // lot) * lot
    return (n if n >= lot else 0), raw


def trend_reduce_state(bars: list[dict], conf: dict | None = None, *,
                       code: str = "", cost: float = 0.0, held: int = 0,
                       sellable: int | None = None, price: float = 0.0,
                       first_date: str = "", today: str = "",
                       quote_date: str = "", t_plus_0: bool = False) -> dict:
    """趋势减仓 / 提前抛 / 硬止损（**纯函数，只产建议，不成交**）。

    用户 2026-10-22 授权时给的三个候选判据（跌破 MA20 / MA20 下穿 MA60 /
    跌破成本 x%）在这里合成一条：**收盘 < MA20 且 MA20 < MA60**（比单看
    「跌破 MA20」多一层趋势确认，震荡市里的单日破位不会触发），
    再加一条**独立**的硬止损（新近跌破成本 x%）。

    为什么必须做「回放」而不是只看今天
    ----------------------------------
    只看今天会犯两种错：① 破位第 5 天才提示，价格已经跌透；
    ② 每轮都提示同一个信号，等于噪音。所以从建仓日 `first_date` 起逐日回放，
    信号只在**触发当天及之后 fresh_days 个交易日**有效，一段下跌只发一次，
    要等连续 `rearm_days` 天站回 MA20 才重新武装，两次之间还有 `cooldown_days`。

    深亏保护（`deep_dd_pct`）
    -------------------------
    浮亏超过阈值时趋势信号只作提示（`trend_wait`），不催卖 —— 一路阴跌 40%
    后再叫人割肉，多半割在阶段低点。这是**提示不是豁免**：止损线以内仍会
    正常给建议，用户自己决定。

    返回的 `shares` 已扣除 T+1 锁定（`sellable` 由调用方从 `sa_trades` 算），
    并按申报单位取整。`shares=0` 时 `why` 里必须写清是哪一条挡住了
    （锁定 / 不足 1 手 / 底仓只剩 1 手），否则用户只看到 0 无法判断该怎么办。
    """
    # `conf` 两种形态都要认：完整 T 配置（含 trend_reduce 子段）或**已经合并好的
    # 子段本身**。只认前一种的话，调用方传子段时 `_conf()` 会去子段里找
    # "trend_reduce" 找不到 → 静默回落到默认值（enabled=False），
    # 表现为「页面开了开关但永远不触发」，且不报任何错。
    raw = conf or {}
    if "trend_reduce" in raw:
        tr = _conf(raw).get("trend_reduce") or {}
    else:
        tr = dict(T_DEFAULTS["trend_reduce"])
        tr.update(raw)
    out: dict = {
        "enabled": bool(tr.get("enabled")), "action": "none", "shares": 0,
        "shares_raw": 0, "lot": lot_size(code), "sellable": sellable,
        "ma20": None, "ma60": None, "close": None,
        "streak": 0, "fires": 0, "last_fire_date": "", "last_fire_price": None,
        "hard_stop_price": None, "hard_stop_date": "", "hard_stop_fresh": False,
        "pnl_pct": None, "deep_dd": False, "why": "", "invalidate": "",
        "truncated": False, "window_cut": False, "partial_window": False,
        "bars_used": 0, "window_start": "", "synthetic_today": False,
        "confirm_days": 0, "reduce_pct": 0.0, "rearm_days": 0,
        "cooldown_days": 0, "max_reductions": 0, "fresh_days": 0,
        "t_plus_0": bool(t_plus_0),
    }
    if not tr.get("enabled"):
        out["why"] = "趋势减仓未启用（t_trading.trend_reduce.enabled = false）"
        return out

    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(tr.get(key) if tr.get(key) is not None
                                       else default)))
        except (TypeError, ValueError):
            return default

    confirm_days = _int("confirm_days", 2, 1, 20)
    rearm_days = _int("rearm_days", 5, 1, 60)
    cooldown_days = _int("cooldown_days", 10, 0, 250)
    max_reductions = _int("max_reductions", 2, 0, 20)
    fresh_days = _int("fresh_days", 2, 1, 30)
    lookback = _int("lookback_days", 180, 80, 800)
    try:
        reduce_pct = max(0.0, min(100.0, float(tr.get("reduce_pct")
                                              if tr.get("reduce_pct") is not None
                                              else 33)))
    except (TypeError, ValueError):
        reduce_pct = 33.0
    try:
        hard_stop_pct = max(0.0, min(90.0, float(tr.get("hard_stop_pct")
                                                if tr.get("hard_stop_pct") is not None
                                                else 10)))
    except (TypeError, ValueError):
        hard_stop_pct = 10.0
    try:
        deep_dd_pct = max(0.0, min(100.0, float(tr.get("deep_dd_pct")
                                              if tr.get("deep_dd_pct") is not None
                                              else 25)))
    except (TypeError, ValueError):
        deep_dd_pct = 25.0
    out.update({"confirm_days": confirm_days, "rearm_days": rearm_days,
                "cooldown_days": cooldown_days,
                "max_reductions": max_reductions, "fresh_days": fresh_days,
                "reduce_pct": reduce_pct})

    all_bars = _clean_daily_bars(bars)
    if not all_bars:
        out["why"] = "取不到日K，趋势判不出（不用分时冒充日线趋势）"
        return out
    ev = all_bars[-lookback:]
    out["window_cut"] = len(all_bars) > len(ev)
    out["bars_used"] = len(ev)
    out["window_start"] = ev[0]["date"]

    fd = str(first_date or "")[:10]
    out["partial_window"] = bool(fd) and ev[0]["date"] > fd
    out["truncated"] = bool(out["window_cut"] or out["partial_window"])
    if fd and ev[-1]["date"] < fd:
        out["why"] = ("日K 最新一根 %s 早于建仓日 %s，无法回放趋势"
                      % (ev[-1]["date"], fd))
        return out

    # 「今日」用实时价补一根，否则盘中永远只能看到昨天的收盘判断 ——
    # 那正是「跌破 MA20 了却没提示」的成因。只补一次，且必须有明确的今日依据：
    # 行情自带 updated_at 时必须等于今天；没有时只接受工作日且离上一根 ≤10 天
    # （节假日没有行情，补一根会造出假收盘）。
    td = str(today or "")[:10]
    qd = str(quote_date or "")[:10]
    if td and ev[-1]["date"] < td and price and float(price) > 0:
        try:
            gap = (datetime.strptime(td, "%Y-%m-%d")
                   - datetime.strptime(ev[-1]["date"], "%Y-%m-%d")).days
            wd = datetime.strptime(td, "%Y-%m-%d").weekday()
        except ValueError:
            gap, wd = 99, 7
        today_ok = (qd == td) if qd else (wd < 5)
        if today_ok and 0 <= gap <= 10:
            ev = ev + [{"date": td, "close": float(price)}]
            out["synthetic_today"] = True

    n = len(ev)
    closes = [b["close"] for b in ev]
    out["close"] = round(closes[-1], 4)
    out["bars_used"] = n
    if n < 60:
        out["why"] = ("日K 只有 %d 根（<60），MA60 算不出 —— 不给趋势建议"
                      "（宁可不做，也不拿残缺均线猜）" % n)
        return out
    ma20 = _ma_series(closes, 20)
    ma60 = _ma_series(closes, 60)
    out["ma20"] = round(ma20[-1], 4) if ma20[-1] is not None else None
    out["ma60"] = round(ma60[-1], 4) if ma60[-1] is not None else None

    # 回放从建仓日开始（更早的 K 线只当均线预热，不参与信号计数）。
    start_i = 59
    if fd:
        for i, b in enumerate(ev):
            if b["date"] >= fd:
                start_i = max(59, i)
                break

    # ---- 硬止损：只认「新近下穿」，不认「一直在水下」 ----
    hs_price = round(cost * (1 - hard_stop_pct / 100.0), 4) if cost > 0 else None
    out["hard_stop_price"] = hs_price
    hs_cross_i = None
    if hs_price is not None:
        for i in range(max(1, start_i), n):
            if closes[i] < hs_price <= closes[i - 1]:
                hs_cross_i = i
    hs_fresh = (hs_cross_i is not None
                and (n - 1 - hs_cross_i) <= fresh_days - 1
                and closes[-1] < hs_price)
    out["hard_stop_fresh"] = bool(hs_fresh)
    if hs_cross_i is not None:
        out["hard_stop_date"] = ev[hs_cross_i]["date"]

    # ---- 趋势回放：一段下跌只发一次，站回 MA20 才重新武装 ----
    armed = True
    fires = 0
    last_fire_i = None
    cond_streak = 0
    above_run = 0
    for i in range(start_i, n):
        if ma20[i] is None or ma60[i] is None:
            continue
        cond = closes[i] < ma20[i] < ma60[i]
        cond_streak = cond_streak + 1 if cond else 0
        above_run = above_run + 1 if closes[i] > ma20[i] else 0
        if not armed and above_run >= rearm_days:
            armed = True
            above_run = 0
        if (armed and cond and cond_streak >= confirm_days
                and fires < max_reductions
                and (last_fire_i is None
                     or i - last_fire_i >= cooldown_days)):
            fires += 1
            last_fire_i = i
            armed = False
            above_run = 0
    out["fires"] = fires
    out["streak"] = cond_streak
    if last_fire_i is not None:
        out["last_fire_date"] = ev[last_fire_i]["date"]
        out["last_fire_price"] = round(closes[last_fire_i], 4)

    cur_cond = bool(ma20[-1] is not None and ma60[-1] is not None
                    and closes[-1] < ma20[-1] < ma60[-1])
    fresh_fire = (last_fire_i is not None
                  and (n - 1 - last_fire_i) <= fresh_days - 1
                  and cur_cond)
    ref_price = float(price) if price and float(price) > 0 else closes[-1]
    pnl_pct = round((ref_price / cost - 1) * 100, 3) if cost > 0 else None
    out["pnl_pct"] = pnl_pct
    deep_dd = bool(pnl_pct is not None and pnl_pct <= -deep_dd_pct)
    out["deep_dd"] = deep_dd
    lot = out["lot"]

    if hs_fresh:
        want = min(int(held or 0),
                   int(sellable) if sellable is not None else int(held or 0))
        out.update({"action": "hard_stop", "shares": max(0, want),
                    "shares_raw": max(0, want),
                    "why": ("收盘 %s 跌破成本止损线 %s（成本 %s × (1-%s%%），"
                            "下穿发生在 %s —— 建议卖出 %d 股"
                            % (fpx(closes[-1]), fpx(hs_price),
                               fpx(cost), fnum(hard_stop_pct, "%.0f"),
                               ev[hs_cross_i]["date"], max(0, want))),
                    "invalidate": (
                        "若收盘重新站回 %s 之上，说明这次下穿可能是假摔；"
                        "但止损是纪律，不因一次反抽推翻" % fpx(hs_price))})
        if sellable is not None and want < int(held or 0):
            out["why"] += "（T+1 锁定，今日可卖 %d / 持仓 %d）" % (want, held)
        return out

    if fresh_fire:
        nsh, raw = _trend_shares(code, held, reduce_pct, sellable, lot)
        head = ("收盘 %s < MA20 %s，且 MA20 < MA60 %s，已连续 %d 个交易日；"
                "这是建仓以来第 %d/%d 次减仓信号（信号日 %s）"
                % (fpx(closes[-1]), fpx(ma20[-1]), fpx(ma60[-1]),
                   cond_streak, fires, max_reductions, ev[last_fire_i]["date"]))
        if deep_dd:
            out.update({
                "action": "trend_wait", "shares": 0,
                "why": ("%s。但浮亏 %s%% 已超过深亏阈值 %s%% —— "
                        "此处割肉容易卖在阶段低点，**只提示不催卖**，"
                        "要不要减由你决定" % (
                            head, fnum(pnl_pct), fnum(deep_dd_pct, "%.0f"))),
                "invalidate": ("若继续加速下跌，风险仍在；若放量站回 MA20 %s "
                               "并连续 %d 天，趋势条件失效" % (
                                   fpx(ma20[-1]), rearm_days)),
            })
            return out
        if nsh <= 0:
            if raw <= 0:
                reason = "没有可减份额（持仓 %d 股，%s%% = 0 股）" % (
                    held, fnum(reduce_pct, "%.0f"))
            elif held < lot:
                reason = "持仓 %d 股不足 1 个申报单位（%d 股）" % (held, lot)
            elif raw < lot:
                reason = ("按 %s%% 算只能减 %d 股，不足 1 个申报单位（%d 股）"
                          % (fnum(reduce_pct, "%.0f"), raw, lot))
            elif sellable is not None and int(sellable) < lot:
                reason = "T+1 锁定后今日可卖 %d 股，不足 1 个申报单位" % int(sellable)
            else:
                reason = "底仓至少保留 1 个申报单位（%d 股），当前无可减份额" % lot
            out.update({
                "action": "trend_reduce", "shares": 0, "shares_raw": raw,
                "why": "%s —— 但%s，这一轮给不出可执行股数" % (head, reason),
                "invalidate": ("若收盘重新站上 MA20 %s 并连续 %d 个交易日，"
                               "减仓建议作废" % (fpx(ma20[-1]), rearm_days)),
            })
            return out
        out.update({
            "action": "trend_reduce", "shares": nsh, "shares_raw": raw,
            "why": ("%s —— 建议减 %d 股（约 %s%%），保留 %d 股"
                    % (head, nsh, fnum(nsh / held * 100 if held else 0, "%.0f"),
                       max(0, held - nsh))),
            "invalidate": ("若收盘重新站上 MA20 %s 并连续 %d 个交易日，"
                           "减仓建议作废" % (fpx(ma20[-1]), rearm_days)),
        })
        if sellable is not None and int(sellable) < nsh:
            out["why"] += "（已按 T+1 可卖 %d 股封顶）" % int(sellable)
        if out["partial_window"]:
            out["why"] += "（注意：日K 从 %s 开始，未覆盖建仓日 %s 之前）" % (
                ev[0]["date"], fd)
        return out

    # ---- 无可执行信号：把「差在哪」说清楚，别只给一句「不做」 ----
    if last_fire_i is not None:
        out["why"] = ("趋势信号曾在 %s 触发（第 %d 次），已超出 %d 个交易日的"
                      "有效期或有价格站回 MA20，本轮不再提示"
                      % (ev[last_fire_i]["date"], fires, fresh_days))
    elif cur_cond:
        out["why"] = ("收盘 %s < MA20 %s 且 MA20 < MA60，已连续 %d/%d 个交易日，"
                      "还差 %d 天确认" % (
                          fpx(closes[-1]), fpx(ma20[-1]), cond_streak,
                          confirm_days, max(0, confirm_days - cond_streak)))
    else:
        out["why"] = ("未满足「收盘<MA20 且 MA20<MA60」：收盘 %s，MA20 %s，MA60 %s"
                      % (fpx(closes[-1]), fpx(ma20[-1]), fpx(ma60[-1])))
    if out["partial_window"]:
        out["why"] += "（日K 从 %s 开始，未覆盖建仓日 %s 之前）" % (ev[0]["date"], fd)
    out["invalidate"] = "—"
    return out


def real_sellable_shares(cur, code: str, held: int, today: str,
                         t_plus_0: bool = False) -> int:
    """**实盘** T+1 可卖股数。

    ⚠️ 不能用 `PT.sellable_shares()` —— 它读的是 `sa_paper_trades`（模拟盘），
       与 `sa_trades`（实盘）是两张完全不同的表。混用会拿模拟盘的持仓去封顶
       实盘建议，**看起来有数字、实际毫不相干**（本模块已因同类“同名不同源”
       踩过一次：`tx_symbol` 两份副本）。

    `held` 是 `_derive_holdings` 按**全部流水**回放出来的净持仓（含今日成交），
    所以今日买入的份额在 held 里但不可卖；今日卖出的份额已不在 held 里，
    不需要再补回。`t_plus_0`（港股/QDII 等）直接全量可卖。
    """
    held = max(0, int(held or 0))
    if held <= 0:
        return 0
    if t_plus_0:
        return held
    try:
        # 必须给聚合列起 `AS shares`：RealDictCursor 下无别名的列名是 `coalesce`，
        # `_r(r, 1, "shares")` 取不到 → bought 恒 0 → 把 T+1 锁定的份额当成可卖。
        cur.execute("""SELECT side, COALESCE(SUM(shares),0) AS shares FROM sa_trades
                       WHERE code=%s AND trade_date=%s GROUP BY side""",
                    (code, today))
        rows = cur.fetchall()
    except Exception:                      # noqa: BLE001
        # 查不到今日流水时**按最保守**处理：一分钱都不许卖。
        # 反过来（默认全可卖）会让 T+1 建议直接违规，用户照着操作会被券商拒单。
        return 0
    bought = 0
    for r in rows:
        side = _r(r, 0, "side")
        qty = _r(r, 1, "shares")
        if str(side) == "buy":
            try:
                bought += int(qty or 0)
            except (TypeError, ValueError):
                pass
    return max(0, min(held, held - bought))


def trend_ticket(code: str, name: str, action: str, price: float, shares: int,
                 conf: dict, fees_c=None, invalidate: str = "",
                 t_plus_0: bool = False, sellable: int | None = None) -> dict:
    """趋势减仓 / 硬止损的**单向卖出单**（纯函数）。

    ⚠️ **绝不能让这种单子走 `execution_ticket(legs="t_sell")`** —— 那会凭空
       生成一条「跌到 X 买回」的腿，用户照着做等于把刚减的仓位原价接回来，
       与「趋势走坏先降仓位」完全相反。所以这里没有 `buy_back` 字段，
       `legs="sell_only"` 让前端走另一套模板。

    与做 T 的差别还有一层：做 T 卖的是**机动仓**（持仓不变），这里降的是
    **总仓位**，减完不会买回。
    """
    shares = max(0, int(shares or 0))
    price = float(price or 0)
    t = {"code": code, "name": name, "legs": "sell_only", "action": action,
         "shares": shares, "price": price,
         "trigger_price": round(price, 4) if price > 0 else None,
         "status": "ready" if (shares >= 1 and price > 0) else "blocked",
         "sell": None, "net": None, "net_cash": None, "fee": None,
         "fee_pct": None, "threshold_pct": None, "safety_mult": None,
         "invalidate": invalidate, "t_plus_0": bool(t_plus_0),
         "sellable": sellable, "warn": ""}
    if shares < 1 or price <= 0:
        t["warn"] = "没有可执行股数/价格，给不出卖出单"
        return t
    value = price * shares
    fee = PT.trade_fees(code, name or "", "sell", value, fees_c or {})["total"]
    t.update({
        "sell": {"price": round(price, 4), "shares": shares,
                 "gross_cash": round(value, 2),
                 "net_cash": round(value - fee, 2),
                 "fee": round(fee, 2)},
        "net_cash": round(value - fee, 2), "fee": round(fee, 2),
        "fee_pct": round(fee / value * 100, 4) if value else None,
    })
    if t_plus_0:
        t["warn"] = "T+0 标的，卖出不受当日买入锁定限制"
    elif sellable is not None and int(sellable) < int(shares):
        t["warn"] = ("T+1：今日可卖仅 %d 股，建议股数已按其封顶"
                     % int(sellable))
    else:
        t["warn"] = "只卖不买回（趋势减仓，不是做 T）；卖完该仓位永久降低"
    return t


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
                                  stab_why, fpx(bp * (1 - thr / 100.0)),
                                  fpx(bp), fnum(thr)))
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
            reached = "" if price <= need else ("（价格已跌破接回位 %s）" % fpx(need))
            out["why"] = ("下行方向，未确认止跌，**不接回**%s：%s。"
                          "等止跌确认后再看接回位 %s（门槛 %s%%）" % (
                              reached, stab_why, fpx(need), fnum(thr)))
            return out
        if price <= need:
            if n_buy >= int(c.get("refill_max_per_day") or 1):
                out["why"] = (f"已跌破买回位 {fpx(need)}（锚价 {fpx(bp)}，"
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
                        f"高抛后回落：锚价 {fpx(bp)} → 现价 {fpx(price)}"
                        f"（{fnum(edge)}%），门槛 {fnum(thr)}%"
                        f"（{bias['label']}方向 ×{bias['buy_mult']}，"
                        f"往返成本 {fnum(cost)}%）{extra}。机动仓 {qty} 股买回"})
            return out
        out["threshold"] = round(thr, 4)
        out["edge_pct"] = round(edge, 4)
        out["anchor_price"] = bp
        out["trigger_price"] = round(need, 4)
        out["why"] = (f"待买回但未到价：锚价 {fpx(bp)}，需跌到 {fpx(need)}"
                      f"（门槛 {fnum(thr)}%，{bias['label']}方向 ×{bias['buy_mult']}），"
                      f"现价 {fpx(price)}")
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
        out["why"] = (f"未高抛：最接近的基准 {fpx(top['anchor'])}，"
                      f"现价 {fpx(price)}（{fnum(top['edge_pct'])}%），"
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
                f"{lname} 基准 {fpx(a['anchor'])}，现价 {fpx(price)}"
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
    tr = c.get("trend_reduce") or {}
    tr_on = bool(tr.get("enabled"))
    # 趋势判据要 MA60，还要从建仓日回放 —— 60 根根本不够。窗口按
    # lookback + 60 取（101 `/kline` 的 days 上限 2000，这里封 800 留足余量）。
    try:
        _lb = max(80, min(800, int(tr.get("lookback_days") or 180)))
    except (TypeError, ValueError):
        _lb = 180
    bars_days = min(800, _lb + 60) if tr_on else 60
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
        # 趋势回放的起点：建仓日之前没有「我的持仓」，那段 MA 只当预热。
        # 调用方（app._derive_holdings / calc_position）本来就给出 first_date，
        # 这里取不到就退回空串（回放从窗口起始算，会更早触发，属偏保守）。
        first_date = str(p.get("first_date") or "")[:10]
        t_plus_0 = PT.is_t_plus_0(code, name)
        it = {"code": code, "name": name, "shares": held, "cost": cost,
              "action": "hold", "shares_suggest": 0, "why": "",
              "invalidate": "", "action_label": "不做",
              "first_date": first_date, "t_plus_0": t_plus_0}
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

        anchor_info, today_t, sellable = None, [], None
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                anchor_info = real_anchor(cur, code)
                cur.execute("""SELECT side FROM sa_trades
                               WHERE code=%s AND trade_date=%s
                                 AND note LIKE %s""", (code, today, T_TAG + "%"))
                today_t = [r[0] for r in cur.fetchall()]
                if tr_on:
                    sellable = real_sellable_shares(cur, code, held, today, t_plus_0)
        except Exception as exc:          # noqa: BLE001
            it["why"] = "读做T台账失败，不给建议：%s" % str(exc)[:100]
            it["invalidate"] = "—"
            items.append(it)
            continue
        it["sellable"] = sellable

        bars_daily = []
        if em_kline_fn:
            try:
                bars_daily = PT.fetch_kline_ohlcv(em_kline_fn, tx_symbol_fn(code),
                                                  days=bars_days)
            except Exception:              # noqa: BLE001
                bars_daily = []
        intraday = None
        try:
            intraday = MD.fetch_intraday(code, "m5", 320)
        except Exception:                  # noqa: BLE001
            intraday = None

        anchors = layer_anchors(bars_daily, intraday, c)
        trend = trend_state(bars_daily)
        trend_adv = None
        if tr_on:
            trend_adv = trend_reduce_state(
                bars_daily, tr, code=code, cost=cost, held=held,
                sellable=sellable, price=price, first_date=first_date,
                today=today,
                quote_date=str(q.get("updated_at") or q.get("date") or ""),
                t_plus_0=t_plus_0)
            it["trend"] = trend_adv
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

        # 趋势规则**优先于做 T**：已经判「该减仓」的仓位，再拿其中一部分去
        # 高抛低吸是自相矛盾。这里只覆盖**建议与展示** —— decide / _execute_one
        # 一个字节没动，所以它永远不会成交（与「只给建议」的授权一致）。
        if tr_on and trend_adv:
            it["t_action"] = it["action"]        # 保留做T原判，便于对照
            ta = trend_adv.get("action") or "none"
            if ta in TREND_ACTIONS:
                old_act = it["action"]
                old_why = it.get("why") or ""
                t_shares = int(trend_adv.get("shares") or 0)
                it.update({
                    "action": ta, "shares_suggest": t_shares,
                    "why": trend_adv.get("why", ""),
                    "action_label": TREND_LABELS.get(ta, "不做"),
                    "invalidate": trend_adv.get("invalidate", ""),
                    # 做T专有的执行字段全部清掉：拿「做T门槛/锚价」去解释一笔
                    # 单向减仓，会让页面和日志同时说谎。
                    "edge_pct": None, "threshold": None, "anchor_price": None,
                    "layer": "", "movable": 0, "base_shares": held,
                    "pending": False, "stabilizing": None,
                })
                note = ""
                if old_act == "t_sell":
                    note = ("做T 本轮也判了高抛（%s%s）—— **只执行这一条，"
                            "别重复下单**；区别是趋势减仓不接回，仓位永久降低"
                            % (old_why[:60], "…" if len(old_why) > 60 else ""))
                elif old_act == "t_buy":
                    note = ("做T 本轮本来要接回，已被趋势规则覆盖：趋势走坏不接回"
                            "（原判：%s%s）" % (old_why[:60],
                                              "…" if len(old_why) > 60 else ""))
                it["override_note"] = note
                it["ticket"] = trend_ticket(
                    code, name, ta, price, t_shares, c, fees_c,
                    invalidate=it["invalidate"], t_plus_0=t_plus_0,
                    sellable=sellable)
                if note:
                    it["ticket"]["note"] = note
        items.append(it)

        # 方向对错与盈亏**分开报**（借鉴 TradingAgents performance.py 的教训）
        if cur_ro is not None and it.get("pending") and it.get("anchor_price"):
            it["direction_check"] = t_direction_result(
                cur_ro, code, "t_sell", float(it["anchor_price"]),
                int(c.get("max_t_per_day") or 3))
    # 趋势建议里 shares=0 的（T+1 锁死 / 不足 1 手 / 深亏只提示）不算可操作 ——
    # 否则「本轮可操作 5 只」会包含根本卖不出去的行，与页面上的 0 股自相矛盾。
    would = [i for i in items
             if i.get("action") in ACT_ACTIONS
             and int(i.get("shares_suggest") or 0) > 0]
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
            fpx(anchor), "跌破" if latest < anchor else "涨回",
            fpx(latest), fnum(out["ret_pct"])))
    elif leg == "t_buy":
        out["verdict"] = "对" if latest > anchor else "错"
        out["note"] = ("低吸 %s 之后价格%s %s（%s%%）" % (
            fpx(anchor), "涨过" if latest > anchor else "未涨回",
            fpx(latest), fnum(out["ret_pct"])))
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
            % (layer, fpx(ap), abs(gap)) if gap < 0 else
            "偏离 %s%% < 需 %s%%（= 往返成本 %s%% × %.1f）"
            % (fnum(edge), fnum(need_thr), fnum(t["cost_pct"]), _safety_mult(c)))
        if gap > 0:
            t["trigger_price"] = round(float(ap) * (1 + need_thr / 100.0), 4)
            t["blocked_hint"] = "涨到 %s 才够门槛（还差 %.2f%%）" % (
                fpx(t["trigger_price"]), need_thr - edge)
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


# ---------------- 判断日志（2026-10-18 用户要求）----------------
#
# 用户原话的痛点：「往往只见到一个两个建议」—— 后台线程每 15 分钟把全部持仓
# 都判了一遍，但只有达到门槛的那一两只被推送，其余 hold/被拦的判断**算完就丢**。
# 于是「为什么其他股票不适合做T」永远无处可查，做T 的历史判断也没法复盘
# （哪只票 hold 了很多天后终于触发？高抛的方向对过几次？）。
#
# 落库点在**调用方**（app.py 的后台线程 + /api/t/advisory），不在 advisory()
# 本体 —— advisory 的契约是「纯只读，一个字节都不写」，写失败也绝不能影响
# 建议本身（persist 内部吞异常）。结构化列（price/edge/threshold/bias）+
# detail JSONB 双轨：前者给 SQL 聚合和后续与 sa_market_kline / 技术指标
# （RSI 等）join 复盘用，后者保真当轮完整 item（含执行单）。

DDL = """
-- 做T判断日志：实盘建议每轮把全部持仓的判断（含 hold/不做）落库。
-- 幂等建表；结构化列供聚合/复盘 join，detail 存当轮完整 item 快照。
CREATE TABLE IF NOT EXISTS sa_t_advisory_log (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    run_type TEXT NOT NULL DEFAULT 'bg',
    code TEXT NOT NULL,
    name TEXT,
    trade_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    action TEXT NOT NULL,
    bias TEXT,
    price NUMERIC,
    cost NUMERIC,
    pnl_pct NUMERIC,
    edge_pct NUMERIC,
    threshold_pct NUMERIC,
    anchor_price NUMERIC,
    trigger_price NUMERIC,
    net NUMERIC,
    layer TEXT,
    movable INTEGER,
    base_shares INTEGER,
    pending BOOLEAN DEFAULT FALSE,
    stabilizing BOOLEAN,
    why TEXT,
    invalidate TEXT,
    detail JSONB
);
CREATE INDEX IF NOT EXISTS idx_tadv_code_time
    ON sa_t_advisory_log (code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tadv_run ON sa_t_advisory_log (run_id);
"""

# 落库列（顺序即 _log_rows 行元组顺序，execute_values 按位置填）。
LOG_COLS = ("run_id", "run_type", "code", "name", "trade_date", "action",
            "bias", "price", "cost", "pnl_pct", "edge_pct", "threshold_pct",
            "anchor_price", "trigger_price", "net", "layer", "movable",
            "base_shares", "pending", "stabilizing", "why", "invalidate",
            "detail")

# 读侧列（顺序即 read_log 的行元组顺序；dict 游标按名取，tuple 按位取）。
LOG_ROW_COLS = ("run_id", "run_type", "code", "name", "trade_date", "action",
                "bias", "price", "cost", "pnl_pct", "edge_pct",
                "threshold_pct", "anchor_price", "trigger_price", "net",
                "layer", "movable", "base_shares", "pending", "stabilizing",
                "why", "invalidate", "created_at", "detail")


def _num_log(v):
    """落库前的数值闸门：None/非数值/NaN/Inf -> None。

    PG 的 numeric 能存 NaN（AGENTS.md 2026-10-13：NaN 进库后读侧
    Decimal('NaN') 运算直接炸出 InvalidOperation），必须在写侧拒掉。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _log_rows(run_id: str, run_type: str, items, today: str) -> list[tuple]:
    """advisory 的 items -> 落库行。**纯函数**，离线可断言。"""
    rows = []
    for it in (items or []):
        if not isinstance(it, dict) or not it.get("code"):
            continue
        tk = it.get("ticket") or {}
        detail = {k: v for k, v in it.items() if k != "detail"}
        try:
            mv = int(it.get("movable")) if it.get("movable") is not None else None
        except (TypeError, ValueError):
            mv = None
        try:
            bs = int(it.get("base_shares")) if it.get("base_shares") is not None else None
        except (TypeError, ValueError):
            bs = None
        rows.append((
            run_id, run_type, str(it["code"]), it.get("name") or "", today,
            it.get("action") or "hold", it.get("bias"),
            _num_log(it.get("price")), _num_log(it.get("cost")),
            _num_log(it.get("pnl_pct")), _num_log(it.get("edge_pct")),
            _num_log(it.get("threshold")),
            _num_log(it.get("anchor_price")),
            _num_log(tk.get("trigger_price")),
            _num_log(tk.get("net")),
            it.get("layer") or "",
            mv, bs,
            bool(it.get("pending")), it.get("stabilizing"),
            it.get("why") or "", it.get("invalidate") or "",
            json.dumps(detail, ensure_ascii=False, default=str),
        ))
    return rows


def persist_advisory(deps: dict, res: dict, run_type: str = "bg",
                     conf: dict | None = None) -> int:
    """把一轮 advisory 的**全部**判断（含 hold/不做）写入 sa_t_advisory_log。

    - 失败隔离：任何异常都吞掉返回 0，只打一行日志 —— 落库是观测，
      绝不能把建议线程/接口拖崩。
    - run_type 区分来源：'bg'=后台建议线程（默认 15 分钟一轮），
      'api'=用户打开页面/点刷新触发的判定。回看时能分清「系统自动判的」
      和「用户盯着看的」。
    - 返回写入行数（关闭/无 items/失败均为 0）。
    """
    try:
        c = _conf(conf)
        if not c.get("log_enabled", True):
            return 0
        items = (res or {}).get("items") or []
        if not items:
            return 0
        now = datetime.now()
        run_id = "%s-%s-%s" % (run_type, now.strftime("%Y%m%d%H%M%S"),
                               uuid.uuid4().hex[:6])
        rows = _log_rows(run_id, run_type, items, now.strftime("%Y-%m-%d"))
        if not rows:
            return 0
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO sa_t_advisory_log (%s) VALUES %%s"
                % ",".join(LOG_COLS),
                rows)
        return len(rows)
    except Exception as exc:                  # noqa: BLE001
        print(f"[t-log] 判断落库失败（不影响建议本身）: {exc}", flush=True)
        return 0


def _r(row, idx: int, key: str):
    """tuple / dict 双游标取值（institution_signals._row 同款坑：生产调用方
    可能传 RealDictCursor，按 tuple 下标取会 KeyError）。"""
    if isinstance(row, dict):
        return row.get(key)
    return row[idx] if idx < len(row) else None


def _read_row(row) -> dict:
    """一行日志 -> 输出 dict：Decimal->float、datetime->isoformat、detail 解包。"""
    g = lambda i, k: _r(row, i, k)            # noqa: E731
    d = {
        "run_id": g(0, "run_id"), "run_type": g(1, "run_type"),
        "code": g(2, "code"), "name": g(3, "name"),
        "trade_date": str(g(4, "trade_date") or "")[:10],
        "action": g(5, "action"), "bias": g(6, "bias"),
        "price": _num_log(g(7, "price")), "cost": _num_log(g(8, "cost")),
        "pnl_pct": _num_log(g(9, "pnl_pct")),
        "edge_pct": _num_log(g(10, "edge_pct")),
        "threshold_pct": _num_log(g(11, "threshold_pct")),
        "anchor_price": _num_log(g(12, "anchor_price")),
        "trigger_price": _num_log(g(13, "trigger_price")),
        "net": _num_log(g(14, "net")),
        "layer": g(15, "layer"),
        "movable": g(16, "movable"), "base_shares": g(17, "base_shares"),
        "pending": bool(g(18, "pending")),
        "stabilizing": g(19, "stabilizing"),
        "why": g(20, "why") or "", "invalidate": g(21, "invalidate") or "",
    }
    created = g(22, "created_at")
    d["created_at"] = (created.isoformat(timespec="seconds")
                       if hasattr(created, "isoformat") else str(created or ""))
    raw = g(23, "detail")
    if raw:
        try:
            det = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
            if isinstance(det, dict):
                det.pop("detail", None)
                d.update(det)          # detail 是当轮完整快照，列只是它的子集
        except (ValueError, TypeError):
            pass
    return d


def read_log(deps: dict, days: int = 3, code: str = "",
             limit_runs: int = 40) -> dict:
    """读判断日志：每轮摘要 + 每只票的统计与最近原因 + 最近一轮完整判断。

    - runs   每轮一条摘要（时间/来源/数量/可操作数），最多 limit_runs 轮
    - codes  每只票在窗口内的动作分布 + 最近一次判断与原因
    - latest 最近一轮的完整 items（detail 解包，含执行单/不做原因）
    - history 仅 ?code= 时：该票窗口内逐条时间线（同样解包 detail），上限 400 条
    detail JSONB 不随全窗口返回 —— 30 只 × 26 轮 × 3 天全带 detail 能到几 MB。
    """
    try:
        days = max(1, min(int(days or 3), 30))
    except (TypeError, ValueError):
        days = 3
    try:
        limit_runs = max(1, min(int(limit_runs or 40), 200))
    except (TypeError, ValueError):
        limit_runs = 40
    code = (code or "").strip()
    out: dict = {"days": days, "code": code or None, "runs": [], "codes": [],
                 "latest": None, "history": []}
    since = (datetime.now() - timedelta(days=days)
             ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        where = "created_at >= %s"
        params: list = [since]
        if code:
            where += " AND code = %s"
            params.append(code)
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT %s FROM sa_t_advisory_log WHERE %s "
                "ORDER BY created_at DESC, id DESC"
                % (",".join(LOG_ROW_COLS), where), params)
            rows = cur.fetchall()
    except Exception as exc:                  # noqa: BLE001
        out["error"] = str(exc)[:160]
        return out
    if not rows:
        return out

    runs: dict = {}
    codes: dict = {}
    latest_run_id = None
    latest_items: list = []
    for row in rows:
        d = _read_row(row)
        rid = d["run_id"]
        if latest_run_id is None:
            latest_run_id = rid
        if rid == latest_run_id and len(latest_items) < 200:
            latest_items.append(d)
        r = runs.setdefault(rid, {"run_id": rid, "run_type": d["run_type"],
                                  "created_at": d["created_at"],
                                  "n_items": 0, "n_act": 0, "act_codes": []})
        r["n_items"] += 1
        act = d["action"]
        if act in T_ACTIONS:
            actionable = True
        elif act in TREND_ACTIONS:
            # 趋势行 shares=0 是「判出来了但今天卖不了/不值得卖」，不能算可操作，
            # 否则「本轮可操作 N 只」里混着 0 股的行。
            try:
                actionable = int(d.get("shares_suggest") or 0) > 0
            except (TypeError, ValueError):
                actionable = False
        else:
            actionable = False
        if actionable:
            r["n_act"] += 1
            if d["code"] not in r["act_codes"] and len(r["act_codes"]) < 10:
                r["act_codes"].append(d["code"])
        cd = codes.setdefault(d["code"], {
            "code": d["code"], "name": d["name"], "total": 0,
            "t_sell": 0, "t_buy": 0, "trend_reduce": 0, "hard_stop": 0,
            "trend_wait": 0, "hold": 0,
            "last_action": None, "last_why": "", "last_created_at": "",
            "last_price": None, "last_edge_pct": None,
            "last_threshold_pct": None, "last_bias": None,
            "first_created_at": d["created_at"]})
        cd["total"] += 1
        if act in ("t_sell", "t_buy", "trend_reduce", "hard_stop", "trend_wait"):
            cd[act] += 1
        else:
            cd["hold"] += 1
        if not cd["last_created_at"] or d["created_at"] > cd["last_created_at"]:
            cd.update({"last_action": d["action"], "last_why": d["why"],
                       "last_created_at": d["created_at"],
                       "last_price": d["price"], "last_edge_pct": d["edge_pct"],
                       "last_threshold_pct": d["threshold_pct"],
                       "last_bias": d["bias"]})
        cd["first_created_at"] = min(cd["first_created_at"], d["created_at"])
        if code and len(out["history"]) < 400:
            out["history"].append(d)

    out["runs"] = list(runs.values())[:limit_runs]
    out["codes"] = sorted(codes.values(),
                          key=lambda x: (-x["total"], x["code"]))
    out["latest"] = ({"run_id": latest_run_id,
                      "run_type": runs[latest_run_id]["run_type"],
                      "created_at": runs[latest_run_id]["created_at"],
                      "items": latest_items} if latest_run_id else None)
    out["n_rows"] = len(rows)
    return out