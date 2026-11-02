"""交易操作评审（只读）。

给平台对话（nanobot agent）用：用户说「我要买/卖 XXX」时，基于真实数据判断这个
操作合不合理 —— 支持、谨慎、还是劝住，并给出反方理由与「什么情况会让我改口」。

只读边界（不许改）
------------------
- 不 import app（app 的 import 要 60~80 秒且会起全套守护线程）
- 不碰数据库、不联网、不下单、不记账
- 不调用 paper_trading._execute_decision / t_trading.scan_once / _execute_one /
  persist_advisory / app 的任何写端点
- 不改任何交易或风控口径：阈值全部从传进来的配置读，这里一个默认值都不硬改

本模块只做两件事：``review(ctx)`` 产出结构化结论，``render(rv)`` 产出给人看的文本。
数据由调用方（app 端点 / MCP 工具）准备好，经 ctx 传进来。缺什么都写进 gaps，
**不拿猜测填空**。

为什么复用 t_trading / paper_trading 而不另写一套判据
----------------------------------------------------
做 T 的方向、趋势减仓、追高闸门、手续费都是已经上线并在真库跑过的东西。
评审要是自己另写一份，两边结论迟早对不上，而「页面上说可以、交易引擎却在拦」
这种矛盾比结论本身更糟。这里只做**引用**，不重算。
"""
from __future__ import annotations

import math
from datetime import date as _date_cls

import paper_trading as PT
import t_trading as T

# ---------------- 结论口径 ----------------

VERDICT_LABELS = {
    "support": "支持",
    "caution": "谨慎（不是不能做，但要先想清楚）",
    "dissuade": "劝住（按现在的信息，不建议这么做）",
    "unknown": "无法评审（数据不足）",
}
SIDE_LABELS = {"buy": "买入", "sell": "卖出"}
KIND_LABEL = {"pro": "支持", "con": "反对", "info": "中性"}

# 净分阈值。正数越大越倾向支持；负数越小越倾向劝住。
SUPPORT_NET = 25.0
DISSUADE_NET = -35.0

# 用户口述价与实时价偏离超过这个比例时才提示（A 股单日涨跌幅上限 10%/20%，
# 超过 20% 偏离不可能是同一天的正常成交价）。
PRICE_DEV_WARN_PCT = 20.0

_MISS_BRANCHES = {
    "miss", "deep_miss",
    "valuation_loss", "valuation_severe", "valuation_weak",
}
_BEAT_BRANCHES = {"beat"}


# ---------------- 基础助手 ----------------

def _num(v, default=None):
    """安全取数：bool 不算数字，非有限值（NaN/Inf）一律当缺失。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _i(v, default=0) -> int:
    f = _num(v, None)
    return int(f) if f is not None else int(default)


def _s(v) -> str:
    return str(v if v is not None else "").strip()


def _is_hk(code) -> bool:
    s = _s(code).lower()
    if s.startswith("hk"):
        s = s[2:]
    return len(s) == 5 and s.isdigit()


def _close_list(bars) -> list[float]:
    out = []
    for b in bars or []:
        if not isinstance(b, dict):
            continue
        c = _num(b.get("close"), None)
        if c is not None and c > 0:
            out.append(c)
    return out


def _last_close(bars):
    cl = _close_list(bars)
    return cl[-1] if cl else None


def _ma(bars, n):
    cl = _close_list(bars)
    if len(cl) < n:
        return None
    return sum(cl[-n:]) / float(n)


def _date10(v):
    s = _s(v)[:10]
    if len(s) != 10:
        return None
    try:
        return _date_cls.fromisoformat(s)
    except ValueError:
        return None


def _days_to(disclose_date, today):
    d = _date10(disclose_date)
    t = _date10(today)
    if not d or not t:
        return None
    return (d - t).days


def _clip(s, n=160) -> str:
    s = _s(s)
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _money(v) -> str:
    f = _num(v, None)
    return "—" if f is None else f"{f:,.0f}"


def _pct(v) -> str:
    return T.fnum(v, "%.2f%%")


def _px(v) -> str:
    return T.fpx(v)


def _r(reasons, kind, text, weight, inv=""):
    """收集一条理由。同 kind 同文案不重复加（同一件事不要占两行）。"""
    text = _s(text)
    if not text:
        return
    for x in reasons:
        if x["kind"] == kind and x["text"] == text:
            return
    reasons.append({
        "kind": kind,
        "text": _clip(text, 240),
        "weight": float(weight or 0.0),
        "inv": _clip(inv, 200),
    })


def _bad(msg) -> dict:
    """参数级失败：不给任何结论，明确说为什么评不了。"""
    m = _s(msg) or "参数不足，无法评审"
    return {
        "ok": False, "error": m, "gaps": [m], "blockers": [], "reasons": [],
        "verdict": "unknown", "verdict_label": VERDICT_LABELS["unknown"],
        "net": 0.0, "pro_sum": 0.0, "con_sum": 0.0,
        "invalidate": [], "code": "", "name": "", "side": "", "side_label": "",
        "shares": 0, "price": None, "amount": None, "amount_cny": None,
        "currency": "", "text": "",
    }


def _pick_event_plan(ep, code):
    if isinstance(ep, list):
        for x in ep:
            if isinstance(x, dict) and _s(x.get("code")) == code:
                return x
        return None
    if isinstance(ep, dict):
        if not ep.get("code") or _s(ep.get("code")) == code:
            return ep
    return None


def _market_temp(mt, group):
    """按市场取温度（"a" / "hk"）。返回 (temp, label)。"""
    for m in (mt or {}).get("markets") or []:
        if not isinstance(m, dict):
            continue
        if _s(m.get("group")) == group:
            t = _num(m.get("temp"), None)
            if t is not None:
                return t, _s(m.get("label")) or group
    return None, ""


# ---------------- 主入口 ----------------

def review(ctx: dict | None) -> dict:
    """评审一次计划中的买卖。**纯函数**：同样的 ctx 必得同样的结论。"""
    c = ctx if isinstance(ctx, dict) else {}
    code = _s(c.get("code"))
    name = _s(c.get("name"))
    side = _s(c.get("side")).lower()
    user_reason = _clip(c.get("reason"), 200)

    if side not in ("buy", "sell"):
        return _bad("side 必须是 buy 或 sell（收到 %r）" % _s(c.get("side")))
    if not code:
        return _bad("未指定股票代码，无法评审")

    pconf = dict(PT.DEFAULTS)
    if isinstance(c.get("paper_conf"), dict):
        pconf.update(c["paper_conf"])
    tconf = dict(T.T_DEFAULTS)
    if isinstance(c.get("t_conf"), dict):
        tconf.update(c["t_conf"])
    fees_conf = c.get("fees_conf")
    if not isinstance(fees_conf, dict):
        fees_conf = pconf.get("fees") or {}

    quote = c.get("quote") if isinstance(c.get("quote"), dict) else {}
    if not name:
        name = _s(quote.get("name"))
    bars = c.get("bars_daily") if isinstance(c.get("bars_daily"), list) else []
    intraday = c.get("intraday") if isinstance(c.get("intraday"), dict) else None

    shares = max(0, _i(c.get("shares"), 0))
    user_price = _num(c.get("price"), None)
    if user_price is not None and user_price <= 0:
        user_price = None
    quote_price = _num(quote.get("price"), None)
    if quote_price is not None and quote_price <= 0:
        quote_price = None
    price = user_price if user_price is not None else quote_price

    held = max(0, _i(c.get("held"), 0))
    cost = _num(c.get("cost"), None)
    if cost is not None and cost <= 0:
        cost = None
    sellable = _num(c.get("sellable"), None)
    sellable = int(sellable) if sellable is not None else None
    t_plus_0 = bool(c.get("t_plus_0"))
    today = _s(c.get("today"))
    hk = _is_hk(code)
    fx_rate = _num(c.get("fx_rate"), None)

    lot = T.lot_size(code)
    currency = "HKD" if hk else "CNY"

    gaps: list[str] = []
    reasons: list[dict] = []
    blockers: list[dict] = []

    # 用户报的价与实时价差太多：只提示不动算（评审必须按用户真会成交的价算），
    # 但要让人看到这两者分歧 —— 否则金额/盈亏/均价比较全在评审另一个价位，
    # 结论看着自洽，其实答的是另一个问题。
    if user_price is not None and quote_price is not None:
        dev = (user_price / quote_price - 1.0) * 100.0
        # 除法在 double 下带 ~1e-15 噪声（120/100-1 = 19.999999999999996），
        # 不补容差的话「恰好 20%」会被判成不提示 —— 边界必须是闭区间。
        if abs(dev) >= PRICE_DEV_WARN_PCT - 1e-9:
            _r(reasons, "info",
               "你报的价 %s 与实时价 %s 相差 %+.1f%%，金额/盈亏/均价比较都按你报的价算；"
               "若这是笔误，请按现价重说一次" % (_px(user_price), _px(quote_price), dev),
               0.0,
               inv="如果你报的价是笔误，换回实时价 %s 后结论会变" % _px(quote_price))

    # ---- 金额与汇率 ----
    amount = None
    amount_cny = None
    if shares > 0 and price is not None:
        amount = float(shares) * float(price)
        if hk:
            if fx_rate and fx_rate > 0:
                amount_cny = amount * float(fx_rate)
            else:
                amount_cny = amount
                gaps.append("缺港币汇率，资金检查按港元近似（结论可能偏严/偏松）")
        else:
            amount_cny = amount
    elif shares > 0 and price is None:
        gaps.append("没有现价，金额、成本与资金检查全部跳过")

    # ---- 阻塞项 ----
    if side == "buy":
        if shares > 0 and shares % lot != 0:
            sug = max(lot, T._round_lot(code, shares))
            blockers.append({
                "text": "买入 %d 股不是 %d 股整数倍，交易所会直接拒单" % (shares, lot),
                "suggest_shares": sug,
            })
        cash = _num(c.get("available_cash"), None)
        if cash is None:
            gaps.append("没有可用资金数据，跳过「买不买得起」检查")
        elif amount_cny is not None and amount_cny > float(cash) + 1e-6:
            per = (price * (fx_rate or 1.0)) if price else 0.0
            sug = T._round_lot(code, int(float(cash) / per)) if per > 0 else 0
            if sug < lot:
                sug = 0
            blockers.append({
                "text": "买入金额约 %s 元，超过可用资金 %s 元" % (
                    _money(amount_cny), _money(cash)),
                "suggest_shares": sug,
            })
    else:  # sell
        if held <= 0:
            blockers.append({
                "text": "当前没有任何持仓，无法卖出",
                "suggest_shares": 0,
            })
        elif shares > 0 and shares > held:
            blockers.append({
                "text": "计划卖出 %d 股，超过当前持仓 %d 股" % (shares, held),
                "suggest_shares": held,
            })
        if sellable is None:
            gaps.append("拿不到 T+1 可卖数量，卖出可行性未校验")
        elif shares > 0 and shares > sellable:
            if sellable <= 0:
                txt = ("今天买入的份额当日不可卖（T+1），当前可卖 0 股"
                       if not t_plus_0 else "当前可卖 0 股")
            else:
                txt = "计划卖出 %d 股，超过可卖 %d 股" % (shares, sellable)
            blockers.append({"text": txt, "suggest_shares": int(sellable)})
        if shares > 0 and shares % lot != 0:
            _r(reasons, "info",
               "卖出 %d 股不是 %d 股整数倍；A 股卖出允许零股，但需要一次性清掉不足一手的部分"
               % (shares, lot), 0)

    if shares <= 0:
        gaps.append("没给具体股数，尺寸/资金/可卖检查全部跳过（结论只看方向与估值）")

    # ---- 公共客观数据 ----
    trend = T.trend_state(bars) if bars else "flat"
    if not bars:
        gaps.append("取不到日K，趋势与追高判据跳过")
    elif len(_close_list(bars)) < 20:
        gaps.append("日K不足 20 根，趋势判不出来（按 flat 处理）")

    group = "hk" if hk else "a"
    mt_temp, mt_label = _market_temp(c.get("market_temp"), group)
    if mt_temp is None:
        gaps.append("缺市场温度数据，市场级冷热未纳入")

    ep = _pick_event_plan(c.get("event_plan"), code)
    if ep is None:
        gaps.append("没有该股的财报预案（event_risk），财报窗口未纳入")

    # 趋势减仓：调用方给了就用，否则自己按同一份日K算（内部会清洗 bars）
    tr = c.get("trend_reduce") if isinstance(c.get("trend_reduce"), dict) else None
    if tr is None and bars:
        try:
            tr = T.trend_reduce_state(
                bars, tconf, code=code, cost=cost or 0.0, held=held,
                sellable=sellable, price=price or 0.0,
                first_date=_s(c.get("first_date")), today=today,
                quote_date=_s(c.get("quote_date")), t_plus_0=t_plus_0)
        except Exception as e:  # 纯函数不该因为这段把整次评审打掉
            gaps.append("趋势减仓判据计算失败（%s）" % _clip(type(e).__name__, 40))

    # 同日反向（实盘口径：查今天这只票已经成交过哪些方向）
    same_day = c.get("today_fills") if isinstance(c.get("today_fills"), dict) else {}
    filled = same_day.get(code) or {}
    filled_sides = set(filled.get("sides") or []) if isinstance(filled, dict) else set()

    # ---- 买入理由 ----
    if side == "buy":
        _buy_reasons(reasons, gaps=gaps, code=code, bars=bars, intraday=intraday,
                     price=price, trend=trend, pconf=pconf, tconf=tconf,
                     fees_conf=fees_conf, amount_cny=amount_cny,
                     held=held, tr=tr, mt_temp=mt_temp, mt_label=mt_label,
                     ep=ep, today=today, filled_sides=filled_sides,
                     portfolio_value=_num(c.get("portfolio_value"), None),
                     pos_value=_num(c.get("pos_value"), None))

    # ---- 卖出理由 ----
    else:
        _sell_reasons(reasons, code=code, bars=bars, intraday=intraday,
                      price=price, trend=trend, tconf=tconf, held=held,
                      shares=shares, cost=cost, sellable=sellable, tr=tr,
                      mt_temp=mt_temp, mt_label=mt_label, ep=ep, today=today,
                      filled_sides=filled_sides)

    # ---- 判决 ----
    pro_sum = sum(r["weight"] for r in reasons if r["kind"] == "pro")
    con_sum = sum(r["weight"] for r in reasons if r["kind"] == "con")
    net = pro_sum - con_sum
    has_judgement = any(r["kind"] in ("pro", "con") for r in reasons)
    if blockers:
        verdict = "dissuade"
    elif not has_judgement:
        verdict = "unknown"
    elif net >= SUPPORT_NET:
        verdict = "support"
    elif net <= DISSUADE_NET:
        verdict = "dissuade"
    else:
        verdict = "caution"

    inval = []
    for r in reasons:
        if r.get("inv") and r["inv"] not in inval:
            inval.append(r["inv"])
    if not inval:
        if side == "buy":
            inval.append("如果日线转空（收盘跌破 MA20 且 MA20 低于 MA60），这次买入的前提就不成立了")
        else:
            inval.append("如果价格重新站上 MA20 且趋势转多，卖出的理由就弱了")
    inval = inval[:4]

    rv = {
        "ok": True,
        "error": "",
        "code": code,
        "name": name,
        "side": side,
        "side_label": SIDE_LABELS.get(side, side),
        "shares": shares,
        "price": price,
        "amount": amount,
        "amount_cny": amount_cny,
        "currency": currency,
        "held": held,
        "cost": cost,
        "sellable": sellable,
        "lot": lot,
        "user_reason": user_reason,
        "trend": trend,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        "net": net,
        "pro_sum": pro_sum,
        "con_sum": con_sum,
        "reasons": reasons,
        "blockers": blockers,
        "invalidate": inval,
        "gaps": gaps,
    }
    rv["text"] = render(rv)
    return rv


# ---------------- 买入理由 ----------------

def _buy_reasons(reasons, *, gaps, code, bars, intraday, price, trend, pconf, tconf,
                 fees_conf, amount_cny, held, tr, mt_temp, mt_label, ep,
                 today, filled_sides, portfolio_value, pos_value):
    # 1) 追高闸门（与模拟盘 new-buy 完全同一份实现）
    if bars:
        try:
            hit = PT.chase_check(bars, pconf)
        except Exception:
            hit = ""
        if hit:
            _r(reasons, "con", "追高信号：" + hit, 25,
               "等回调到 20 日线附近、或距 20 日高点回撤超过 10% 再考虑")

    # 2) 日线趋势
    if bars and len(_close_list(bars)) >= 20:
        if trend == "up":
            _r(reasons, "pro", "日线均线多头排列，趋势向上（顺势买入）", 15,
               "如果收盘重新跌破 MA20，顺势的前提就没了")
        elif trend == "down":
            m20, m60 = _ma(bars, 20), _ma(bars, 60)
            _r(reasons, "con",
               "日线空头排列（现价 %s / MA20 %s / MA60 %s），现在买是逆势" % (
                   _px(_last_close(bars)), _px(m20), _px(m60)), 25,
               "等收盘站回 MA20 之上、且 MA20 不再下行，才叫趋势修复")

    # 3) 做 T 方向（顺带告诉你「这个位置适合先卖还是先买」）
    if intraday is not None and price:
        bias = T.t_bias(trend, intraday, price)
        if bias.get("bias") == "bull":
            _r(reasons, "pro", "做T方向偏多：" + _s(bias.get("why")), 10,
               "如果价格跌破日内 VWAP，方向判据会翻成 bear")
        elif bias.get("bias") == "bear":
            _r(reasons, "con", "做T方向偏空：" + _s(bias.get("why")), 15,
               "如果价格站上日内 VWAP 且均线转多，方向判据会翻成 bull")
        elif bias.get("bias") == "flat":
            _r(reasons, "info",
               "方向判不出来（" + _s(bias.get("why")) + "）；看不清的时候宁可不动", 0)

    # 4) 分时止跌确认
    if intraday is not None:
        bars_it = intraday.get("bars") if isinstance(intraday.get("bars"), list) else []
        ok_stab, why_stab = T.stabilizing(bars_it, 6)
        if ok_stab:
            _r(reasons, "pro", "分时已止跌（" + _clip(why_stab, 100) + "）", 8,
               "如果分时重新创新低，止跌确认就失效了")
        elif trend != "up":
            _r(reasons, "con",
               "分时还没止跌（" + _clip(why_stab, 100) + "），现在接是在下跌途中接", 12,
               "等分时不再创新低、且近 3 根均价高于前 3 根，才算止跌")

    # 5) 市场温度
    if mt_temp is not None:
        if mt_temp >= 70:
            _r(reasons, "con",
               "%s市场温度 %.1f°C（偏高），整体估值不便宜" % (mt_label, mt_temp), 20,
               "等市场温度回落到 60°C 以下再提高仓位")
        elif mt_temp <= 30:
            _r(reasons, "pro",
               "%s市场温度 %.1f°C（偏低），整体估值处于低位" % (mt_label, mt_temp), 15,
               "如果市场温度快速升到 70°C 以上，低估值的窗口就关了")

    # 6) 财报事件
    if isinstance(ep, dict):
        br = _s(ep.get("branch"))
        if ep.get("disclosed"):
            if br in _MISS_BRANCHES:
                _r(reasons, "con",
                   "最新财报已披露且不及预期（分支 %s：%s）" % (
                       br, _s(ep.get("name")) or code), 20,
                   "如果下一期预告显示利润修复，这条才算解除")
            elif br in _BEAT_BRANCHES:
                _r(reasons, "pro", "最新财报已披露且超预期（分支 beat）", 10,
                   "如果下季度指引下修，超预期的支撑就没了")
        else:
            d = _days_to(ep.get("disclose_date"), today)
            if d is not None and 0 <= d <= 7:
                _r(reasons, "con",
                   "距财报披露只剩 %d 天（%s），业绩不确定，此时买入是在赌财报" % (
                       d, _s(ep.get("disclose_date"))), 25,
                   "等财报落地、看清实际值落在哪个分支再买")
            elif d is not None and d < 0:
                _r(reasons, "con",
                   "已过预约披露日 %s 仍未披露，信息状态不确定" % _s(ep.get("disclose_date")),
                   15, "等公司正式披露后再判断")

    # 7) 集中度
    pv = _num(portfolio_value, None)
    psv = _num(pos_value, None)
    if pv is not None and pv > 0:
        cur_pos = psv if psv is not None else (
            float(held) * float(price) if (held > 0 and price) else 0.0)
        add = amount_cny or 0.0
        ratio = (cur_pos + add) / pv * 100.0
        cap = _num(pconf.get("max_position_pct"), None) or 0.0
        if ratio > 40:
            _r(reasons, "con",
               "买后该股占总资产约 %.1f%%，集中度过高" % ratio, 30,
               "如果这只是一只占总资产 20% 以内的票，集中度就不构成反对理由")
        elif cap > 0 and ratio > cap:
            _r(reasons, "con",
               "买后该股占总资产约 %.1f%%，超过单票上限 %.0f%%" % (ratio, cap), 18,
               "如果把仓位控制在 %.0f%% 以内，这条就解除" % cap)
        else:
            _r(reasons, "info",
               "买后该股占总资产约 %.1f%%（上限 %.0f%%）" % (ratio, cap or 0), 0)
    elif held > 0:
        gaps.append("缺总资产/持仓市值，集中度未纳入")

    # 8) 手续费成本占比
    if amount_cny and amount_cny > 0:
        try:
            cost_pct = T.cost_for_amount(code, amount_cny, tconf, fees_conf)
        except Exception:
            cost_pct = 0.0
        if cost_pct > 0.5:
            _r(reasons, "con",
               "这一笔的往返成本约 %.2f%%（金额太小，佣金最低 5 元摊薄不了）" % cost_pct,
               10, "如果把单笔金额放大到 3 万元以上，成本占比会降到 0.5% 以下")

    # 9) 现有持仓已在减仓信号里
    if isinstance(tr, dict) and held > 0:
        act = _s(tr.get("action"))
        if act in ("trend_reduce", "hard_stop"):
            _r(reasons, "con",
               "现有持仓已经触发「%s」信号（%s），此时加仓和风控方向相反" % (
                   T.TREND_LABELS.get(act, act), _clip(_s(tr.get("why")), 100)), 30,
               "如果价格重新站回 MA20 且趋势转多，减仓信号才算解除")

    # 10) 同日反向
    if "sell" in filled_sides:
        _r(reasons, "con",
           "今天这只票已经卖出过（同日反向）。一天之内先卖后买，多数是情绪驱动的追涨杀跌"
           "；模拟盘有同日反向闸门会直接拦下", 30,
           "如果卖出是因为换仓到更好的标的、而现在又发现判断错了，那另说 —— "
           "但请先把两次的理由都写下来")


# ---------------- 卖出理由 ----------------

def _sell_reasons(reasons, *, code, bars, intraday, price, trend, tconf, held,
                  shares, cost, sellable, tr, mt_temp, mt_label, ep, today,
                  filled_sides):
    # 1) 趋势减仓 / 硬止损（与实盘建议引擎同一份）
    if isinstance(tr, dict):
        if not tr.get("enabled"):
            _r(reasons, "info",
               "趋势减仓规则未启用（t_trading.trend_reduce.enabled = false），"
               "系统不会催你卖，这条结论只基于你自己的计划", 0)
        else:
            act = _s(tr.get("action"))
            why = _clip(_s(tr.get("why")), 140)
            if act == "hard_stop":
                _r(reasons, "pro", "触发硬止损：" + why, 45,
                   "如果收盘重新站回止损线之上，硬止损信号才解除（但已触发的风险仍在）")
            elif act == "trend_reduce":
                sug = _i(tr.get("shares"), 0)
                _r(reasons, "pro",
                   "触发趋势减仓（建议减 %d 股）：%s" % (sug, why), 30,
                   "如果价格连续站回 MA20 达到重新武装天数，趋势信号才解除")
            elif act == "trend_wait":
                _r(reasons, "info", "深亏保护：趋势已走坏但浮亏过深，规则只提示不催卖（%s）"
                   % why, 15)
            elif act == "none":
                _r(reasons, "info",
                   "趋势减仓规则当前没有卖出信号（%s）" % (why or "条件未满足"), 0)

    # 2) 日线趋势
    if bars and len(_close_list(bars)) >= 20:
        if trend == "down":
            _r(reasons, "pro", "日线空头排列，卖出顺应趋势", 20,
               "如果收盘重新站上 MA20 且 MA20 转向上，趋势卖出的理由就弱了")
        elif trend == "up":
            m20 = _ma(bars, 20)
            _r(reasons, "con",
               "日线仍是多头排列（现价 %s / MA20 %s），此时卖出是在强势里下车" % (
                   _px(_last_close(bars)), _px(m20)), 25,
               "如果跌破 MA20 且 MA20 开始走平/下行，顺势卖出的理由才成立")

    # 3) 盈亏状态
    if cost and price and held > 0:
        pnl = (float(price) / float(cost) - 1.0) * 100.0
        act = _s((tr or {}).get("action"))
        if pnl <= -25 and act != "hard_stop":
            _r(reasons, "con",
               "当前浮亏 %.2f%%（成本 %s / 现价 %s），这个位置割肉很容易割在阶段低点" % (
                   pnl, _px(cost), _px(price)), 10,
               "如果跌破成本价的趋势证据（MA20 下穿 MA60）进一步恶化，割肉才有依据")
        elif pnl >= 30:
            _r(reasons, "pro", "当前浮盈 %.2f%%，兑现利润是合理选择" % pnl, 15,
               "如果基本面出现新的上行动力，可以改为分批减而不是一次清")
        elif -25 < pnl <= -10:
            _r(reasons, "pro", "当前浮亏 %.2f%%，及时止损能把损失控制在可承受范围" % pnl,
               8, "如果只是短期波动且趋势未坏，可以等反弹再减")

    # 4) 卖点相对日内均价
    if intraday is not None and price:
        vwap = _num(intraday.get("vwap"), None)
        if vwap and vwap > 0:
            if price > vwap * 1.002:
                _r(reasons, "pro",
                   "现价 %s 高于日内均价 %s，卖在均价上方" % (_px(price), _px(vwap)), 10,
                   "如果价格回落到均价下方，这个卖点优势就没了")
            elif price < vwap * 0.998:
                _r(reasons, "con",
                   "现价 %s 低于日内均价 %s，卖在均价下方（卖在相对低位）" % (
                       _px(price), _px(vwap)), 10,
                   "等价格回到日内均价上方再卖，同样的仓位能多拿一点")

    # 5) 市场温度
    if mt_temp is not None and mt_temp >= 70:
        _r(reasons, "pro",
           "%s市场温度 %.1f°C（偏高），整体估值偏贵，减仓方向与市场冷热一致" % (
               mt_label, mt_temp), 15,
           "如果市场温度回落到 60°C 以下，减仓的紧迫性就下降了")

    # 6) 财报事件
    if isinstance(ep, dict):
        br = _s(ep.get("branch"))
        if ep.get("disclosed"):
            if br in _MISS_BRANCHES:
                _r(reasons, "pro", "财报已披露且不及预期（分支 %s），卖出与基本面一致" % br,
                   25, "如果下一期预告显示利润修复，这条就解除")
            elif br in _BEAT_BRANCHES:
                _r(reasons, "con",
                   "财报已披露且超预期（分支 beat），此时卖是在好消息里走", 20,
                   "如果股价已经充分反映超预期、或者你判断下季度会环比走弱，可以另说")
        else:
            d = _days_to(ep.get("disclose_date"), today)
            if d is not None and 0 <= d <= 7:
                _r(reasons, "pro",
                   "距财报披露只剩 %d 天，披露前减仓可以避开业绩不确定性" % d, 12,
                   "如果公司已发业绩预告且区间明确向上，可以等披露后再决定")

    # 7) 做 T 方向（只在卖的是机动仓时才看）
    if intraday is not None and price and held > 0:
        try:
            movable = T.movable_shares(code, held, tconf)
        except Exception:
            movable = 0
        if movable > 0 and (shares <= 0 or movable >= shares):
            bias = T.t_bias(trend, intraday, price)
            if bias.get("bias") == "bear":
                _r(reasons, "pro", "做T方向偏空，先卖后接符合下行节奏：" +
                   _s(bias.get("why")), 10,
                   "如果价格站上日内 VWAP 且均线转多，方向会翻成 bull")
            elif bias.get("bias") == "bull":
                _r(reasons, "con",
                   "做T方向偏多（上行途中），此时卖出容易卖飞、接不回来", 15,
                   "如果价格跌破日内 VWAP，方向判据会翻成 bear")

    # 8) 同日反向
    if "buy" in filled_sides:
        _r(reasons, "con",
           "今天这只票已经买入过（同日反向）。一天之内先买后卖，多半是在跟情绪走"
           "；模拟盘有同日反向闸门会直接拦下", 30,
           "如果买入后出现了当初没预料到的坏消息，那卖出是对的 —— 但请先把新信息说清楚")


# ---------------- 渲染 ----------------

def render(rv: dict) -> str:
    """把结论渲染成给对话用的纯文本（无 emoji，目标 < 2500 字符）。"""
    rv = rv if isinstance(rv, dict) else {}
    if not rv.get("ok"):
        return "【交易评审（只读，不会下单）】\n无法评审：%s" % (
            _s(rv.get("error")) or "参数不足")

    lines = ["【交易评审（只读，不会下单）】"]
    lines.append("结论：%s" % _s(rv.get("verdict_label")))

    code = _s(rv.get("code"))
    name = _s(rv.get("name"))
    side_label = _s(rv.get("side_label")) or _s(rv.get("side"))
    shares = _i(rv.get("shares"), 0)
    price = _num(rv.get("price"), None)
    cur = _s(rv.get("currency"))
    sym = "HK$" if cur == "HKD" else "¥"

    qty = ("%d 股" % shares) if shares > 0 else "（未指定股数）"
    if price is not None:
        head = "%s %s %s %s @ %s" % (side_label, code, name, qty, _px(price))
        amt = _num(rv.get("amount"), None)
        if amt is not None:
            head += "（约 %s%s）" % (sym, _money(amt))
    else:
        head = "%s %s %s %s（无现价）" % (side_label, code, name, qty)
    lines.append("标的：" + head)

    if rv.get("user_reason"):
        lines.append("你的说法：%s" % _clip(rv.get("user_reason"), 160))

    net = _num(rv.get("net"), 0.0) or 0.0
    pro = _num(rv.get("pro_sum"), 0.0) or 0.0
    con = _num(rv.get("con_sum"), 0.0) or 0.0
    lines.append("评分：net %+.0f（支持 %.0f / 反对 %.0f）" % (net, pro, con))

    blockers = rv.get("blockers") or []
    if blockers:
        lines.append("【必须先解决】")
        for b in blockers:
            if not isinstance(b, dict):
                continue
            t = _s(b.get("text"))
            sug = b.get("suggest_shares")
            if sug is not None and _i(sug, -1) >= 0 and t:
                if _i(sug, 0) > 0:
                    t += "（可改为 %d 股）" % _i(sug, 0)
                else:
                    t += "（当前无法执行）"
            if t:
                lines.append("· " + t)

    for kind in ("pro", "con", "info"):
        items = [x for x in (rv.get("reasons") or [])
                 if isinstance(x, dict) and x.get("kind") == kind]
        if not items:
            continue
        lines.append("【%s】" % KIND_LABEL[kind])
        for x in items[:6]:
            lines.append("· " + _s(x.get("text")))

    inval = [x for x in (rv.get("invalidate") or []) if _s(x)]
    if inval:
        lines.append("【什么情况会让我改口】")
        for x in inval[:4]:
            lines.append("· " + _s(x))

    gaps = [x for x in (rv.get("gaps") or []) if _s(x)]
    if gaps:
        lines.append("【缺的数据】")
        for x in gaps[:6]:
            lines.append("· " + _s(x))

    lines.append("（只读评审，不会替你下单，也不改任何交易或风控口径。）")
    out = "\n".join(lines)
    return out if len(out) <= 3000 else out[:2999] + "…"
