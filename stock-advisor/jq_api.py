# -*- coding: utf-8 -*-
"""聚宽（JoinQuant）API 的本地实现 —— 让抓来的策略源码能在我的回测引擎里跑。

为什么必须有这一层
----------------
社区文章里的源码长这样：

    def initialize(context):
        g.max_hold_count = 1
        set_order_cost(OrderCost(close_tax=0.001))
        run_daily(before_open, time='9:26')
    def before_open(context):
        df = get_price(get_all_securities('stock'), '1d', count=60)
        df['ma20'] = df['close'].rolling(20).mean()
        order_target(s, 10000)

`initialize` / `get_price` / `order_target` / `OrderCost` / `context.portfolio`
**全是聚宽平台专有的**，别处一个都没有。直接扔进任何 Python 里跑，
第一句就 `NameError: name 'initialize' is not defined` —— 连「跑不起来」
都到不了。所以要么改写成我引擎的接口（那就得逐篇人工/LLM 改写，
慢且容易改错），要么**把平台 API 补上**（一次性投入，之后所有策略直接跑）。

选后者。这个模块就是那份「补上」。

设计要点
--------
1. **不 import 本项目的 backtest.py**。沙箱里要跑的是一个**独立副本**
   （镜像里只有 runner.py + 数据切片 + 策略源码），所以这个文件必须能
   单独拎出去用，只依赖 pandas/numpy。

2. **只实现策略真正会用到的那部分**。聚宽 API 有几百个函数，全实现不现实
   也不需要。实测社区策略高频用到的是下面这些（按我抓到的样本统计）：
   行情取数、选股、下单、成本设置、定时调度、持仓查询、日志。
   没实现的用一个**会明确报错的占位**顶上，而不是静默返回 None ——
   静默 None 会让策略「跑通了但结果是错的」，那比报错糟糕得多。

3. **语义对齐聚宽的真实行为**，尤其是几个容易踩的点：
   - `get_price` 返回**列是 ['open','high','low','close','volume','money'] 的
     DataFrame，且按证券代码升序**；多标的是 MultiIndex(证券, 字段)。
   - 复权：聚宽默认 `use_real_price=True`（前复权）。我本地存的是**不复权**
     日线（复权要靠除权数据算），所以这里明确按不复权处理，并在返回值里
     不假装是前复权 —— 差异写进 warnings 让上层知道。
   - `order_target` 是**目标市值**语义（不是股数），且当天买单当天算持仓。
   - `order_value` 是目标金额。`order` 是股数。
   - 涨停不能买、跌停不能卖：聚宽是真按这个规则拒单的，我这里也必须模拟，
     否则回测收益会虚高（这是国内策略回测最常见的注水来源）。

4. **数据从哪来**：沙箱里没有数据库，只有一份 CSV（由
   sandbox_runner.py 预先切好、只读挂载）。所以本模块只认内存里的
   `DATA` 全局字典，不自己连任何东西 —— 网络在沙箱里是关的。

不认识的 API 会记录到 `JQAPI.unknown_calls`，让上层能报「这个策略
用了我还没实现的 X」，而不是让它莫名失败。
"""
from __future__ import annotations

import datetime as _dt
import math
import sys
import traceback
from collections import defaultdict

import numpy as np
import pandas as pd

# ==========================================================================
# 全局状态（由 runner.py 注入数据后调用 setup 初始化）
# ==========================================================================

JQ = {
    "data": None,            # dict: {code: DataFrame[date, open..volume]}
    "codes": [],             # 全部证券代码
    "benchmark": None,       # 基准代码
    "start": None,           # date
    "end": None,             # date
    "cash": 0.0,             # 可用资金
    "total_value": 0.0,      # 总资产（策略自己改这个就能改杠杆）
    "positions": {},         # code -> {amount, avg_cost, close}
    "current_dt": None,      # 当前模拟时间
    "current_bar": None,     # 当前 bar（回测里就是日线，恒为 None）
    "log": [],               # 策略 log.info 收集到的
    "orders": [],            # 成交明细
    "rejected": [],          # 被拒的订单（涨停/停牌/资金不足）
    "warnings": [],          # 语义差异与未实现项
    "unknown_calls": defaultdict(int),
    "min_commission": 5.0,
    "scheduled": defaultdict(list),   # 'daily' -> [(time, func_name)]
    "g": None,               # 策略的 g 全局
    "portfolio": None,       # 见 Portfolio
    "context": None,         # 见 Context
    "finished": False,
}

FIELDS = ["open", "high", "low", "close", "volume", "money"]
PRICE_FIELDS = ("open", "high", "low", "close")


class JQError(Exception):
    """聚宽 API 侧的异常。runner.py 会把它当成「策略写错」而不是「引擎崩了」。"""


def setup(data: dict, codes: list, benchmark: str | None, start, end,
          cash: float) -> None:
    """runner.py 调一次，把数据灌进来。"""
    JQ["data"] = data
    JQ["codes"] = list(codes)
    JQ["benchmark"] = benchmark
    JQ["start"] = pd.Timestamp(start)
    JQ["end"] = pd.Timestamp(end)
    JQ["cash"] = float(cash)
    JQ["total_value"] = float(cash)
    JQ["g"] = _StrategyGlobal()
    JQ["portfolio"] = Portfolio()
    JQ["context"] = Context()
    # context.portfolio 必须真的挂上去。Context.__getattr__ 对未知属性是
    # 直接报错的（fail loud），而 portfolio 不设成实例属性的话，
    # 策略里第一句 context.portfolio.available_cash 就会炸
    # 「context 没有属性 'portfolio'」—— 看着像 API 没实现，其实是漏挂。
    JQ["context"].portfolio = JQ["portfolio"]
    _add_builtins()


class _StrategyGlobal:
    """策略里的 `g.xxx` 就是这里的属性。聚宽的 g 是一个全局命名空间，
    策略用 `g.max_hold_count = 1` 设置、`g.max_hold_count` 读取。"""

    def __repr__(self):
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return "<g %s>" % d


# runner 每推进一天就写这个，供 context.previous_date 用。
# 为什么不能直接 `current_dt - 1 day`：那是**日历日**不是交易日，
# 周一的前一天是周日。策略拿它算「上一个交易日」会算错。
def _set_prev_trade_day(day):
    JQ["_prev_trade_day"] = day


class Portfolio:
    """`context.portfolio` 的实现。"""

    @property
    def available_cash(self):
        return JQ["cash"]

    @property
    def cash(self):
        return JQ["cash"]

    @property
    def total_value(self):
        return JQ["total_value"]

    @property
    def market_value(self):
        return sum(p["amount"] * p["close"] for p in JQ["positions"].values())

    @property
    def positions(self):
        return list(JQ["positions"].values())

    @property
    def holdings(self):
        return self.positions

    @property
    def start_cash(self):
        return JQ["start_cash"]

    @property
    def returns(self):
        if not JQ["start_cash"]:
            return 0.0
        return JQ["total_value"] / JQ["start_cash"] - 1.0

    @property
    def daily_returns(self):
        return JQ.get("daily_returns", 0.0)

    @property
    def portfolio_value(self):
        return JQ["total_value"]

    def get_positions(self):
        return self.positions

    def position_cost_price(self, code):
        p = JQ["positions"].get(code)
        return p["avg_cost"] if p else 0.0

    # 聚宽的 Portfolio 是一组只读属性，用 __getattr__ 兜住剩下的
    def __getattr__(self, name):
        raise AttributeError("context.portfolio 没有属性 %r（未实现）" % name)


class Context:
    """`context` 对象。聚宽里 context 是个动态对象，策略随便加属性
    （`context.something = 1`），所以这里用 __setattr__ 开放。

    `current_dt` / `previous_date` 是社区策略最常问的两个（实测那篇微盘股
    复刻策略两个都用），必须真的返回**回测当天 / 上一交易日**，不能报
    「未实现」—— 那会让一大类策略直接跑不起来，而它们的逻辑其实我完全
    有能力支持。
    """

    @property
    def current_dt(self):
        dt = JQ["current_dt"]
        if dt is None:
            return None
        # 用 _dt（模块顶部是 import datetime as _dt），不是 datetime。
        # 这里写错过一次：属性是新增的，忘了跟文件其余部分一样用别名，
        # 结果 `name 'datetime' is not defined`。而这个 NameError 发生在
        # **策略调用 context.current_dt 时**，报错栈指向策略那一行，
        # 看起来像策略的问题，实际是我的 bug —— 排查时被带偏了一轮。
        return _dt.datetime(dt.year, dt.month, dt.day, 15, 0, 0)

    @property
    def previous_date(self):
        """上一**交易日**，返回 **date**（不是 datetime）。

        这个类型差别是实测踩出来的：社区策略写
            prev = context.previous_date
            return prev <= sf          # sf 是 datetime.date(...)
        我第一版返回 datetime，于是 `can't compare datetime.datetime to
        datetime.date` —— 而且它只在**调仓日**那条分支上炸，其余 640 天
        都正常，回测还「跑完了」。所以聚宽这两个属性的类型必须分清：
          current_dt    -> datetime
          previous_date -> date
        """
        dt = JQ["current_dt"]
        if dt is None:
            return None
        prev = JQ.get("_prev_trade_day") or dt
        return _dt.date(prev.year, prev.month, prev.day)

    @property
    def run_dt(self):
        return self.current_dt

    @property
    def run_freq(self):
        return "daily"

    @property
    def universe(self):
        return list(JQ["codes"])

    def __getattr__(self, name):
        raise AttributeError("context 没有属性 %r（未实现）" % name)

    def __repr__(self):
        dt = JQ["current_dt"]
        return "<context %s>" % (dt.date() if dt is not None else "?")


# ==========================================================================
# 行情取数
# ==========================================================================

def _slice(code: str, count: int, end: str | None, df: pd.DataFrame
           ) -> pd.DataFrame:
    """取某只票截至某天的最近 count 根日线。"""
    d = df
    if end is not None:
        d = d[d.index <= pd.Timestamp(end)]
    if count and count > 0:
        d = d.tail(count)
    return d


def _norm_field(f) -> str | list:
    """聚宽的 field 可以是 'close' 或 ['close','volume']。"""
    if f is None:
        return FIELDS
    if isinstance(f, str):
        return f
    if isinstance(f, (list, tuple)):
        return [str(x) for x in f]
    return FIELDS


def get_price(security, start_date=None, end_date=None, frequency="daily",
              fields=None, skip_paused=False, fq="pre", count=None,
              panel=True, fill_paused=True, is_panel=1, **kw):
    """聚宽 get_price 的实现（只支持日线，本地也只有日线）。

    与聚宽的差异（都写进 warnings，不藏着）：
    - **不支持分钟/秒线**。社区里依赖分钟线的策略（打板类很多）会拿到日线
      替代，结果不可信 -> 这里显式记一条 warning，由上层决定是否采信。
    - **不复权**。聚宽默认前复权；我本地存的是原始日线。差异会让含分红
      除权的策略结果偏大，所以必须记 warning。
    - 多标的返回 MultiIndex(证券, 字段)，与聚宽一致。
    """
    if frequency not in ("daily", "1d", "d", None):
        JQ["warnings"].append(
            "get_price(%r) 只支持日线，%s 被当成日线处理，结果不可信"
            % (frequency, frequency))
    if fq in ("pre", "post", "pre_after", "post_after"):
        JQ["warnings"].append(
            "get_price: 本地日线未做%s复权（聚宽默认前复权），"
            "含分红除权的标的结果会偏大" % ("前" if fq == "pre" else "后"))
    want = _norm_field(fields)
    want = [want] if isinstance(want, str) else list(want)
    bad = [x for x in want if x not in FIELDS]
    if bad:
        raise JQError("get_price: 不支持的字段 %s（可用：%s）" % (bad, FIELDS))

    # security 可以是单个代码、列表、或者 get_all_securities() 的返回
    if isinstance(security, str):
        codes = [_norm_code(security)]
        single = True
    elif isinstance(security, (list, tuple, set, pd.Index)):
        codes = [_norm_code(x) for x in security]
        single = False
    else:
        codes = [_norm_code(x) for x in (security or [])]
        single = False

    end = end_date or (JQ["current_dt"].strftime("%Y-%m-%d")
                       if JQ["current_dt"] is not None else None)
    # count 语义：聚宽是「截至 end_date 的最近 count 根」
    n = count
    if n is None and start_date is not None:
        try:
            n = (pd.Timestamp(end) - pd.Timestamp(start_date)).days + 1
        except Exception:                   # noqa: BLE001
            n = None
    if not single and n is None:
        n = 60                          # 策略没给 count 时给个默认，别直接空

    out = {}
    data = JQ["data"]
    for c in codes:
        df = data.get(c)
        if df is None or df.empty:
            continue
        s = _slice(c, n, end, df)
        if s.empty:
            continue
        if skip_paused:
            s = s[s["volume"] > 0]
        if fill_paused and len(s) < (n or 0):
            # 聚宽默认 fill_paused=True：停牌日用前值补齐
            full = _slice(c, n, end, data[c])
            if not full.empty:
                s = full.reindex(
                    pd.date_range(full.index[0], full.index[-1], freq="D")
                ).ffill().tail(n or len(full))
                if skip_paused:
                    s = s[s["volume"].fillna(0) > 0]
        out[c] = s[want].copy()

    if single:
        return out.get(codes[0], pd.DataFrame(columns=want))
    if not out:
        return pd.DataFrame()
    if len(want) == 1:
        df = pd.DataFrame({c: d[want[0]] for c, d in out.items()})
        return df
    frames = {}
    for c, d in out.items():
        for f in want:
            frames[(c, f)] = d[f]
    return pd.DataFrame(frames)


# 聚宽里这两个是 get_price 的别名，社区策略用得很多
history = get_price
get_bars = get_price


def attribute(security, attribute, dt=None, **kw):
    """取某个字段的历史序列（聚宽的 attribute）。

    返回 numpy 数组（聚宽就是这个语义，不是 DataFrame）。
    """
    df = get_price(security, end_date=dt, fields=attribute, count=kw.get("count"))
    if isinstance(df, pd.Series):
        return df.values
    if isinstance(df, pd.DataFrame) and attribute in df.columns:
        return df[attribute].values
    if isinstance(df, pd.DataFrame) and not df.empty:
        return df.iloc[:, 0].values
    return np.array([])


def get_billboard_list_industry(stocks, dt=None):
    raise JQError("get_billboard_list_industry 未实现（需要龙虎榜数据）")


def get_index_stocks(index_symbol, date=None):
    """指数成分股。**未实现，必须报错，不能返回空列表。**

    这里我第一版写的是「返回空 + 记一条 warning」，理由是「不想让策略崩」。
    结果被自己的测试判为「静默失败，最危险」，而且我没法反驳：

        for s in get_index_stocks('000300.XSHG'):
            buy(s)

    返回 [] 的后果不是「策略报错」，而是**策略一股都不买、净值一条直线**，
    回测「成功」返回 0% 收益。我看到 0% 会以为「这策略不赚钱」，
    实际是「数据没接上」。这比直接崩掉糟糕得多 —— 崩掉会让我去查，
    0% 只会让我去下一个策略。

    所以：缺数据 = 报错。要接指数成分股就把成分股表灌进 sa_market_kline
    的扩展表，或者把指数换成对应的 ETF 代码（本地已有 ETF 日线）。
    """
    raise JQError(
        "get_index_stocks(%s) 未实现：本地没有指数成分股数据。"
        "返回空列表会让策略空仓、回测「成功」返回 0%%，看起来像「不赚钱」"
        "其实是数据没接上 —— 所以这里明确报错。"
        "替代方案：把指数换成对应的 ETF 代码（本地已有 ETF 日线），"
        "或把成分股名单作为参数传进来。" % index_symbol)


def get_all_securities(types=(), date=None):
    """全部证券列表。

    **返回 DataFrame，按证券代码做索引**（列：display_name / name /
    start_date / end_date / type）—— 这不是随便定的：实测社区策略写的是
    `all_sec.index` 取代码、`df[df['type']=='stock']` 过滤，返回 dict 就
    直接 AttributeError。而聚宽官方就是返回 DataFrame，我第一版图省事
    返回了 dict。
    """
    rows = []
    for c in JQ["codes"]:
        rows.append({"display_name": c, "name": c, "start_date": None,
                     "end_date": None, "type": "stock"})
    if not rows:
        return pd.DataFrame(columns=["display_name", "name", "start_date",
                                     "end_date", "type"])
    df = pd.DataFrame(rows).set_index("display_name")
    if types:
        want = {str(t) for t in (types if isinstance(types, (list, tuple))
                                 else [types])}
        # 本地没有 bond/fund/etf 的分类，只能按代码前缀粗分
        if "stock" in want:
            df = df[df.index.map(
                lambda c: not str(c).startswith(("5", "15", "16", "50")))]
        JQ["warnings"].append(
            "get_all_securities(types=%s) 的分类是按代码前缀近似的，"
            "本地没有证券类型表" % (list(want),))
    return df


def get_all_securities_dict(types=(), date=None):   # 聚宽的新版命名
    return get_all_securities(types, date)


def get_current_data():
    """`get_current_data()[code].last_price` / `.paused` / `.is_st`。
    这里返回**截至当前模拟日**的快照。"""
    dt = JQ["current_dt"]
    cache = JQ.get("_curdata_cache")
    if cache and cache[0] == dt:
        return cache[1]
    cur = {}
    for c in JQ["codes"]:
        df = JQ["data"].get(c)
        if df is None or df.empty:
            continue
        d = df[df.index <= dt] if dt is not None else df
        if d.empty:
            continue
        row = d.iloc[-1]
        cur[c] = _CurrentData(c, row, _is_st_code(c))
    JQ["_curdata_cache"] = (dt, cur)
    return cur


class _CurrentData:
    __slots__ = ("_code", "_row", "paused", "is_st", "last_price", "high_limit",
                 "low_limit", "name")

    def __init__(self, code, row, is_st=False):
        self._code = code
        self._row = row
        self.paused = bool(row.get("volume", 0) == 0)
        self.is_st = is_st
        self.last_price = float(row["close"])
        self.name = code
        # 用当日 high/low 当涨跌停价：本地没有精确的涨停价，
        # 但策略只用它做「能不能买」的判断，近似够用（差额极小）
        self.high_limit = float(row["high"])
        self.low_limit = float(row["low"])

    def __repr__(self):
        return "<CurrentData %s %s%s>" % (self._code, self.last_price,
                                          " ST" if self.is_st else "")


def _is_st_code(code: str) -> bool:
    """用 JQ 的 ST 标记表判断（由数据切片注入）。没有表就当 False。"""
    return bool((JQ.get("st_codes") or set()).get(_norm_code(code)))


# ==========================================================================
# 下单
# ==========================================================================

def _price_now(code: str, at_price=None) -> float:
    if at_price:
        return float(at_price)
    p = JQ["positions"].get(code)
    if p:
        return float(p["close"])
    cur = get_current_data().get(code)
    if cur is None:
        raise JQError("取不到 %s 的当前价（是不是不在数据切片里？）" % code)
    return float(cur.last_price)


def _can_trade(code: str) -> tuple[bool, str]:
    """涨停不能买、跌停不能卖、停牌不能动。

    这条规则必须模拟：不模拟的话「打板/追涨停」类策略回测收益会虚高得离谱，
    因为现实中根本买不到。我本地没有精确的涨跌停价（只有当日 high/low），
    所以用「收盘价 == 当日最高价 且 涨幅接近 10%」这种保守近似，
    并记一条 warning 说明是近似。
    """
    cur = get_current_data().get(code)
    if cur is None:
        return False, "无行情"
    if cur.paused:
        return False, "停牌"
    return True, ""


def _is_limit_up(code: str) -> bool:
    """近似判定涨停：用「收盘 == 最高」且「涨幅 >= 9.5%」。"""
    cur = get_current_data().get(code)
    if cur is None:
        return False
    df = JQ["data"][code]
    d = df[df.index <= JQ["current_dt"]]
    if len(d) < 2:
        return False
    prev = float(d["close"].iloc[-2])
    c = float(d["close"].iloc[-1])
    if prev <= 0:
        return False
    return abs(c - cur.last_price) < 1e-9 and (c / prev - 1) >= 0.095


def _is_limit_down(code: str) -> bool:
    cur = get_current_data().get(code)
    if cur is None:
        return False
    df = JQ["data"][code]
    d = df[df.index <= JQ["current_dt"]]
    if len(d) < 2:
        return False
    prev = float(d["close"].iloc[-2])
    c = float(d["close"].iloc[-1])
    if prev <= 0:
        return False
    return abs(c - cur.last_price) < 1e-9 and (prev and (c / prev - 1) <= -0.095)


def _norm_code(code) -> str:
    """把 '000905.XSHG' / 'sh600519' 归一成 '600519' / '000905'。

    聚宽的代码带交易所后缀，我本地只存 6 位。**不归一的话
    `set_benchmark('000905.XSHG')` 之后的每一次取数都会落空**，
    而且不报错 —— 基准永远是空的，超额收益算出来是 0，看着还挺正常。
    """
    s = str(code).strip()
    if "." in s:
        s = s.split(".", 1)[0]
    if len(s) == 8 and s[:2].isalpha():
        s = s[2:]
    return s


def _fee(code: str, price: float, shares: float, is_buy: bool) -> float:
    """手续费。

    优先用 `set_order_cost(OrderCost(...))` 里策略自己设的费率（聚宽就是
    这个语义：策略设了就以策略为准）。没设就用聚宽默认：佣金万分之2.5、
    最低 5 元，卖出另加印花税千分之1。
    """
    amount = abs(shares * price)
    if amount <= 0:
        return 0.0
    c = JQ.get("_cost")
    if c is not None:
        if is_buy:
            rate = float(getattr(c, "open_commission", 0) or 0)
            rate += float(getattr(c, "open_tax", 0) or 0)
        else:
            rate = float(getattr(c, "close_commission", 0) or 0)
            rate += float(getattr(c, "close_tax", 0) or 0)
        lo = float(getattr(c, "min_commission", 0) or 0)
    else:
        rate = 0.00025 + (0.0 if is_buy else 0.001)
        lo = 5.0
    return max(amount * rate, lo)


def _exec_buy(code: str, shares: float, price: float) -> dict:
    shares = int(shares)
    if shares <= 0:
        raise JQError("买入股数必须为正：%s %s" % (code, shares))
    ok, why = _can_trade(code)
    if not ok:
        JQ["rejected"].append({"date": _ds(), "code": code, "side": "buy",
                               "reason": why})
        return {}
    if _is_limit_up(code):
        JQ["rejected"].append({"date": _ds(), "code": code, "side": "buy",
                               "reason": "涨停，无法买入（近似判定）"})
        return {}
    cost = shares * price + _fee(code, price, shares, True)
    if cost > JQ["cash"] + 1e-6:
        # 聚宽不会自动减量，直接拒单
        JQ["rejected"].append({"date": _ds(), "code": code, "side": "buy",
                               "reason": "资金不足：需 %.2f 可用 %.2f"
                                         % (cost, JQ["cash"])})
        return {}
    JQ["cash"] -= cost
    p = JQ["positions"].get(code)
    if p is None:
        p = JQ["positions"][code] = {"code": code, "amount": 0,
                                     "avg_cost": 0.0, "close": price}
    total = p["amount"] + shares
    p["avg_cost"] = (p["avg_cost"] * p["amount"] + price * shares) / total
    p["amount"] = total
    p["close"] = price
    rec = {"date": _ds(), "code": code, "side": "buy", "shares": shares,
           "price": price, "fee": _fee(code, price, shares, True)}
    JQ["orders"].append(rec)
    return rec


def _exec_sell(code: str, shares: float, price: float) -> dict:
    shares = int(shares)
    p = JQ["positions"].get(code)
    if p is None or p["amount"] <= 0:
        JQ["rejected"].append({"date": _ds(), "code": code, "side": "sell",
                               "reason": "无持仓"})
        return {}
    shares = min(shares, p["amount"])
    if shares <= 0:
        return {}
    ok, why = _can_trade(code)
    if not ok:
        JQ["rejected"].append({"date": _ds(), "code": code, "side": "sell",
                               "reason": why})
        return {}
    if _is_limit_down(code):
        JQ["rejected"].append({"date": _ds(), "code": code, "side": "sell",
                               "reason": "跌停，无法卖出（近似判定）"})
        return {}
    fee = _fee(code, price, shares, False)
    JQ["cash"] += shares * price - fee
    p["amount"] -= shares
    p["close"] = price
    rec = {"date": _ds(), "code": code, "side": "sell", "shares": shares,
           "price": price, "fee": fee}
    JQ["orders"].append(rec)
    if p["amount"] <= 0:
        JQ["positions"].pop(code, None)
    return rec


def _ds() -> str:
    return JQ["current_dt"].strftime("%Y-%m-%d") if JQ["current_dt"] else ""


def order(security, amount, style="normal", at_price=None, **kw):
    """按股数下单。amount>0 买、<0 卖。"""
    code = _norm_code(security)
    price = _price_now(code, at_price)
    if amount > 0:
        return _exec_buy(code, amount, price)
    return _exec_sell(code, -amount, price)


def order_value(security, value, style="normal", at_price=None, **kw):
    """按金额下单（value>0 买入 value 元）。"""
    code = _norm_code(security)
    price = _price_now(code, at_price)
    if price <= 0:
        raise JQError("%s 价格异常（%s）" % (code, price))
    shares = int(value / price / 100) * 100     # A股按手
    if value > 0 and shares <= 0:
        shares = 100
    return order(code, shares, style, at_price)


def order_target(security, amount, style="normal", at_price=None, **kw):
    """调仓到目标**市值**（不是股数）。聚宽就是这个语义。"""
    code = _norm_code(security)
    price = _price_now(code, at_price)
    p = JQ["positions"].get(code)
    cur_amount = p["amount"] if p else 0
    target_amount = int(amount / price / 100) * 100 if amount > 0 else 0
    diff = target_amount - cur_amount
    if diff == 0:
        return {}
    return order(code, diff, style, at_price)


def order_target_value(security, amount, style="normal", at_price=None, **kw):
    """调仓到目标市值。"""
    code = _norm_code(security)
    price = _price_now(code, at_price)
    p = JQ["positions"].get(code)
    cur_mv = p["amount"] * price if p else 0.0
    diff = float(amount) - cur_mv
    if abs(diff) < 1:
        return {}
    return order_value(code, diff, style, at_price)


def order_target_percent(security, percent, style="normal", at_price=None, **kw):
    return order_target_value(security, JQ["total_value"] * float(percent),
                              style, at_price)


# ==========================================================================
# 成本 / 滑点 / 调度
# ==========================================================================

class OrderCost:
    """聚宽 OrderCost。参数按官方签名给全。

    `close_today_commission` 是**实测踩出来的**：我第一版只写了
    open/close tax + open/close commission + min_commission，结果社区里
    一篇《万得微盘股指数复刻策略》的 initialize 直接
        TypeError: OrderCost.__init__() got an unexpected keyword argument
                   'close_today_commission'
    连 initialize 都没跑起来。所以这里按官方签名写全。

    额外吃 **kwargs：未知参数不当错误，而是记一条 warning。
    为什么 —— 未知成本参数的后果是「这个策略的费算得不准」，而不是
    「结果完全错」；报错的代价（整个策略跑不了）比不准大得多。
    这跟 get_index_stocks 那种「静默返回空 -> 回测显示 0% 收益、
    看起来像不赚钱」的情况不一样，所以这里选 warning。
    """

    KNOWN = ("open_tax", "close_tax", "open_commission", "close_commission",
             "close_today_commission", "min_commission", "type")

    def __init__(self, open_tax=0.0, close_tax=0.0, open_commission=0.0,
                 close_commission=0.0, close_today_commission=0.0,
                 min_commission=0.0, type="fund", **kw):
        self.open_tax = open_tax
        self.close_tax = close_tax
        self.open_commission = open_commission
        self.close_commission = close_commission
        self.close_today_commission = close_today_commission
        self.min_commission = min_commission
        self.type = type
        self.unknown = {k: v for k, v in kw.items() if k not in self.KNOWN}
        if self.unknown:
            JQ["warnings"].append(
                "OrderCost 收到未建模的成本参数 %s —— 已忽略，"
                "该策略的手续费会算得不准（但不会跑不了）"
                % ", ".join(sorted(self.unknown)))

    def __repr__(self):
        return ("<OrderCost 开税%s 闭税%s 开佣%s 闭佣%s 当日平仓佣%s 最低%s>"
                % (self.open_tax, self.close_tax, self.open_commission,
                   self.close_commission, self.close_today_commission,
                   self.min_commission))


def set_order_cost(cost, type="stock"):
    """接受策略自己构造的 OrderCost，按里面的费率覆盖默认值。"""
    if not isinstance(cost, OrderCost):
        raise JQError("set_order_cost 第一个参数必须是 OrderCost(...)，"
                      "收到 %r" % type(cost).__name__)
    JQ["_cost"] = cost
    JQ["min_commission"] = float(cost.min_commission or 0) or 0.0
    JQ["_open_tax"] = float(cost.open_tax or 0)
    JQ["_close_tax"] = float(cost.close_tax or 0)
    JQ["_open_comm"] = float(cost.open_commission or 0)
    JQ["_close_comm"] = float(cost.close_commission or 0)
    return cost


def set_slippage(slippage, type="stock"):
    """滑点。FixedSlippage(3/10000) -> 固定 3bp；PriceRelatedSlippage(0.002) -> 比例。
    这里记下来，在成交价上应用。"""
    JQ["_slippage"] = slippage
    JQ.setdefault("_slip_kw", {})[type] = slippage
    return slippage


class FixedSlippage:
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return "<FixedSlippage %s>" % self.value


class PriceRelatedSlippage:
    def __init__(self, ratio):
        self.ratio = ratio

    def __repr__(self):
        return "<PriceRelatedSlippage %s>" % self.ratio


class StepRelatedSlippage:
    def __init__(self, price_step=0.01, tick=0.01):
        self.price_step = price_step
        self.tick = tick


def run_daily(func, time="every_bar", reference_security="000300.XSHG"):
    """日频调度。本地只有日线，所以 time 只在 '开盘'/'收盘前' 这类粗粒度有意义，
    精确到分钟的 time 会被记成 warning（打板类策略大量依赖它）。"""
    JQ["scheduled"]["daily"].append((str(time), getattr(func, "__name__", "?")))
    if ":" in str(time):
        JQ["warnings"].append(
            "run_daily(time=%r) 精确到分钟，但本地只有日线 —— 该策略的"
            "日内择时部分无法复现，结果不可信" % time)


def run_weekly(func, day="monday", time="open", **kw):
    JQ["scheduled"]["weekly"].append((str(day), str(time),
                                     getattr(func, "__name__", "?")))


def run_monthly(func, monthday=1, time="open", **kw):
    JQ["scheduled"]["monthly"].append((str(monthday), str(time),
                                       getattr(func, "__name__", "?")))


# ==========================================================================
# 日志 / 杂项
# ==========================================================================

class _Log:
    """策略里的 `log.info(...)` / `log.warn(...)`。

    注意：Python 不允许 `def log.info(...)` 这种写法（函数名里不能有���），
    所以只能做成一个对象。社区策略里 log 的用法很杂（有的 log.info 不带
    参、有的带 % 格式、有的用 f-string），这里统一成「参数拼成一行」，
    不做 % 格式化 —— 因为格式串和参数分开传进来时，拼接会丢信息，
    而保留原文对排查更有用。
    """

    MAX = 4000            # 防止策略疯狂 log 把内存打满

    def _add(self, level, args):
        if len(JQ["log"]) < self.MAX:
            JQ["log"].append(level + " ".join(str(x) for x in args))
        elif len(JQ["log"]) == self.MAX:
            JQ["log"].append("... (log 已截断)")

    def info(self, *a):
        self._add("", a)

    def warn(self, *a):
        self._add("[warn] ", a)

    warning = warn

    def error(self, *a):
        self._add("[error] ", a)

    def debug(self, *a):
        self._add("[debug] ", a)


log = _Log()


def set_benchmark(security):
    JQ["benchmark"] = _norm_code(security)


def set_option(option, value):
    JQ.setdefault("options", {})[str(option)] = value


def set_max_stock_num(num):
    JQ["max_stock_num"] = int(num)


def set_min_stock_num(num):
    JQ["min_stock_num"] = int(num)


def is_st(code, date=None):
    cur = get_current_data().get(_norm_code(code))
    return bool(cur and cur.is_st)


def is_st_stock(code, date=None):
    return is_st(code, date)


def is_suspended(code, count=1, date=None):
    cur = get_current_data().get(_norm_code(code))
    return bool(cur and cur.paused)


def get_extras(info, code_list, df=True, start_date=None, end_date=None,
               **kw):
    """`get_extras('is_st', codes, df=True)` —— 小市值策略必用。

    只支持 is_st：本地没有 ST 标记表，所以 ST 是从数据切片注入的
    `st_codes` 里查（切片生成时会带上）。**查不到就当 False**，
    也就是「假装没有 ST 股」—— 这里必须在 warnings 里明说，
    因为它会让策略**多买一些本来买不到的票**，收益偏高。
    """
    info = str(info)
    if info not in ("is_st", "paused", "is_halted"):
        raise JQError(
            "get_extras(%r) 未实现。已支持的是 is_st / paused / is_halted。"
            "ST 标记来自数据切片里的 st_codes，本地没有独立的 ST 标记表 —— "
            "所以这个判断是**不完整**的（查不到的按非 ST 处理），"
            "会让策略多买一些实际买不到的票，回测收益偏高。" % info)
    if not JQ.get("st_codes"):
        JQ["warnings"].append(
            "get_extras(%s) 拿不到 ST 标记（数据切片没带 st_codes）—— "
            "所有标的都按非 ST 处理，回测收益会偏高" % info)
    codes = [_norm_code(c) for c in (code_list or [])]
    out = {}
    for c in codes:
        if info == "is_st":
            out[c] = 1 if _is_st_code(c) else 0
        elif info in ("paused", "is_halted"):
            cur = get_current_data().get(c)
            out[c] = 1 if (cur and cur.paused) else 0
    if df:
        return pd.DataFrame([out], index=["is_st" if info == "is_st" else info])
    return out


class _Query:
    """`query(valuation.code, valuation.market_cap)` 的产物。

    聚宽里 `query()` 只是描述「要哪些字段」，真正干活的是 get_fundamentals。
    这里提供一个能构造、能打印的占位，让 `get_fundamentals` 能给出**明确**
    的错误 —— 否则策略会先死在 `NameError: name 'query' is not defined`
    上，看不出真正缺的是市值数据。
    """

    def __init__(self, *fields):
        self.fields = [getattr(f, "_path", str(f)) for f in fields]

    def __repr__(self):
        return "<query %s>" % ", ".join(self.fields)


class _Field:
    """`valuation.market_cap` 这种字段引用。"""

    def __init__(self, path):
        self._path = path

    def __getattr__(self, name):
        return _Field("%s.%s" % (self._path, name))

    def __repr__(self):
        return "<field %s>" % self._path

    def __hash__(self):
        return hash(self._path)

    def __eq__(self, other):
        return isinstance(other, _Field) and other._path == self._path


def query(*fields):
    return _Query(*fields)


# 聚宽的表名空间。未实现的表给一个空命名空间，让 get_fundamentals 去报
# 「这个字段我没数据」而不是「valuation 这个名字不存在」。
valuation = _Field("valuation")
income = _Field("income")
balance = _Field("balance")
cash_flow = _Field("cash_flow")
fundamentals = _Field("fundamentals")
market = _Field("market")


def get_fundamentals(query, **kw):
    """财务/估值数据。**未实现，必须报错。**

    实测有三篇社区策略都是靠它选股的：
      q = query(valuation.code, valuation.market_cap)   # 微盘/小市值类
      q = query(valuation.code, valuation.pe_ratio)     # 低估值类

    为什么必须报错而不是瞎填：返回一个假的 market_cap（比如「价格 ×
    常数」），策略会按它排序，选出来的票完全随机，而回测照样「成功」
    返回一个漂亮的数字。那比直接报错糟糕得多 —— 报错会让我去补数据，
    假数据会让我相信一个完全无意义的结论。
    """
    fields = getattr(query, "fields", None)
    if fields is None:
        fields = [str(query)]
    raise JQError(
        "get_fundamentals(%s) 未实现：本地没有财务/估值数据表。"
        "**不能拿假数据凑** —— 小市值/低估值类策略靠这些字段排序，"
        "填假值等于随机选股，回测照样「成功」但结果毫无意义。"
        "补齐办法：把估值数据（总市值/流通市值/PE/PB）灌进 "
        "sa_stock_valuation 表，再扩展本函数。"
        % ", ".join(str(f) for f in fields))


def get_fundamentals_continuously(query, **kw):
    raise JQError("get_fundamentals_continuously 未实现")


def get_money_flow(day=None):
    raise JQError("get_money_flow 未实现（本地没有资金流数据）")


def get_industry(code_or_name, date=None):
    raise JQError("get_industry 未实现（本地没有行业分类数据）")


def get_industry_stocks(industry, date=None):
    raise JQError("get_industry_stocks 未实现")


def get_valuation(code, date=None):
    cur = get_current_data().get(code)
    if cur is None:
        raise JQError("取不到 %s 行情" % code)
    return {"pe_ratio": float("nan"), "pb_ratio": float("nan"),
            "ps_ratio": float("nan"), "pcf_ratio": float("nan"),
            "capitalization": float("nan"), "market_cap": float("nan"),
            "last_price": cur.last_price}


def get_turnover_rate(code, date=None):
    return 0.0


def get_slope(code, end_date=None, days=1, frequency="daily", field="close"):
    s = attribute(code, field, dt=end_date, count=days + 1)
    if len(s) < 2 or s[-2] == 0:
        return 0.0
    return float(s[-1] / s[-2] - 1)


def get_drawdown(code, end_date=None, days=1, frequency="daily", field="close"):
    s = attribute(code, field, dt=end_date, count=days)
    if len(s) == 0:
        return 0.0
    return float(s[-1] / max(s) - 1)


def get_market_cap(code, date=None):
    return 0.0


def get_circulating_market_cap(code, date=None):
    return 0.0


def get_price_change_rate(code, start_date=None, end_date=None):
    s = attribute(code, "close", dt=end_date, count=2)
    if len(s) < 2 or s[-2] == 0:
        return 0.0
    return float(s[-1] / s[-2] - 1)


def filter(security_list, filter_func, *args, **kw):
    return [c for c in security_list if filter_func(c, *args, **kw)]


def get_zipline_data(*a, **kw):
    raise JQError("get_zipline_data 未实现")


def get_jiqin_quota(date=None):
    return 10000.0


def use_real_price(flag=True):
    """聚宽的复权开关。本地日线未复权，明确告知而不是假装支持。"""
    if flag:
        JQ["warnings"].append(
            "use_real_price(True) 已忽略：本地日线是不复权原始价，"
            "与聚宽的前复权序列对比会产生系统性偏差")


# ==========================================================================
# 把 API 装进 builtins —— 聚宽策略是「裸函数」风格，不 import 任何东西
# ==========================================================================

def _add_builtins():
    import builtins
    public = {}
    for k, v in globals().items():
        if k.startswith("_") or k in ("pd", "np", "math", "sys", "traceback",
                                      "defaultdict", "_dt"):
            continue
        if k in ("setup", "JQ", "JQError", "FIELDS", "PRICE_FIELDS",
                 "Portfolio", "Context", "code_blocks"):
            continue
        public[k] = v
    for k, v in public.items():
        setattr(builtins, k, v)
    builtins.JQ = JQ
    builtins.g = JQ["g"]
    builtins.log = _Log()
    return sorted(public)


def api_names() -> list[str]:
    """已实现的 API 名单（给「这个策略用了我没实现的东西」的报告用）。"""
    return _add_builtins()
