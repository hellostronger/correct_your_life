"""用库里真实抓到的策略源码，测 jq_api 能不能让它跑起来。

这是端到端的第一环：**不跑容器**，先在本地验证 API 适配层对不对。
如果这一步不过，容器跑得再干净也没意义。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd                                    # noqa: E402
import jq_api                                          # noqa: E402

print("=== jq_api 装进 builtins 后，裸函数风格的策略能不能跑 ===")

# 造一小段行情（模拟从云库切出来的切片）
dates = pd.bdate_range("2024-01-01", periods=120)
rng = pd.RangeIndex(0, 0)          # 固定可复现
import numpy as np                                     # noqa: E402
np.random.seed(7)


def mk(code: str, base: float, drift: float) -> pd.DataFrame:
    ret = np.random.normal(drift, 0.02, len(dates))
    close = base * np.cumprod(1 + ret)
    return pd.DataFrame({
        "open": close * (1 + np.random.normal(0, 0.004, len(dates))),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": np.random.randint(1e6, 9e6, len(dates)),
        "money": np.random.randint(1e7, 9e7, len(dates)),
    }, index=dates)


data = {
    "600000": mk("600000", 12.0, 0.0006),
    "600036": mk("600036", 38.0, 0.0004),
    "000001": mk("000001", 11.0, 0.0002),
    "600519": mk("600519", 1700.0, 0.0003),
}
codes = sorted(data)
jq_api.setup(data, codes, "600000", "2024-01-01", "2024-07-01", 1_000_000)
jq_api.JQ["start_cash"] = 1_000_000
print("  已注入 %d 只票 x %d 个交易日" % (len(codes), len(dates)))
print("  builtins 里的聚宽 API: %d 个" % len(jq_api.api_names()))

STRATEGY = '''
def initialize(context):
    g.max_n = 2
    g.th = 0.03
    set_order_cost(OrderCost(open_tax=0, close_tax=0.001,
                            open_commission=0.00025,
                            close_commission=0.00025, min_commission=5))
    set_slippage(FixedSlippage(3/10000))
    run_daily(before_open, time='9:26')
    log.info("初始化完成，最多持有 %d 只" % g.max_n)

def before_open(context):
    codes = [c for c in get_all_securities('stock')]
    df = get_price(codes, count=20, fields=['close', 'volume'])
    if df is None or (hasattr(df, "empty") and df.empty):
        return
    # 取涨得最好的 g.max_n 只，满仓等权
    chg = (df.xs('close', axis=1, level=1).iloc[-1] /
           df.xs('close', axis=1, level=1).iloc[0] - 1)
    picks = chg.sort_values(ascending=False).head(g.max_n).index.tolist()
    per = context.portfolio.available_cash / max(1, len(picks))
    for s in picks:
        cur = get_current_data()[s]
        if cur.paused or cur.last_price <= 0:
            continue
        order_target_value(s, per)
        log.info("买入 %s @ %.2f" % (s, cur.last_price))
'''

print()
print("=== 跑一个真实形态的策略（run_daily + 多标的 get_price + 下单）===")
g = {"__name__": "jq_strategy", "__builtins__": __builtins__}
exec(compile(STRATEGY, "strategy.py", "exec"), g)
g["initialize"](jq_api.JQ["context"])

equity = []
for day in dates[:60]:
    jq_api.JQ["current_dt"] = day
    jq_api.JQ["_curdata_cache"] = None
    try:
        g["before_open"](jq_api.JQ["context"])
    except Exception as exc:                      # noqa: BLE001
        print("  !! before_open 在 %s 抛异常: %s" % (day.date(), exc))
        import traceback
        traceback.print_exc()
        break
    mv = 0.0
    for code, p in jq_api.JQ["positions"].items():
        px = float(data[code].loc[:day, "close"].iloc[-1])
        p["close"] = px
        mv += p["amount"] * px
    jq_api.JQ["total_value"] = jq_api.JQ["cash"] + mv
    equity.append(jq_api.JQ["total_value"])

print("  成交 %d 笔" % len(jq_api.JQ["orders"]))
print("  被拒 %d 笔" % len(jq_api.JQ["rejected"]))
for r in jq_api.JQ["orders"][:4]:
    print("    %s %s %s x%d @%.2f 费%.2f" % (r["date"], r["side"], r["code"],
                                              r["shares"], r["price"], r["fee"]))
print("  最终权益 %.2f（初始 1000000，收益 %.2f%%）"
      % (jq_api.JQ["total_value"], (jq_api.JQ["total_value"] / 1e6 - 1) * 100))
print("  策略日志 %d 条，前 3 条:" % len(jq_api.JQ["log"]))
for l in jq_api.JQ["log"][:3]:
    print("    " + l[:100])
print("  语义差异/告警 %d 条:" % len(jq_api.JQ["warnings"]))
for w in sorted(set(jq_api.JQ["warnings"]))[:5]:
    print("    - " + w[:110])

print()
print("=== 未实现的 API 会不会静默失败（必须明确报错）===")
for bad in ("get_fundamentals", "get_index_stocks", "get_money_flow"):
    try:
        globals_try = {"get_fundamentals": jq_api.get_fundamentals,
                       "get_index_stocks": jq_api.get_index_stocks,
                       "get_money_flow": jq_api.get_money_flow}[bad]
        globals_try("x")
        print("  %-22s !! 没报错（静默失败，最危险）" % bad)
    except jq_api.JQError as exc:
        print("  %-22s OK 明确报错: %s" % (bad, str(exc)[:70]))
    except Exception as exc:                       # noqa: BLE001
        print("  %-22s OK 报错(%s): %s" % (bad, type(exc).__name__, str(exc)[:60]))
