# -*- coding: utf-8 -*-
"""strategy_gate 判据单测。零依赖，不需要起服务。

跑法（在 stock-advisor 目录下）：
    python _gate_test.py

为什么要有这个：判据全是**阈值**，而阈值最容易被后来的一次「顺手调一下」改坏。
而且这里踩过一个具体的坑 —— trade_stats 里卖出笔数的键名是 n_sell_trades，
写成 n_sells 取不到值会退回「成交总数」（买入+卖出），1778 被当成样本数，
min_trades=30 于是永远不触发。这类「键名写错但测试也用错键名」的错误
只有断言真实形状才抓得到。
"""
import sys

# Windows 控制台默认 GBK，打不出 ⚠ / ✅ 这类符号（UnicodeEncodeError 会把
# 测试结果本身搞挂 —— 比被测代码挂了更难查）。强制 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                  # noqa: BLE001
    pass

import strategy_gate as G

FAILS = []


def chk(name, cond, extra=""):
    print("  " + ("PASS" if cond else "FAIL") + "  " + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def src(**kw):
    """一份「干净且能跑」的源码侧：语法过、没脱敏、有 initialize。"""
    base = {"syntax_ok": True, "syntax_state": "ok", "redacted": False,
            "has_initialize": True, "n_funcs": 3,
            "needs_all_market": False, "universe_size": 400}
    base.update(kw)
    return base


def run(**kw):
    base = {"ok": True, "error": "", "metrics": {}, "trade_stats": {},
            "warnings": [], "n_rejected": 0}
    base.update(kw)
    return base


GOOD = {"metrics": {"sharpe": 1.2, "total_return": 0.30, "annual_return": 0.15,
                    "max_drawdown": -0.12, "trading_days": 480},
        "trade_stats": {"n_sell_trades": 200, "win_rate": 0.55}}


def codes(j):
    return [f["code"] for f in j["flags"]]


print("=== 1. 基准：一个各项都正常的策略应该 pass ===")
j = G.judge(src(), run(**GOOD))
chk("verdict=pass", j["verdict"] == "pass", j["verdict"])
chk("无 flag", codes(j) == [], codes(j))
chk("score=3", j["score"] == 3, j["score"])
chk("usable", G.usable(j) is True)

print()
print("=== 2. 硬否决 ===")
j = G.judge(src(syntax_ok=False, syntax_state="fragment"),
            run(**GOOD))
chk("语法不通过 -> reject", j["verdict"] == "reject", j["verdict"])
chk("点名 syntax", "syntax" in codes(j), codes(j))
chk("score=1", j["score"] == 1, j["score"])
chk("reject 不可用", G.usable(j) is False)

j = G.judge(src(redacted=True), run(**GOOD))
chk("脱敏 -> reject", j["verdict"] == "reject", j["verdict"])
chk("点名 redacted", "redacted" in codes(j), codes(j))

j = G.judge(src(needs_all_market=True, universe_size=400), run(**GOOD))
chk("全市场被截断 -> reject", j["verdict"] == "reject", j["verdict"])
chk("点名 universe_truncated", "universe_truncated" in codes(j), codes(j))

j = G.judge(src(), run(ok=False, error="RuntimeError: 没有 initialize"))
chk("运行失败 -> reject", j["verdict"] == "reject", j["verdict"])
chk("错误原文进了 blocker",
    any("initialize" in b for b in j["blockers"]), j["blockers"])

print()
print("=== 3. 没有 initialize ===")
j = G.judge(src(has_initialize=False, n_funcs=2), run(**GOOD))
chk("缺 initialize -> reject", j["verdict"] == "reject", j["verdict"])
chk("点数说清是几个函数",
    any("2 个函数" in b for b in j["blockers"]), j["blockers"])

j = G.judge(src(has_initialize=False, n_funcs=0), run(**GOOD))
chk("0 函数 -> reject", j["verdict"] == "reject")
chk("0 函数说清是参数片段",
    any("参数片段" in b for b in j["blockers"]), j["blockers"])

print()
print("=== 4. 扣分项阈值（含边界）===")
T = G.THRESHOLDS

# 夏普：低于 min_sharpe 扣分，正好等于不扣
j = G.judge(src(), run(**{**GOOD, "metrics": {**GOOD["metrics"],
                                              "sharpe": T["min_sharpe"]}}))
chk("夏普恰好等于门槛 -> 不扣", codes(j) == [], codes(j))
j = G.judge(src(), run(**{**GOOD, "metrics": {**GOOD["metrics"],
                                              "sharpe": T["min_sharpe"] - 0.01}}))
chk("夏普低于门槛 -> 扣分", "low_sharpe" in codes(j), codes(j))
chk("扣分 -> partial（不是 reject）", j["verdict"] == "partial", j["verdict"])

# 回撤：门槛是**幅度**，所以用 abs 比；-25% 不扣、-26% 扣
j = G.judge(src(), run(**{**GOOD, "metrics": {**GOOD["metrics"],
                                              "max_drawdown": -0.25}}))
chk("回撤 -25% -> 不扣", codes(j) == [], codes(j))
j = G.judge(src(), run(**{**GOOD, "metrics": {**GOOD["metrics"],
                                              "max_drawdown": -0.26}}))
chk("回撤 -26% -> 扣分", "deep_drawdown" in codes(j), codes(j))

# 样本量按**卖出**笔数
j = G.judge(src(), run(**{**GOOD, "trade_stats": {"n_sell_trades": 29,
                                                  "win_rate": 0.6}}))
chk("卖出 29 笔 -> 扣分", "thin_sample" in codes(j), codes(j))
j = G.judge(src(), run(**{**GOOD, "trade_stats": {"n_sell_trades": 30,
                                                  "win_rate": 0.6}}))
chk("卖出 30 笔 -> 不扣", codes(j) == [], codes(j))
j = G.judge(src(), run(**{**GOOD, "trade_stats": {"n_sell_trades": 0}}))
chk("零卖出 -> 点名零成交", "no_trades" in codes(j), codes(j))
# 回归：键名必须是 n_sell_trades。这里用「成交 1778 / 卖出 545」的真实形状，
# 若实现写错成 n_sells，会退化成 1778 而永不触发 thin_sample。
j = G.judge(src(), run(**{**GOOD, "trade_stats": {"n_trades": 1778,
                                                  "n_sell_trades": 545,
                                                  "win_rate": 0.7376}}))
chk("真实形状(1778/545) -> 按 545 判，不扣",
    codes(j) == [], codes(j))

# 窗口太短
j = G.judge(src(), run(**{**GOOD, "metrics": {**GOOD["metrics"],
                                              "trading_days": 59}}))
chk("回测 59 天 -> 扣分", "short_window" in codes(j), codes(j))

# 拒单过多
j = G.judge(src(), run(**{**GOOD, "n_rejected": 601}))
chk("拒单 601 vs 卖出 200 -> 扣分", "many_rejects" in codes(j), codes(j))
j = G.judge(src(), run(**{**GOOD, "n_rejected": 600}))
chk("拒单 600 = 卖出 200×3 -> 不扣（严格大于）", codes(j) == [], codes(j))
j = G.judge(src(), run(**{**GOOD, "n_rejected": 100}))
chk("拒单 100 vs 卖出 200 -> 不扣", codes(j) == [], codes(j))

# 数据缺口（沙箱自己说的）
j = G.judge(src(), run(**{**GOOD, "warnings": [
    "get_extras(is_st) 拿不到 ST 标记（数据切片没带 st_codes）—— 所有标的都按非 ST 处理，回测收益会偏高"]}))
chk("沙箱警告里的数据缺口 -> 扣分", "data_gap" in codes(j), codes(j))

# 日内择时不可复现是**硬否决**（不是扣分）
j = G.judge(src(), run(**{**GOOD, "warnings": [
    "run_daily(time='14:50') 精确到分钟，但本地只有日线 —— 该策略的日内择时部分无法复现，结果不可信"]}))
chk("日内不可复现 -> reject（硬否决）", j["verdict"] == "reject", j["verdict"])
chk("点名 intraday_unreproducible", "intraday_unreproducible" in codes(j),
    codes(j))

print()
print("=== 5. 扣分累加 ===")
j = G.judge(src(), run(**{
    "metrics": {"sharpe": 0.2, "max_drawdown": -0.40, "trading_days": 30},
    "trade_stats": {"n_sell_trades": 10, "win_rate": 0.5}, "n_rejected": 900}))
chk("多个扣分 -> partial", j["verdict"] == "partial", j["verdict"])
chk("扣到下限 1 分", j["score"] == 1, j["score"])
chk("5 条扣分都在", len(j["penalties"]) == 5, len(j["penalties"]))

print()
print("=== 6. one_line 不编数字 ===")
# ok=True 但零成交、零净值：判 reject（空跑），且不写任何指标。
j = G.judge(src(), run())
s = G.one_line("空跑", j)
chk("空跑 -> reject", "no_result" in codes(j), codes(j))
chk("没指标就不写指标", "总收益" not in s and "夏普" not in s, s)
chk("但仍给结论", "不可用" in s, s)
# 跑出了净值但一笔没卖：这是扣分不是否决（它确实在跑，只是没交易）。
j3 = G.judge(src(), run(**{**GOOD, "trade_stats": {"n_sell_trades": 0}}))
chk("有净值零卖出 -> partial（扣分）",
    j3["verdict"] == "partial" and "no_trades" in codes(j3), codes(j3))
chk("零卖出被点名", "零卖出" in G.one_line("不交易", j3), j3)
s2 = G.one_line("失败的", G.judge(src(), run(ok=False, error="RuntimeError: 炸了")))
chk("失败的写不可用", "不可用" in s2, s2)
chk("失败的仍不写指标", "总收益" not in s2, s2)
s = G.one_line("正常", G.judge(src(), run(**GOOD)))
chk("正常策略写出全部指标",
    all(k in s for k in ("总收益", "年化", "回撤", "夏普")), s)
chk("回撤按正幅度显示（不带负号）", "回撤 12.0%" in s, s)
# 标题截断到 40 字。one_line 的格式是 "标题 —— 结论"，分隔符前带空格，
# 所以 split 出来的前半段是 40 字 + 1 个空格。
chk("标题被截断到 40 字",
    len(G.one_line("标" * 80, j).split("——")[0].strip()) == 40,
    len(G.one_line("标" * 80, j).split("——")[0]))

print()
if FAILS:
    print("失败 %d: %s" % (len(FAILS), " | ".join(FAILS)))
    sys.exit(1)
print("全部通过")