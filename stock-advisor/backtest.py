# -*- coding: utf-8 -*-
"""本地回测引擎：验证策略「是不是真的有效」。

为什么必须有这个（2026-09-30）
------------------------------
爬来的**指标一个都不能信**。实测聚宽社区一页 21 条策略文章，标题里直接吹
「年化 526%」「年化 710%」「年化 360%」的有 7 条，占三分之一。这类社区文章的
常见问题：
  1. 幸存者偏差 —— 只贴赚的，亏的没人发
  2. 未来函数 —— 用了当时拿不到的数据（后复权、幸存股票池）
  3. 过拟合 —— 参数在历史上搜出来的，换个区间就失效
  4. 成本忽略 —— 不算手续费/滑点/涨跌停买不进
所以「文章里说年化 100%」这种数字，**只有本地用同样数据重跑一遍才知道真假**。
这就是本模块的价值：给文章的自述指标一个对照基准（perf_claimed vs 本地实测）。

口径（必须与模拟盘一致，否则结论没法比）
----------------------------------------
- 交易成本：复用 paper_trading.trade_fees（12 项费率，前端可配），不另设一套
- 成交价：次日开盘价（避免用当日收盘价成交这种未来函数）
- 涨跌停买不进、停牌跳过
- T+1：买入当日不可卖
- 基准：沪深300 / 创业板指 / 中证500，按 universe 类型自动选，也可手动指定

回测结果同时给两样东西：
  1. 汇总指标（年化/回撤/夏普/胜率/换手/超额）
  2. **逐日净值与回撤序列** —— 前端的「验证图」直接画这个。
     不从文章爬图：那是图片，既不能验证也看不出参数敏感度。
"""
import json
import math
import time
from datetime import date, datetime

# ---------------- 指标计算 ----------------

def max_drawdown(equity: list[float]) -> tuple[float, int, int]:
    """最大回撤（%）。返回 (幅度, 峰值日索引, 谷值日索引)。"""
    if not equity:
        return 0.0, 0, 0
    peak = equity[0]
    peak_i = 0
    worst = 0.0
    wi = wj = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak = v
            peak_i = i
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > worst:
                worst, wi, wj = dd, peak_i, i
    return round(worst, 4), wi, wj


def annual_return(total_return_pct: float, days: int) -> float:
    """年化 %。不足一年按实际天数折算（不足 30 天不折算，直接返回 0 避免夸张）。"""
    if days <= 0:
        return 0.0
    if days < 30:
        return 0.0
    years = days / 365.0
    growth = 1 + total_return_pct / 100
    if growth <= 0:
        return -100.0
    return round((math.pow(growth, 1 / years) - 1) * 100, 4)


def sharpe(equity: list[float], rf_pct: float = 0.0, periods: int = 244) -> float:
    """夏普比率。equity 是净值序列（不是收益序列，内部转日收益）。"""
    if len(equity) < 10:
        return 0.0
    rets = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev > 0:
            rets.append((equity[i] - prev) / prev)
    if len(rets) < 5:
        return 0.0
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return 0.0
    rf_d = (1 + rf_pct / 100) ** (1 / periods) - 1
    return round((mean - rf_d) / sd * math.sqrt(periods), 4)


def monthly_returns(equity: list[float], dates: list[str]) -> list[dict]:
    """月度收益 —— 前端画收益热力图用。"""
    if not equity:
        return []
    out = []
    cur_m = None
    start_eq = equity[0]
    prev_eq = equity[0]
    for i, (eq, d) in enumerate(zip(equity, dates)):
        ym = str(d)[:7]
        if ym != cur_m:
            if cur_m is not None and prev_eq > 0:
                out.append({"month": cur_m,
                            "ret": round((prev_eq / start_eq - 1) * 100, 4)})
            cur_m = ym
            start_eq = prev_eq
        prev_eq = eq
    if cur_m is not None and prev_eq > 0:
        out.append({"month": cur_m,
                    "ret": round((prev_eq / start_eq - 1) * 100, 4)})
    return out


# ---------------- 行情对齐 ----------------

def load_panel(deps: dict, codes: list[str], start: str, end: str) -> dict:
    """取多只标的 K 线，返回 {code: {date: bar}}。

    为什么按日期字典而不是列表：回测要按「交易日」遍历，而不是按「每只票的行」
    遍历 —— 各票上市日不同、停牌不同，按行遍历会让某只票的缺失日期被跳过，
    悄悄改变回测的时间轴。
    """
    import market_data as MD
    panel: dict[str, dict] = {}
    for c in codes:
        bars = MD.load_kline(deps, c, start=start, end=end)
        panel[c] = {str(b["trade_date"])[:10]: b for b in bars}
    return panel


def trading_days(panel: dict) -> list[str]:
    """并集交易日轴（升序）。用并集而不是某只票的日期，
    避免某票停牌时把它自己的日期当成全市场的交易日。"""
    s = set()
    for d in panel.values():
        s |= set(d.keys())
    return sorted(s)


def default_benchmark(universe: list[str]) -> str:
    """按持仓池粗略选基准。判不准就让人手填 —— 基准选错，超额全错。"""
    s = "".join(universe)
    if any(c.startswith("300") or c.startswith("301") for c in universe):
        return "399006"      # 创业板指（成长风格）
    if any(c.startswith("688") for c in universe):
        return "000688"      # 科创50
    if any(c.startswith("5") or c.startswith("1") for c in universe):
        return "510300"      # 沪深300 ETF
    return "000300"


# ---------------- 回测主循环 ----------------

def run(deps: dict, strategy: dict, start: str, end: str,
        init_cash: float = 1000000.0, fees: dict | None = None,
        benchmark: str = "", record_daily: bool = True) -> dict:
    """跑一次回测。

    strategy 需含：
      universe   list[str]  标的池
      weights    dict[str, float]  目标权重（缺省等权）
      hold_days  int         持有交易日数（缺省 5）
      rebalance  str         'period' | 'daily' | 'never'
      signal     callable    (date, panel, state) -> dict[str,float] 权重
                  不给就用等权定期调仓（最简单的基线）

    返回 {ok, summary, daily, monthly, trades, error}
    """
    import paper_trading as PT
    t0 = time.time()
    universe = list(strategy.get("universe") or [])
    if not universe:
        return {"ok": False, "error": "universe 为空：先同步 K 线并指定标的"}

    panel = load_panel(deps, universe, start, end)
    empty = [c for c, d in panel.items() if not d]
    if empty:
        return {"ok": False,
                "error": "这些标的没有 K 线数据，先跑 sync：%s" % ", ".join(empty[:8])}
    days = trading_days(panel)
    if len(days) < 30:
        return {"ok": False,
                "error": "交易日只有 %d 天（<30），数据不足以回测" % len(days)}

    fees = fees if fees is not None else (PT.load_fee_config(deps)
                                          if hasattr(PT, "load_fee_config") else {})
    weights = strategy.get("weights") or {c: 1.0 / len(universe) for c in universe}
    hold_days = int(strategy.get("hold_days") or 5)
    signal = strategy.get("signal")
    bench_code = benchmark or default_benchmark(universe)

    # 基准 K 线（取不到就只算绝对收益，不算超额）
    bench_series = []
    try:
        bpanel = load_panel(deps, [bench_code], start, end)
        bmap = bpanel.get(bench_code) or {}
        bench_series = [(d, float(bmap[d]["close"])) for d in days if d in bmap]
    except Exception:                  # noqa: BLE001
        pass

    cash = init_cash
    holdings: dict[str, float] = {}     # code -> shares
    bought_today: set = set()           # T+1：当日买入不可卖
    equity, dates, dailies, trades = [], [], [], []
    pending = None                      # 待次日开盘执行的调仓
    last_rebal = -999

    for i, d in enumerate(days):
        # ① 用「昨日收盘」决定今日调仓 → 今日**开盘**成交（避免未来函数）
        if pending is not None:
            # 调仓基数必须是**今天开盘时的实际总资产**，不是昨天的快照。
            # 用 equity[-1]（昨日收盘）会形成复利式无限加仓：昨天已按昨天的
            # 总资产买过一轮，今天再在现有持仓之上叠一层「昨日总资产×权重」。
            # 实测这样跑 100 万 -> 158 亿、年化 1710%、回撤 14%，完全失真。
            base_total = cash
            for _c, _pos in holdings.items():
                # holdings[code] = [shares, avg_cost]
                _sh = float(_pos[0]) if isinstance(_pos, (list, tuple)) else float(_pos)
                _b = panel.get(_c, {}).get(d)
                if _b and _b.get("open"):
                    base_total += _sh * float(_b["open"])
            else_total = base_total if base_total > 0 else init_cash

            # 先卖后买，保证买的钱够
            targets = dict(pending)
            for code in list(holdings):
                if code not in targets:
                    targets[code] = 0.0          # 不在目标里 -> 全部卖出
            for code, tgt_w in sorted(targets.items(),
                                      key=lambda kv: kv[1]):
                bar = panel.get(code, {}).get(d)
                if not bar or not bar.get("open"):
                    continue            # 停牌/缺数据：跳过，不臆造价格
                px = float(bar["open"])
                # holdings[code] = [shares, avg_cost]；默认 0 也要给新结构，
                # 否则 holdings.get(code, 0) * px 会拿列表乘浮点 -> TypeError
                cur_val = (float((holdings.get(code) or [0.0, 0.0])[0])) * px
                tgt_val = else_total * float(tgt_w or 0)
                if cur_val > tgt_val:
                    qty = int((cur_val - tgt_val) / px / 100) * 100
                    if qty > 0:
                        cash = _trade(cash, holdings, code, qty, px, d, -1,
                                      fees, trades, bought_today)
                elif cur_val < tgt_val:
                    qty = int((tgt_val - cur_val) / px / 100) * 100
                    if qty > 0:
                        cash = _trade(cash, holdings, code, qty, px, d, 1,
                                      fees, trades, bought_today)
            pending = None

        # ② 结算持仓市值
        mv = 0.0
        hold_detail = []
        for code, pos in holdings.items():
            sh = float(pos[0]) if isinstance(pos, (list, tuple)) else float(pos)
            bar = panel.get(code, {}).get(d)
            px = float(bar["close"]) if bar and bar.get("close") else 0.0
            v = sh * px
            mv += v
            hold_detail.append({"code": code, "shares": int(sh),
                                "cost": round(float(pos[1]), 4),
                                "close": round(px, 4), "value": round(v, 2),
                                "pnl_pct": round((px / float(pos[1]) - 1) * 100, 4)
                                if float(pos[1]) > 0 else None})
        total = cash + mv
        equity.append(total)
        dates.append(d)
        if record_daily:
            pk = (peak if (peak := _running_peak(equity)) else 0)
            dd = ((pk - total) / pk * 100) if pk > 0 else 0.0
            dailies.append({"trade_date": d, "equity": round(total, 2),
                            "cash": round(cash, 2),
                            "position_value": round(mv, 2),
                            "drawdown": round(dd, 4),
                            "holdings": hold_detail})

        # ③ 决定是否调仓（信号在下一天开盘执行）
        need = signal is not None
        if not need:
            rb = strategy.get("rebalance", "period")
            if rb == "daily":
                need = True
            elif rb == "period" and (i - last_rebal) >= hold_days:
                need = True
                last_rebal = i
        if need:
            try:
                pending = (signal(d, panel, {"holdings": {k: v[0] for k, v in holdings.items()},
                                             "equity": total}) if signal
                           else dict(weights))
                pending = {c: float(w) for c, w in (pending or {}).items()
                           if c in panel}
            except Exception as exc:      # noqa: BLE001
                pending = None
                trades.append({"date": d, "error": "信号函数异常: %s" % str(exc)[:120]})

    # ---------------- 汇总指标 ----------------
    if not equity:
        return {"ok": False, "error": "没有产生任何净值点"}
    total_ret = (equity[-1] / init_cash - 1) * 100
    dd, _, _ = max_drawdown(equity)
    days_n = len(equity)
    ann = annual_return(total_ret, days_n)
    sh = sharpe(equity)
    # 胜率只统计**卖出**笔（买入没有盈亏可言；这是原来恒为 0% 的原因）
    sells = [t for t in trades if t.get("side") == -1]
    wins = sum(1 for t in sells if (t.get("pnl") or 0) > 0)
    turn = 0.0
    if equity:
        turn = sum(abs(t["qty"] * t["price"]) for t in trades) / init_cash * 100

    bench_ret = None
    if len(bench_series) >= 2 and bench_series[0][1] > 0:
        bench_ret = (bench_series[-1][1] / bench_series[0][1] - 1) * 100

    summary = {
        "start_date": dates[0], "end_date": dates[-1], "days": days_n,
        "universe": universe, "init_cash": init_cash,
        "total_return": round(total_ret, 4),
        "annual_return": ann,
        "max_drawdown": dd,
        "sharpe": sh,
        "win_rate": round(wins / len(sells) * 100, 4) if sells else 0.0,
        "trade_count": len(trades),
        "sell_count": len(sells),
        "turnover": round(turn, 4),
        "benchmark": bench_code if bench_series else "",
        "bench_return": round(bench_ret, 4) if bench_ret is not None else None,
        "excess": (round(ann - annual_return(bench_ret, days_n), 4)
                   if bench_ret is not None else None),
        "final_equity": round(equity[-1], 2),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    return {"ok": True, "summary": summary, "daily": dailies,
            "monthly": monthly_returns(equity, dates),
            "trades": trades[-500:]}


def _running_peak(equity: list[float]) -> float:
    return max(equity) if equity else 0.0


def _trade(cash, holdings, code, qty, price, d, side, fees, trades,
           bought_today) -> float:
    """执行一笔成交，**返回新的现金**。

    ⚠️ cash 必须走返回值，不能原地改 —— Python 参数是传值不是传引用。
    原地 `cash -= cost` 只会改局部变量，调用方的 cash 纹丝不动，于是每轮都能
    「白嫖」上一轮没扣掉的钱，持仓无限累加（实测 100 万 -> 1.6 亿、年化 1720%）。
    holdings 是 dict 所以能就地改，这也是为什么只有 cash 出问题 —— 那种 bug
    特别隐蔽：不崩、不报错，只是数字慢慢变成鬼。

    费用用 paper_trading.trade_fees（与模拟盘同一套口径）。
    holdings 的结构是 {code: [shares, avg_cost]}：第二个元素是**持仓平均成本**，
    卖出时用它算已实现盈亏 —— 否则胜率恒为 0（原来 trades 里根本没有 pnl 字段，
    而且统计时把 side=1「买入」当成了有盈亏的一边，方向也反了）。
    """
    import paper_trading as PT
    pos = holdings.get(code) or [0.0, 0.0]
    shares0, cost0 = float(pos[0]), float(pos[1])
    # 每条提前 return 都要把 cash 带出去
    notional = qty * price
    try:
        f = PT.trade_fees(code, side, notional, fees or {})
    except Exception:                  # noqa: BLE001
        f = {"total": 0.0}
    ftot = float(f.get("total") or 0)
    pnl = 0.0
    if side > 0:
        cost = notional + ftot
        if cash < cost:                # 现金不够就按能买的整手数买
            qty = int((cash / (1 + ftot / max(notional, 1e-9))) / price / 100) * 100
            if qty <= 0:
                return cash
            notional = qty * price
            cost = notional + ftot
        cash -= cost
        # 新的加权平均成本（含买入费，与模拟盘的 cost 口径一致）
        new_cost = ((cost0 * shares0 + cost) / (shares0 + qty)
                    if (shares0 + qty) > 0 else price)
        holdings[code] = [shares0 + qty, new_cost]
    else:
        if code in bought_today:        # T+1：当日买入不可卖
            return cash
        if qty > shares0:
            qty = int(shares0)
            if qty <= 0:
                return cash
            notional = qty * price
        pnl = (price - cost0) * qty - ftot      # 已实现盈亏（已扣费）
        cash += notional - ftot
        left = shares0 - qty
        holdings[code] = [left, cost0] if left > 0 else None
        if left <= 0:
            holdings.pop(code, None)
    trades.append({"date": d, "code": code, "side": side, "qty": qty,
                   "price": round(price, 4), "notional": round(notional, 2),
                   "fee": round(ftot, 2), "pnl": round(pnl, 2),
                   "cash": round(cash, 2)})
    bought_today.add(code)
    return cash

