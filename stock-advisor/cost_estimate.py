# -*- coding: utf-8 -*-
"""加仓摊薄成本预估：算清「再买 X 股，成本会变成多少」。

为什么单独一个模块而不是塞进 app.py：估算逻辑要在三个地方被调用
（页面实时试算、接口返回、下单前二次确认），且**要能被离线断言**。
app.py 一 import 就起全部守护线程（实测 46.8 秒，见 AGENTS.md），
所以核心算法必须是纯函数、独立模块。

## 口径（必须与真实账本一致，否则预估会骗人）

摊薄成本用**移动加权平均**，与 `app.calc_position` / `sa_holdings` 的
`avg_cost` 同一公式：

    new_avg = (avg_old × shares_old + price_new × n) / (shares_old + n)

但真实账户的 `avg_cost` 是**不含买入费用**的（`calc_position` 只做
`price * n`），而用户心里的「成本」通常指**真金白银花出去的钱**。两个数
不一样，差额就是佣金 + 过户费。所以这里**两个都给**，不替用户选：

    avg_cost_gross  账面成本（不含费）—— 与持仓页显示的一致
    avg_cost_net    真实摊薄成本（含买入费用）—— 卖出时真正要回本的价

为什么要分开：卖出费用（印花税）只在卖出时收，所以 `avg_cost_net` 里只有
买入侧费用。如果把它算成「含双边费用」，用户会以为回本价更高，实际是错的。
卖出时那一次费用单独在 `exit_cost` 里给出。

## 分红/送转不建模
本模块只处理现金买卖。A 股分红送转会改变持股与成本，口径分散（除权日
前一日收盘价调整等），在这里估不准。要算含分红得走 `app.calc_position`
的真实流水回放 —— 所以本模块在 docstring 里明写这个边界，不假装覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class FeeConf:
    """交易费用。默认值与 `paper_trading.DEFAULT_FEES` 保持一致 ——
    两处独立定义会漂移，所以本模块的 `DEFAULT_FEES` 直接引用它。"""
    commission_rate: float = 0.00025      # 万2.5，双向
    commission_min: float = 5.0           # 单笔最低 5 元
    stamp_duty_sell: float = 0.0005       # 0.05%，仅卖出（2023-08-28 由 0.1% 减半）
    transfer_fee: float = 0.00001         # 0.001%，双向
    is_fund: bool = False                 # 场内基金免印花税


@dataclass
class Leg:
    """一笔买入。"""
    price: float
    shares: int
    note: str = ""


@dataclass
class Estimate:
    # 输入
    old_shares: int = 0
    old_avg_cost: float = 0.0
    old_cash_paid: float = 0.0        # 现有持仓**累计已付现金**（含历史所有费用）
    new_price: float = 0.0
    new_shares: int = 0
    market: str = "A"                 # "A" | "HK"
    fee: FeeConf = field(default_factory=FeeConf)
    current_price: float = 0.0        # 现价（可选，给回本涨幅用）

    # 输出
    new_shares_total: int = 0
    new_avg_cost_gross: float = 0.0   # 账面成本（不含费），与持仓页一致
    new_avg_cost_net: float = 0.0     # 真实摊薄成本（含买入费用）
    cost_delta: float = 0.0           # 账面成本变动（负=降本）
    cost_delta_pct: float = 0.0       # 相对原成本的降幅 %
    buy_commission: float = 0.0
    buy_transfer: float = 0.0
    buy_fees_total: float = 0.0
    total_cash_paid: float = 0.0      # 摊薄后累计已付现金（含全部费用）
    total_fees_paid: float = 0.0      # 累计已付费用（历史 + 本次）
    cash_needed: float = 0.0          # 本次买入总付出（含费）
    exit_cost_at_new_avg: float = 0.0  # 未来卖出这笔仓位时要付的费用
    break_even_from_now: float = 0.0    # 现价涨多少 % 才回本（含卖出费）
    dilution_ratio: float = 0.0         # 摊薄强度：摊薄后成本 / 原成本

    def to_dict(self) -> dict:
        """对外形态。**内部累计值不在计算途中四舍五入**（见 estimate 注释），
        只在这里收敛到分/厘，避免逐笔丢精度。"""
        d = asdict(self)
        d["fee"] = asdict(self.fee)
        for k in ("total_cash_paid", "total_fees_paid", "cash_needed",
                  "exit_cost_at_new_avg"):
            d[k] = round(float(d[k]), 2)
        return d


# ---------------------------------------------------------------------------
# 费用
# ---------------------------------------------------------------------------

def buy_fees(value: float, fee: FeeConf, market: str = "A") -> dict:
    """买入侧费用。买入**没有印花税**（A 股印花税仅卖出收）。

    value = 成交金额（不含费）。返回各项与合计。
    """
    value = max(0.0, float(value or 0))
    if value <= 0:
        return {"commission": 0.0, "transfer": 0.0, "total": 0.0}
    if market == "HK":
        # 港股印花税**双向**收，与 A 股不同（见 paper_trading.trade_fees）
        stamp = value * 0.001
        comm = max(value * fee.commission_rate, fee.commission_min)
        transfer = value * fee.transfer_fee
        total = comm + stamp + transfer
        return {"commission": round(comm, 4), "transfer": round(transfer, 4),
                "stamp_duty": round(stamp, 4), "total": round(total, 4)}
    comm = max(value * fee.commission_rate, fee.commission_min)
    transfer = value * fee.transfer_fee
    total = comm + transfer
    return {"commission": round(comm, 4), "transfer": round(transfer, 4),
            "stamp_duty": 0.0, "total": round(total, 4)}


def sell_fees(value: float, fee: FeeConf, market: str = "A") -> dict:
    """卖出侧费用。"""
    value = max(0.0, float(value or 0))
    if value <= 0:
        return {"commission": 0.0, "transfer": 0.0, "stamp_duty": 0.0, "total": 0.0}
    if market == "HK":
        return buy_fees(value, fee, market)
    comm = max(value * fee.commission_rate, fee.commission_min)
    stamp = 0.0 if fee.is_fund else value * fee.stamp_duty_sell
    transfer = value * fee.transfer_fee
    return {"commission": round(comm, 4), "transfer": round(transfer, 4),
            "stamp_duty": round(stamp, 4),
            "total": round(comm + stamp + transfer, 4)}


# ---------------------------------------------------------------------------
# 核心预估
# ---------------------------------------------------------------------------

def estimate(old_shares: int, old_avg_cost: float, new_price: float,
             new_shares: int, *, current_price: float = 0.0,
             market: str = "A", fee: FeeConf | None = None,
             old_cash_paid: float | None = None) -> Estimate:
    """预估「按 new_price 买 new_shares 股」之后的摊薄成本。

    参数按现有账本口径：old_shares 是**当前净持股**，old_avg_cost 是账面
    成本（不含费，与 /api/holdings 的 avg_cost 同口径）。

    `old_cash_paid` = 现有持仓**累计已付现金**（含历史所有费用）。
    **不传就退化成 old_avg_cost × old_shares，即假设历史费用为 0**。
    必须显式传，否则多笔加仓时历史费用会被逐轮忘记 ——
    `simulate()` 早期版本就有这个 bug：每步拿不含费的账面成本当下一轮的
    基数，10 笔各 100 股的净成本收敛到 10.005，而正确值是 10.0501
    （50 元佣金被"摊"掉了 90%）。

    四个成本概念，别混：
      1. new_avg_cost_gross   账面摊薄成本，与持仓页一致（不含任何费用）
      2. new_avg_cost_net     含买入费用的真实回本价
      3. total_fees_paid      累计已付费用（gross×股数 与 cash_paid 的差额）
      4. break_even_from_now  现价要涨多少才真正回本（含未来卖出费）
    """
    fee = fee or FeeConf()
    e = Estimate(old_shares=int(old_shares or 0),
                 old_avg_cost=float(old_avg_cost or 0.0),
                 new_price=float(new_price or 0.0),
                 new_shares=int(new_shares or 0),
                 market=market, fee=fee,
                 current_price=float(current_price or 0.0))
    # 历史已付现金：显式传就用真的，否则假设无历史费用
    e.old_cash_paid = (float(old_cash_paid) if old_cash_paid is not None
                       else e.old_avg_cost * e.old_shares)

    n_new, n_old = e.new_shares, e.old_shares
    e.new_shares_total = n_old + n_new
    if e.new_shares_total <= 0:
        return e

    # 1) 账面摊薄（不含费）—— 与 calc_position 同公式
    gross = (e.old_avg_cost * n_old + e.new_price * n_new) / e.new_shares_total
    e.new_avg_cost_gross = round(gross, 4)

    # 2) 本次买入费用
    buy_value = e.new_price * n_new
    f = buy_fees(buy_value, fee, market)
    e.buy_commission = f["commission"]
    e.buy_transfer = f.get("transfer", 0.0)
    e.buy_fees_total = f["total"]
    e.cash_needed = buy_value + e.buy_fees_total

    # 3) 真实摊薄成本 = 累计已付现金 / 总股数
    #    分子用 old_cash_paid（含历史费用），不是 old_avg_cost × n_old
    #
    # ⚠️ 这里**不四舍五入**。simulate() 每笔都把 total_cash_paid 传给下一笔，
    # 若这里 round 到 2 位，20 笔各 505.005 元就会逐笔丢 0.005，
    # 累计少算 0.1 元（实测：total_fees_paid 100.07，正确值 100.1）。
    # 只在 to_dict() 对外时收敛精度。
    e.total_cash_paid = e.old_cash_paid + buy_value + e.buy_fees_total
    e.new_avg_cost_net = round(e.total_cash_paid / e.new_shares_total, 4)
    # 累计费用 = 历史已付 + 本次；由 old_cash_paid 反推历史部分，
    # 而不是用「现金 - 账面成本」相减（后者在 old_avg_cost 与 old_cash_paid
    # 不自洽时会把差额全算成费用，得出离谱的数）
    old_fees = max(0.0, e.old_cash_paid - e.old_avg_cost * n_old)
    e.total_fees_paid = old_fees + e.buy_fees_total

    # 4) 成本变动（账面口径，与持仓页可比）
    e.cost_delta = round(e.new_avg_cost_gross - e.old_avg_cost, 4)
    if e.old_avg_cost > 0:
        e.cost_delta_pct = round(
            (e.new_avg_cost_gross / e.old_avg_cost - 1) * 100, 3)
        e.dilution_ratio = round(e.new_avg_cost_gross / e.old_avg_cost, 4)

    # 5) 未来卖出费用 → 回本价（解「卖出净收入 == 累计已付现金」）
    e.exit_cost_at_new_avg = sell_fees(
        e.new_avg_cost_gross * e.new_shares_total, fee, market)["total"]
    if e.current_price > 0:
        target_gross = _gross_price_for_net(
            e.total_cash_paid, e.new_shares_total, fee, market)
        if target_gross and target_gross > 0:
            e.break_even_from_now = round(
                (target_gross / e.current_price - 1) * 100, 3)
    return e


def _gross_price_for_net(total_cost: float, shares: int, fee: FeeConf,
                         market: str) -> float | None:
    """解「卖出 gross 价时，净收入 == total_cost」的 gross 价。

    卖出费含**最低佣金**和与金额无关的部分，所以不是纯线性，要迭代。
    直接闭式会忽略最低 5 元佣金对小额的影响（2000 元的单子差 0.25%）。
    用不动点迭代，20 次足够收敛（步长收缩）。
    """
    if shares <= 0:
        return None
    guess = total_cost / shares if shares else 0.0
    if guess <= 0:
        return None
    for _ in range(20):
        net = guess * shares - sell_fees(guess * shares, fee, market)["total"]
        diff = total_cost - net
        if abs(diff) < 0.005:
            break
        guess += diff / shares
        if guess <= 0:
            return None
    return guess


# ---------------------------------------------------------------------------
# 多次加仓 / 网格
# ---------------------------------------------------------------------------

def simulate(legs: list[Leg], *, start_shares: int = 0,
             start_avg_cost: float = 0.0, start_cash_paid: float | None = None,
             market: str = "A", fee: FeeConf | None = None) -> list[Estimate]:
    """按顺序模拟多笔加仓，每步返回一次预估（可画「成本下降曲线」）。

    分批加仓的实际摊薄效果**不等于**一次性买入的算术平均 —— 因为最低佣金
    是每笔收 5 元。同样 1000 股：1 笔 10000 元佣金 5 元，10 笔各 1000 元
    佣金 50 元。账面成本两者都是 10.0，真实回本价差 0.045 元/股。
    所以要逐步模拟，不能只算加权平均价。

    `start_cash_paid` 同 estimate()：不传则假设起始持仓无历史费用。
    每一步都会把 `total_cash_paid` 传给下一步，**历史费用不会丢**。
    """
    fee = fee or FeeConf()
    shares, avg = int(start_shares), float(start_avg_cost or 0.0)
    cash = (float(start_cash_paid) if start_cash_paid is not None
            else avg * shares)
    out = []
    for leg in legs:
        e = estimate(shares, avg, leg.price, leg.shares, market=market, fee=fee,
                     old_cash_paid=cash)
        out.append(e)
        shares, avg, cash = e.new_shares_total, e.new_avg_cost_gross, e.total_cash_paid
    return out


def ladder(old_shares: int, old_avg_cost: float, price: float, budgets: list[float],
           *, market: str = "A", fee: FeeConf | None = None,
           lot: int = 100, old_cash_paid: float | None = None) -> list[dict]:
    """按预算档位生成加仓计划（每档能买多少股、摊薄到多少）。

    `lot` = 最小买入单位。A 股买入必须 100 股整数倍（卖出可以是零股）。
    档位是**累计**的：第 2 档建立���第 1 档之上，反映「分批加仓」的实际路径。
    """
    fee = fee or FeeConf()
    out, shares, avg = [], int(old_shares), float(old_avg_cost or 0.0)
    cash = (float(old_cash_paid) if old_cash_paid is not None else avg * shares)
    for b in budgets:
        n = int(float(b) // (float(price) * lot)) * lot if price > 0 else 0
        if n <= 0:
            out.append({"budget": round(float(b), 2), "shares": 0,
                        "avg_after": round(avg, 4),
                        "avg_after_net": round(cash / shares, 4) if shares else 0.0,
                        "note": "预算不足一手"})
            continue
        e = estimate(shares, avg, price, n, market=market, fee=fee,
                     old_cash_paid=cash)
        # 金额一律 round 到分再输出：内部 total_fees_paid 是不四舍五入的累计值
        # （见 estimate 注释），直接吐给前端会出现 10.040000000000145 这种值
        out.append({"budget": round(float(b), 2), "shares": n,
                    "avg_after": e.new_avg_cost_gross,
                    "avg_after_net": e.new_avg_cost_net,
                    "cost_delta": e.cost_delta,
                    "cost_delta_pct": e.cost_delta_pct,
                    "total_fees_paid": round(e.total_fees_paid, 2),
                    "cash_needed": round(e.cash_needed, 2),
                    "note": ""})
        shares, avg, cash = e.new_shares_total, e.new_avg_cost_gross, e.total_cash_paid
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_lines(e: Estimate) -> list[str]:
    """人类可读的渲染。**所有取值走兜底格式化**，不让格式化层有能力弄丢
    数据（AGENTS.md「长任务」那条纪律）。

    ⚠️ 股数**不能**用 `format(v, 'd')`：股数字段声明是 int，但接口层可能
    传进来 float（比如从 JSON 或 pandas 取的），`format(1000.0, 'd')` 抛
    ValueError → 第一版全渲染成「— 股」，而持仓数和成本是这功能的核心。
    所以股数一律先转 int 再格式化。
    """
    def num(v, spec="", dash="—"):
        try:
            f = float(v)
            if f != f:            # NaN
                return dash
            return format(f, spec) if spec else str(round(f, 4))
        except (TypeError, ValueError):
            return dash

    def cnt(v, dash="—"):
        """股数：float/str 都能吃，坏值给破折号而不是崩。"""
        try:
            return format(int(round(float(v))), "d")
        except (TypeError, ValueError):
            return dash

    L = []
    L.append(f"当前：{cnt(e.old_shares)} 股 @ 账面成本 {num(e.old_avg_cost, '.4f')}")
    if e.new_shares <= 0:
        L.append("→ 未指定买入股数，无法预估")
        return L
    L.append(f"加仓：{cnt(e.new_shares)} 股 @ {num(e.new_price, '.3f')}"
             f"（含费需 {num(e.cash_needed, '.2f')} 元）")
    L.append(f"→ 持仓 {cnt(e.new_shares_total)} 股，"
             f"账面摊薄成本 {num(e.old_avg_cost, '.4f')} → "
             f"{num(e.new_avg_cost_gross, '.4f')}"
             f"（{num(e.cost_delta, '+.4f')}，{num(e.cost_delta_pct, '+.3f')}%）")
    L.append(f"   真实回本价（含全部买入费）{num(e.new_avg_cost_net, '.4f')}"
             f"，累计费用 {num(e.total_fees_paid, '.2f')} 元")
    if e.current_price > 0:
        L.append(f"   现价 {num(e.current_price, '.3f')}："
                 f"涨 {num(e.break_even_from_now, '+.3f')}% 回本"
                 f"（已含未来卖出费 {num(e.exit_cost_at_new_avg, '.2f')} 元）")
    return L