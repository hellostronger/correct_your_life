# -*- coding: utf-8 -*-
"""沙箱内的执行器 —— 被 docker run 启动，跑完把结果打到 stdout。

它必须**只依赖** jq_api.py + pandas/numpy + 一个 CSV，别的什么都不依赖：
    - 不连数据库（网络在沙箱里是关的）
    - 不 import 本项目的任何东西
    - 只有一个入口 main()，结果用一个 `#RESULT <json>` 前缀的独占行输出

为什么用「前缀独占行」而不是直接把 JSON 打到 stdout
--------------------------------------------------
策略自己会 print（而且很可能 print 得很多）。如果直接把结果 JSON 打到
stdout，前面混进策略的输出就没法解析了。所以用一行前缀 + 只取最后一行
以它开头的输出。这个坑不实测想不到。

输出协议
--------
stdout 末尾（且仅末尾）有一行：
    #RESULT {json}
json 含：ok / error / traceback / metrics / trades / daily / warnings /
         rejected / unknown_apis / log_tail
"""
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr

RESULT_TAG = "#RESULT "


def _load_data(path: str) -> dict:
    """读数据切片。

    格式（由 sandbox_runner.py 写出）：CSV，第一列 date，其余列
    `code.open` / `code.high` / ... 的宽表。用 pivot 拆成 {code: DataFrame}。
    """
    import pandas as pd
    df = pd.read_csv(path, parse_dates=["date"])
    if df.empty:
        return {}
    df = df.set_index("date").sort_index()
    out = {}
    for col in df.columns:
        if "." not in col:
            continue
        code, field = col.split(".", 1)
        out.setdefault(code, {})[field] = df[col]
    res = {}
    for code, fields in out.items():
        d = pd.DataFrame(fields)
        # 复权/停牌导致的缺失用前值补：聚宽默认 fill_paused=True
        d = d.ffill()
        d.index.name = "date"
        res[code] = d
    return res


def _emit(payload: dict):
    """把结果作为最后一行输出。ensure_ascii=False 让中文可读。"""
    sys.stdout.write("\n" + RESULT_TAG
                     + json.dumps(payload, ensure_ascii=False,
                                  default=str) + "\n")
    sys.stdout.flush()


def _metrics(daily: list) -> dict:
    """从逐日净值算指标。跟本地 backtest.py 的口径保持一致 ——
    两边算法不一样的话，同一个策略在本地和沙箱跑出两个数，我没法判断
    哪个对，所以这里只算最基础的（总收益/年化/最大回撤/夏普/胜率），
    复杂指标统一由 backtest.py 负责。"""
    import numpy as np
    if not daily:
        return {}
    eq = np.array([d["equity"] for d in daily], dtype=float)
    if eq[0] <= 0:
        return {}
    total = float(eq[-1] / eq[0] - 1.0)
    days = len(eq)
    years = max(days / 244.0, 1e-9)      # A股约 244 个交易日/年
    ann = float((eq[-1] / eq[0]) ** (1.0 / years) - 1.0) if eq[-1] > 0 else -1.0
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    mdd = float(dd.min())
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    sharpe = float(rets.mean() / rets.std() * np.sqrt(244)) \
        if len(rets) > 2 and rets.std() > 0 else 0.0
    downside = rets[rets < 0]
    sortino = float(rets.mean() / downside.std() * np.sqrt(244)) \
        if len(downside) > 1 and downside.std() > 0 else 0.0
    return {
        "total_return": round(total, 6),
        "annual_return": round(ann, 6),
        "max_drawdown": round(mdd, 6),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "trading_days": days,
    }


def _trade_stats(trades: list) -> dict:
    """卖出胜率。注意口径：买入没有盈亏，胜率只能从**卖出的那一笔**算。
    第一版我按 side=1（买入）去算，胜率恒为 0。"""
    import numpy as np
    buys = {}
    sells = []
    for t in trades:
        if t["side"] == "buy":
            buys.setdefault(t["code"], []).append(t)
        else:
            sells.append(t)
    wins = losses = 0
    pnls = []
    for t in sells:
        b = buys.get(t["code"])
        if not b:
            continue
        # 移动平均成本（简化：按 FIFO 更准，这里用最近一笔买入近似）
        cost = b[-1]["price"]
        pnl = (t["price"] - cost) * t["shares"] - t.get("fee", 0.0)
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        else:
            losses += 1
    n = wins + losses
    return {
        "n_trades": len(trades),
        "n_sell_trades": len(sells),
        "win_rate": round(wins / n, 4) if n else 0.0,
        "avg_pnl_per_sell": round(float(np.mean(pnls)), 2) if pnls else 0.0,
        "total_pnl": round(float(np.sum(pnls)), 2) if pnls else 0.0,
    }


def _mark_to_market(data: dict, date):
    """按当日收盘给持仓估值。"""
    mv = 0.0
    for code, p in JQ_pos().items():
        df = data.get(code)
        if df is None:
            mv += p["amount"] * p["avg_cost"]
            continue
        d = df[df.index <= date]
        px = float(d["close"].iloc[-1]) if len(d) else p["avg_cost"]
        p["close"] = px
        mv += p["amount"] * px
    return mv


def JQ_pos():
    import jq_api
    return jq_api.JQ["positions"]


class _SimClock:
    """给策略用的 datetime —— today()/now() 返回**回测当天**，不是真实时间。

    为什么必须替换
    --------------
    社区策略里 `datetime.date.today()` 极常见，典型用法是
        if datetime.date.today().weekday() == 4:   # 周五调仓
    在真实回测里那是**我启动沙箱的那天**，于是「周五调仓」永远不触发、
    或者天天触发，而且**不报任何错**，只是结果错了。
    这类 bug 极难发现 —— 回测能跑完、能出净值，就是数字不对。

    所以在注入策略命名空间之前把它换掉。用 __getattr__ 转发其余属性，
    这样 timedelta / date(2020,1,1) 这类用法照常工作。
    """

    def __init__(self, jq_mod):
        import datetime as _real
        self._real = _real

    def _now(self):
        import datetime as _real
        cur = JQ_dt()["current_dt"]
        if cur is None:
            return _real.datetime.now()
        return _real.datetime(cur.year, cur.month, cur.day, 15, 0, 0)

    def today(self):
        return self._now().date()

    def now(self):
        return self._now()

    def date(self, *a, **kw):
        return self._real.date(*a, **kw)

    def datetime(self, *a, **kw):
        return self._real.datetime(*a, **kw)

    def timedelta(self, *a, **kw):
        return self._real.timedelta(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def JQ_dt():
    import jq_api
    return jq_api.JQ


def main() -> int:
    import jq_api

    t0 = time.time()
    cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    data_path = cfg.get("data")
    strat_path = cfg.get("strategy")
    start = cfg.get("start")
    end = cfg.get("end")
    cash = float(cfg.get("cash", 1_000_000))
    bench = cfg.get("benchmark")

    payload = {"ok": False}
    try:
        data = _load_data(data_path)
        if not data:
            raise RuntimeError("数据切片为空：%s" % data_path)
        codes = sorted(data)
        jq_api.setup(data, codes, bench, start, end, cash)
        jq_api.JQ["start_cash"] = cash

        # ---- 加载策略源码 ----
        src = open(strat_path, "r", encoding="utf-8").read()
        strategy_globals = {"__name__": "jq_strategy",
                            "__builtins__": __builtins__}
        # 注入「模拟时钟」的 datetime。
        # 策略很爱写 `datetime.date.today()` 来决定调仓日 —— 真实回测里
        # 那是**回测当天**而不是今天。不替换的话策略会以为「今天」永远
        # 是我启动它的那天，于是「每周五调仓」这类逻辑全错，而且**不报错**。
        strategy_globals["datetime"] = _SimClock(jq_api)
        exec(compile(src, "strategy.py", "exec"), strategy_globals)

        if "initialize" not in strategy_globals:
            raise RuntimeError("策略源码里没有 initialize(context) —— "
                               "这不是一个聚宽风格的策略")
        ctx = jq_api.JQ["context"]

        # ---- 逐日驱动 ----
        # 日历取所有证券日期的并集（实际就是基准/全市场的交易日）
        all_days = sorted(set().union(*[set(df.index) for df in data.values()]))
        if start:
            all_days = [d for d in all_days if str(d.date()) >= str(start)]
        if end:
            all_days = [d for d in all_days if str(d.date()) <= str(end)]
        if not all_days:
            raise RuntimeError("指定区间内没有交易日：%s ~ %s" % (start, end))

        # 聚宽的固定回调，社区策略用得最多的四个。这里按聚宽的真实顺序调：
        #     before_trading_start -> market_open -> (策略自己的 run_daily)
        #     -> handle_data -> after_trading_end -> market_close
        # 顺序错了会让「先算信号再下单」变成「先下单再算信号」，
        # 结果可能天差地别，所以这里显式排好。
        FIXED_CB = ["before_trading_start", "market_open",
                    "after_trading_end", "market_close", "handle_data"]
        # initialize 只在第一天调用一次
        jq_api.JQ["current_dt"] = all_days[0]
        buf = io.StringIO()
        init_out = ""
        init_failed = False
        init_tb = ""
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                strategy_globals["initialize"](ctx)
                init_out = buf.getvalue()
            except Exception:                 # noqa: BLE001
                # **两件事都必须在这个 except 里面做**：
                #  ① format_exc() 必须在 except 里调，出了 except 就没有
                #     活动异常了，拿到的是 "NoneType: None"（我踩过一次）
                #  ② 不能顺手 _emit()：此刻 stdout 还 redirect 到 buf，
                #     报告会被吞掉，进程 return 1 而 stdout 零字节，
                #     外面完全无从下手。所以只记下来，出 with 再 emit。
                init_failed = True
                init_tb = traceback.format_exc()
        if init_failed:
            payload = {"ok": False, "error": "initialize 抛异常",
                       "traceback": init_tb,
                       "init_log": buf.getvalue()[-4000:],
                       "elapsed": round(time.time() - t0, 2)}
            _emit(payload)
            return 1

        daily = []
        for idx_day, day in enumerate(all_days):
            jq_api.JQ["current_dt"] = day
            jq_api.JQ["_curdata_cache"] = None
            jq_api.JQ["_prev_trade_day"] = all_days[idx_day - 1] \
                if idx_day > 0 else day
            with redirect_stdout(buf), redirect_stderr(buf):
                for name in FIXED_CB:
                    fn = strategy_globals.get(name)
                    if fn is None:
                        continue
                    try:
                        if name == "handle_data":
                            fn(ctx, {})
                        else:
                            fn(ctx)
                    except Exception as exc:      # noqa: BLE001
                        payload.setdefault("callback_errors", []).append(
                            "%s %s: %s" % (day.date(), name, exc))
                        payload.setdefault("callback_traceback", {}).setdefault(
                            "%s@%s" % (name, day.date()),
                            traceback.format_exc()[-1500:])
                # run_daily 注册的函数。本地只有日线，所以每个交易日都调一次
                # （分钟级语义已在 jq_api.run_daily 里记成 warning）。
                for t, fname in jq_api.JQ["scheduled"].get("daily", []):
                    fn = strategy_globals.get(fname)
                    if fn is None:
                        continue
                    try:
                        fn(ctx)
                    except Exception as exc:      # noqa: BLE001
                        payload.setdefault("callback_errors", []).append(
                            "%s %s: %s" % (day.date(), fname, exc))
                        payload.setdefault("callback_traceback", {}).setdefault(
                            "%s@%s" % (fname, day.date()),
                            traceback.format_exc()[-1500:])
            # 收盘估值
            mv = _mark_to_market(data, day)
            jq_api.JQ["total_value"] = jq_api.JQ["cash"] + mv
            daily.append({"date": str(day.date()),
                          "equity": round(jq_api.JQ["total_value"], 2),
                          "cash": round(jq_api.JQ["cash"], 2),
                          "market_value": round(mv, 2),
                          "n_positions": len(jq_api.JQ["positions"]),
                          "holdings": [{"code": c, "amount": p["amount"],
                                        "close": round(p["close"], 3)}
                                       for c, p in jq_api.JQ["positions"].items()]})

        trades = jq_api.JQ["orders"]
        cb_errors = payload.get("callback_errors", []) or []
        cb_tb = payload.get("callback_traceback", {}) or {}
        # **静默失败必须当成失败**（我自己的原则，别自己破）
        # ---------------------------------------------------
        # 上面这版有个很典型的坑：回调逐日抛异常被 catch 住、只记进
        # callback_errors，而 run_daily 注册的**每一个交易日**都抛，
        # 结果 641 天全失败、0 笔成交，最后却报
        #     ok=true  total_return=0.0  trading_days=641
        # 我第一眼看到会以为「这策略不赚钱」—— 其实它**根本没跑**。
        # 这跟我在 get_index_stocks 上坚持的原则是同一条：宁可报错，
        # 不要「看起来正常的假结果」。
        # 判据：抽样看第一个交易日、以及最后一笔成交日之后的回调错误。
        ran_any = False
        for i, d in enumerate(daily):
            if any((" %s " % d["date"]) in e for e in cb_errors[:60]):
                ran_any = True
                break
        all_broken = bool(cb_errors) and not trades and not ran_any
        if all_broken:
            # 注意文案里的百分号要写成 %% —— 这段用的是 % 格式化，
            # 写成「收益 0%」会报 "not enough arguments for format string"，
            # 而且报错位置落在文案这一行，跟真正的问题完全无关。
            payload = {
                "ok": False,
                "error": "策略每个交易日都抛异常，等于没跑（%d 天 %d 个错误，"
                         "0 笔成交）。**不是「收益 0%%」，是没跑成。**"
                         % (len(daily), len(cb_errors)),
                "callback_errors": cb_errors[:30],
                "callback_traceback": {k: v for k, v in
                                       list(cb_tb.items())[:5]},
                "metrics": _metrics(daily),
                "n_days": len(daily),
                "elapsed": round(time.time() - t0, 2),
            }
            _emit(payload)
            return 1

        payload = {
            "ok": True,
            "metrics": _metrics(daily),
            "trade_stats": _trade_stats(trades),
            "trades": trades[:4000],
            "n_trades_total": len(trades),
            "daily": daily,
            "n_days": len(daily),
            "warnings": sorted(set(jq_api.JQ["warnings"]))[:60],
            "rejected": jq_api.JQ["rejected"][:200],
            "n_rejected": len(jq_api.JQ["rejected"]),
            "unknown_apis": dict(jq_api.JQ["unknown_calls"]),
            "log_tail": jq_api.JQ["log"][-200:],
            "init_log": init_out[-3000:],
            "strategy_log": buf.getvalue()[-6000:],
            "callback_errors": cb_errors[:30],
            "n_callback_errors": len(cb_errors),
            "callback_traceback": {k: v for k, v in list(cb_tb.items())[:5]},
            "elapsed": round(time.time() - t0, 2),
            "n_codes": len(codes),
        }
        _emit(payload)
        return 0
    except Exception:                          # noqa: BLE001
        payload = {"ok": False, "error": "runner 异常",
                   "traceback": traceback.format_exc()[-4000:],
                   "elapsed": round(time.time() - t0, 2)}
        _emit(payload)
        return 1


if __name__ == "__main__":
    sys.exit(main())
