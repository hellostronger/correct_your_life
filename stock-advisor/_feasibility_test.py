# -*- coding: utf-8 -*-
"""strategy_feasibility 离线断言（不联网不连库）。
跑法：python stock-advisor/_feasibility_test.py"""
import sys, os, json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_feasibility as F

N = 0
def ok(cond, label):
    global N
    N += 1
    if not cond:
        print(f"  FAIL #{N}: {label}")

FULL = {c: True for c in F.CAPABILITIES}

# 三条真实策略的摘要原文（来自 sa_strategy_digest，2026-10-06）
T_BOLL = ("日线级别数据。用BBI(3,6,12,24)判断多空大方向，仅在BBI上方做多；"
          "用BOLL(20,2)带宽收窄识别蓄势、上轨突破做入场、中下轨回踩做低吸；"
          "成交量验证突破真伪。需剔除ST、退市、上市不足120天股票")
T_ETF = ("五福系列通过ETF动量轮动选股，13:10计算排名、随机时段执行规避狙击，"
         "融合RSRS择时、动态动量周期、TWAP分批及多层回撤控制，单ETF持仓。"
         "防御ETF固定为511880.XSHG")
T_HS300 = ("在沪深300成分股中用7个因子（估值、盈利、成长、动量、反转、低波）"
           "横截面打分，按牛熊状态动态调整权重，每4日调仓持仓20只")

print("=== 1) baseline 能力（不靠关键词）===")
caps, ev = F.required_caps("")
ok("daily_ohlcv" in caps, "空文本仍有 daily_ohlcv")
ok("limit_price" in caps, "空文本仍有 limit_price（涨跌停对 A 股普遍成立）")
ok("baseline" in ev["daily_ohlcv"], f"baseline 证据明确：{ev['daily_ohlcv']}")
ok(not F.required_caps("")[1].get("daily_ohlcv", "").count("命中"),
   "baseline 不是靠关键词命中的")

print("\n=== 2) 纯日线技术策略不误加能力 ===")
c, e = F.required_caps(T_BOLL)
ok(set(c) == {"daily_ohlcv", "limit_price", "st_flag"}, f"实际={c}")
ok("intraday_bar" not in c, "「日线级别数据」不能触发 intraday_bar（实测踩过）")

print("\n=== 3) ETF 策略需要 etf_pool + intraday_bar ===")
c2, e2 = F.required_caps(T_ETF)
ok("etf_pool" in c2, f"etf_pool 在 {c2}")
ok("intraday_bar" in c2, "13:10 + TWAP → intraday_bar")
ok("cross_section" in c2, "轮动 → cross_section")
ok("511880" in e2["etf_pool"], f"证据含原词 511880：{e2['etf_pool']}")

print("\n=== 4) 多因子策略需要 valuation + fundamental_roe + index_constituent ===")
c3, e3 = F.required_caps(T_HS300)
for cap in ("valuation", "fundamental_roe", "index_constituent", "cross_section"):
    ok(cap in c3, f"{cap} 在 {c3}")
ok("沪深300" in e3["index_constituent"], f"证据含原词：{e3['index_constituent']}")

print("\n=== 5) judge：数据全齐 → 可回测 ===")
fe = F.judge("纯技术", "趋势跟踪", T_BOLL, FULL)
ok(fe.ok, "全齐 → ok")
ok(not fe.degraded_missing, "全齐无降级项")
ok("\u2705" in F.one_line(fe), "渲染 U+2705")

print("\n=== 6) judge：只缺 ST → 降级但仍 ok ===")
av = dict(FULL); av["st_flag"] = False
fe2 = F.judge("纯技术", "趋势跟踪", T_BOLL, av)
ok(fe2.ok, "ST 是 degraded 不是 fatal → 仍 ok")
ok(len(fe2.degraded_missing) == 1, f"degraded 1 项：{fe2.degraded_missing}")
ok("\u26a0" in F.one_line(fe2), "渲染带降级标记")

print("\n=== 7) judge：ETF 策略在缺 ETF 池时 → 硬否 ===")
av3 = dict(FULL); av3["etf_pool"] = False
fe3 = F.judge("五福ETF", "板块轮动", T_ETF, av3)
ok(not fe3.ok, "缺 ETF 池 → 不可回测")
ok(any("ETF" in x for x in fe3.fatal_missing), f"fatal 含 ETF：{fe3.fatal_missing}")
ok("\u274c" in F.one_line(fe3), "渲染 U+274C")

print("\n=== 8) judge：avail 缺 key → 按 False（信息不足不放行）===")
fe4 = F.judge("未知", "", T_BOLL, {})
ok(not fe4.ok, "avail 空 → 判不可行")
ok(fe4.fatal_missing, "有 fatal 项")
fe5 = F.judge("未知", "", T_BOLL, {"daily_ohlcv": True})
ok(not fe5.ok, "只探测到 1 项 → 仍不可行（limit_price baseline 也算缺）")

print("\n=== 9) 证据不串台 ===")
c9, e9 = F.required_caps("13:10 排名；ETF 轮动；剔除 ST")
ok("13:10" not in e9["st_flag"], f"st_flag 证据不串台：{e9['st_flag']}")
ok("13:10" in e9["intraday_bar"], f"intraday 证据含 13:10：{e9['intraday_bar']}")
ok("ETF" in e9["etf_pool"], f"etf_pool 证据含 ETF：{e9['etf_pool']}")
# 多条规则命中同一能力 → 证据拼接而非覆盖
_, e9b = F.required_caps("ETF 轮动，防御ETF固定为511880.XSHG")
ok(" / " in e9b["etf_pool"], f"多条命中拼接：{e9b['etf_pool']}")

print("\n=== 10) detail_lines 结构 ===")
fe6 = F.judge("沪深300多因子", "多因子", T_HS300,
              {**FULL, "fundamental_roe": False, "index_constituent": False})
lines = F.detail_lines(fe6)
n_missing = sum(1 for f in fe6.findings if not f.available)
ok(len(lines) == 1 + len(fe6.findings) + n_missing,
   f"行数 = 1 + 需求 + 缺失 = {len(lines)}")
marks = [l for l in lines[1:] if l.lstrip().startswith(("\u2713", "\u2717"))]
ok(len(marks) == len(fe6.findings), f"每项一行标记：{len(marks)}/{len(fe6.findings)}")
ok(any("缺了会怎样" in l for l in lines), "缺失项有「缺了会怎样」说明")
ok(any("可得性依据" in l for l in lines), "缺失项有「可得性依据」")

print("\n=== 11) report 结构 ===")
rep = F.report([fe, fe3], FULL, {c: "stub" for c in F.CAPABILITIES})
ok("本项目数据能力现状" in rep, "报告有现状段")
ok("逐条判定" in rep, "报告有逐条段")
ok("可回测 1/2" in rep, f"报告有汇总：可回测 1/2")

print("\n=== 12) 能力清单自身一致性 ===")
ok(set(F.CAPABILITIES) == set(F._BASELINE) | set(F._RULES and [
    c for _p, need, _w in F._RULES for c in need]),
   "能力清单与规则/baseline 覆盖一致")
for cap, (name, basis, sev, why) in F.CAPABILITIES.items():
    ok(all([name, basis, why]), f"{cap} 四个字段都非空")
    ok(sev in (F.FATAL, F.DEGRADED, F.COSMETIC), f"{cap} 分级合法：{sev}")

print(f"\n=== 共 {N} 条 ===")