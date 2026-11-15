"""持仓派生指标（纯计算，不联网、不连库、不 import app）。

目前只做「累计回本价」：把**已实现盈亏**也算进去，涨到哪个价整只票的
累计总盈亏（浮动 + 已实现）才归零。只算浮动盈亏的「成本价」会低估回本门槛
—— 例如 00100 MINIMAX 摊薄成本 167.72 HKD、已实现 −27,797 HKD / 2100 股，
真正的累计回本价是 180.96 HKD，不是 167.72。
"""
from __future__ import annotations

import math


def _f(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def breakeven_price(avg_cost, realized_pnl, shares) -> float | None:
    """累计回本价（原币）。

    (P* − avg_cost) × shares + realized_pnl = 0
        → P* = avg_cost − realized_pnl / shares
    realized_pnl 为负（已实现亏损）时 P* 高于摊薄成本；为正时低于成本
    （表示「跌到这个价才把累计盈利吐完」）。无持股或成本缺失返回 None。
    """
    n = _f(shares)
    avg = _f(avg_cost, -1.0)
    if n <= 0 or avg <= 0:
        return None
    be = avg - _f(realized_pnl) / n
    return max(0.0, be)


def breakeven(avg_cost, realized_pnl, shares, price, fx: float = 1.0) -> dict:
    """给持仓行用的完整回本信息，金额/价格均为原币，*_cny 为折人民币。"""
    be = breakeven_price(avg_cost, realized_pnl, shares)
    px = _f(price, -1.0)
    k = _f(fx, 1.0) or 1.0
    if be is None:
        return {"price": None, "price_cny": None, "pct": None}
    return {
        "price": round(be, 4),
        "price_cny": round(be * k, 4),
        # 相对现价还要涨多少（同币种比值，与汇率无关）；负数=已经回本
        "pct": round((be / px - 1) * 100, 2) if px > 0 else None,
    }


def recover_to_avg_cost(avg_cost, price) -> float | None:
    """回到**摊薄成本**需要的涨幅 %：(avg_cost / price − 1) × 100。

    与 breakeven_price 的区别：那个把**已实现盈亏**也摊进股价（累计口径，
    「累计回本价」列），这个只看摊薄成本 vs 现价（券商 App 习惯的「回本需涨」）。
    同币种比值、与汇率无关；现价已在成本上方 → 负值（前端显示「成本下方」）；
    无成本或无现价返回 None（不编造）。
    """
    avg = _f(avg_cost, -1.0)
    px = _f(price, -1.0)
    if avg <= 0 or px <= 0:
        return None
    return round((avg / px - 1) * 100, 2)
