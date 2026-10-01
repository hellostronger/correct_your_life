"""板块数据服务多源采集的包内自测。

不依赖 pytest，直接 `python -m data_service.selftest` 跑。
覆盖：字段规范化 / 归一化边界 / 请求预算记账 / 跨源合并 / 评分公式。
网络相关的用桩替换，保证离线可跑。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from . import aggregate as A
from . import normalize as N
from . import sources as S
from .tests.asserts import FAIL, PASS, check, report

print("=" * 74)
print("1. normalize.num —— 各种脏输入")
print("=" * 74)
check("正常 float", N.num(1.5), 1.5)
check("int", N.num(3), 3.0)
check("百分号字符串", N.num("4.63%"), 4.63)
check("带千分位", N.num("1,234.5"), 1234.5)
check("None", N.num(None), None)
check("空串", N.num(""), None)
check("横杠", N.num("-"), None)
check("nan 字符串", N.num("nan"), None)
check("float nan", N.num(float("nan")), None)
check("float inf", N.num(float("inf")), None)
check("非数字", N.num("abc"), None)
check("bool 被拒", N.num(True), None)

print()
print("=" * 74)
print("2. 同花顺行业行 → 统一结构")
print("=" * 74)
row = {"序号": 1, "板块": "生物制品", "涨跌幅": 4.63, "总成交量": 878.68,
       "总成交额": 236.62, "净流入": 14.36, "上涨家数": 53, "下跌家数": 2,
       "均价": 26.93, "领涨股": "康希诺", "领涨股-最新价": 102.64,
       "领涨股-涨跌幅": 20.00}
b = N.from_ths_industry(row, "881142")
check("name", b["name"], "生物制品")
check("code", b["code"], "881142")
check("kind", b["kind"], "industry")
check("pct", b["pct"], 4.63)
check("turnover", b["turnover"], 236.62)
check("main_inflow", b["main_inflow"], 14.36)
check("up_count", b["up_count"], 53)
check("down_count", b["down_count"], 2)
check("lead_stock", b["lead_stock"], "康希诺")
check("lead_stock_pct", b["lead_stock_pct"], 20.0)
check("turnover_rate 源不给 -> None(不填0)", b["turnover_rate"], None)
check("source", b["source"], "ths")

print()
print("=" * 74)
print("3. 开盘红行 → 统一结构（含 net_inflow 错位修正）")
print("=" * 74)
k = N.from_kph({
    "plate_id": "881142", "plate_name": "生物制品", "amount": 236.62,
    "change_pct": 4.486, "amplitude": 0.088,
    "net_inflow": 23262219085,          # 实测这个是 5 日累计
    "net_inflow_5d": 1100000373,        # 实测这个才是当日
    "buy_amount": 6953985520, "sell_amount": -5853985147,
    "turnover_rate": 1.317, "market_cap": 6e11, "stock_count": 55,
}, "industry")
check("main_inflow 用当日值(已纠错名)", k["main_inflow"], round(1100000373 / 1e8, 4))
check("main_inflow_5d 另存真 5 日值", k["main_inflow_5d"], round(23262219085 / 1e8, 4))
check("turnover_rate 源给了", k["turnover_rate"], 1.317)
check("n_stocks", k["n_stocks"], 55)
# 核心不变量：buy + sell 必须等于当日净流入（实测 405/405 成立）
check("buy+sell == main_inflow",
      round((6953985520 + (-5853985147)) / 1e8, 4), k["main_inflow"])
check("kind 映射题材", N.from_kph({"plate_name": "X"}, "theme")["kind"], "theme")

print()
print("=" * 74)
print("4. 请求预算记账（这是原实现被烧死的根因，必须防回归）")
print("=" * 74)
S.METER._counts.clear()
S.METER._day = date.today().isoformat()
budget = S.HOST_DAILY_BUDGET
check("第一次允许", S.METER.allow("test.example.com", 1), True)
check("用 1 后剩 budget-1", S.METER.allow("test.example.com", 1), True)
# 一次要超预算的请求应被拒
check("超额被拒(关键)", S.METER.allow("test.example.com", budget), False)
check("被拒后计数不增长(关键)", S.METER.snapshot()["used"]["test.example.com"], 2)
check("别的 host 独立记账", S.METER.allow("other.example.com", 1), True)
# 跨天自动清零
S.METER._day = (date.today() - timedelta(days=1)).isoformat()
check("跨天自动重置", S.METER.allow("test.example.com", 1), True)
S.METER._counts.clear()

print()
print("=" * 74)
print("4b-pre. 开盘红 amount 负值不得当成交额（实测 IT服务 给 -79.0）")
print("=" * 74)
neg = N.from_kph({"plate_id": "1", "plate_name": "IT服务", "amount": -79.0,
                  "change_pct": -1.13, "turnover_rate": 1.0,
                  "buy_amount": 1e9, "sell_amount": -1.1e9}, "industry")
check("负成交额被拒 -> None", neg["turnover"], None)
check("但原始值留档供排查", neg["amount_raw"], -79.0)
pos = N.from_kph({"plate_id": "1", "plate_name": "X", "amount": 236.0,
                  "buy_amount": 1e9, "sell_amount": -1e9}, "industry")
check("正成交额采信", pos["turnover"], 236.0)

print()
print("=" * 74)
print("4c. 新浪源：涨跌幅是比率且低精度，合并时不得覆盖主源")
print("=" * 74)
sn = N.from_sina({"name": "医疗器械", "avg_changeratio": "0.0245828",
                  "netamount": "5385744875.06", "turnover": "213.016",
                  "ts_name": "智飞生物", "ts_changeratio": "0.0688073",
                  "ts_symbol": "sz300122"}, "industry")
check("比率转百分比 2.46", sn["pct"], 2.46)
check("标记为低精度", sn["pct_low_precision"], True)
check("净流入转亿(5385744875.06元 -> 53.8574亿)", sn["main_inflow"], 53.8574)
check("领涨股", sn["lead_stock"], "智飞生物")
check("领涨股涨幅转百分比", sn["lead_stock_pct"], 6.88)

main1 = N.from_ths_industry({"板块": "医疗器械", "涨跌幅": 1.15, "净流入": 5.56,
                             "总成交额": 177.6, "上涨家数": 10,
                             "下跌家数": 10, "领涨股": "X"}, "881166")


class _Snap:
    """A._merge 只需要 notes 列表，够用即可。"""
    def __init__(self):
        self.notes = []
        self.degraded = False
        self.cross_check = None


A._merge([main1], [sn], _Snap())
check("主源 pct 未被新浪低精度值覆盖", main1["pct"], 1.15)

main2 = N.from_ths_industry({"板块": "仅有概念", "涨跌幅": 3.0, "净流入": 1.0,
                             "总成交额": None, "上涨家数": 1,
                             "下跌家数": 1, "领涨股": "Y"}, "300001")
sn2 = N.from_sina({"name": "仅有概念", "avg_changeratio": "0.019",
                   "netamount": "990000000", "turnover": "9.9"}, "concept")
A._merge([main2], [sn2], _Snap())
check("主源 turnover 为空时新浪可补", main2["turnover"], 9.9)

print()
print("=" * 74)
print("5. 跨源合并：同花顺为主，辅助源补空字段 + 保留独有板块")
print("=" * 74)
ths_rows = [
    N.from_ths_industry({"板块": "生物制品", "涨跌幅": 4.63, "总成交额": 236.62,
                         "净流入": 14.36, "上涨家数": 53, "下跌家数": 2,
                         "领涨股": "康希诺", "领涨股-涨跌幅": 20.0}, "881142"),
    N.from_ths_concept({"行业": "猴痘概念", "行业-涨跌幅": 2.96, "净额": 1.1,
                        "领涨股": "康希诺", "领涨股-涨跌幅": 20.0}, "308941"),
]
kph_rows = [
    N.from_kph({"plate_id": "881142", "plate_name": "生物制品",
                "change_pct": 4.486, "amount": 240.0, "net_inflow_5d": 1100000373,
                "buy_amount": 6.9e9, "sell_amount": -5.8e9, "turnover_rate": 1.317,
                "stock_count": 55}, "industry"),
    N.from_kph({"plate_id": "801770", "plate_name": "黑龙江省",
                "change_pct": 1.128, "amount": 62.0, "net_inflow_5d": 1.3e10,
                "buy_amount": 2.6e7, "sell_amount": -3.8e7,
                "turnover_rate": 0.961, "stock_count": 40}, "region"),
]


class _Snap:
    def __init__(self):
        self.notes = []
        self.degraded = False


snap = _Snap()
merged = A._merge(ths_rows, kph_rows, snap)
by = {m["name"]: m for m in merged}
check("合并后总数 3(生物制品+猴痘+黑龙江)", len(merged), 3)
check("地域板块被保留", "黑龙江省" in by, True)
check("概念板块被保留", "猴痘概念" in by, True)
check("同花顺已有值不被覆盖(4.63 不是 4.486)", by["生物制品"]["pct"], 4.63)
check("空字段被 kph 补上 turnover_rate", by["生物制品"]["turnover_rate"], 1.317)
check("cross_check 已生成", hasattr(snap, "cross_check"), True)
cc = getattr(snap, "cross_check", {})
check("校验指标是涨跌幅", cc.get("metric"), "pct_change")
check("校验有一对厂商", len(cc.get("pairs", [])), 1)
p0 = cc["pairs"][0]
check("共同板块数=1", p0.get("common"), 1)
check("涨跌幅差 4.63 vs 4.486 = 0.144", p0.get("max_pp"), 0.144)
check("并记录了补字段条数", any("补齐" in n for n in snap.notes), True)

print()
print("=" * 74)
print("6. 轮动评分公式（与原 sector.py 一致）")
print("=" * 74)
bs = [
    {"name": "A", "pct": 5.0, "main_inflow": 100.0},
    {"name": "B", "pct": 3.0, "main_inflow": 50.0},
    {"name": "C", "pct": 1.0, "main_inflow": 10.0},
    {"name": "D", "pct": -2.0, "main_inflow": -80.0},
    {"name": "E", "pct": -4.0, "main_inflow": -120.0},
]
scored = A.score_boards([dict(x) for x in bs], zt={"by_board": {"A": 3, "B": 1}})
sby = {x["name"]: x for x in scored}
check("按分降序", [x["name"] for x in scored][0], "A")
# A 涨幅最高 -> 涨幅分 40（4 个更低 / 5 个 = 0.8）
check("A 涨幅分 = 0.8*40 = 32", sby["A"]["score_parts"]["pct"], 32.0)
# 正流入共 3 个 [100,50,10]，A 最高 -> 2/3 分位 = 0.667*30 = 20.0
# （注意不是 30：与原 sector.py._pct_rank 同款，最高者拿不到满分 1.0）
check("A 资金分 = 2/3*30 = 20", sby["A"]["score_parts"]["flow"], 20.0)
check("A 涨停分 = min(3*4,20) = 12", sby["A"]["score_parts"]["zt"], 12)
check("B 涨停分 = 1*4 = 4", sby["B"]["score_parts"]["zt"], 4)
check("D 负流入 -> 资金分 0", sby["D"]["score_parts"]["flow"], 0.0)
check("A 总分 = 32+20+12+0 = 64", sby["A"]["score"], 64.0)
# 最低的正流入拿 0 分（0 个比它低）
check("C 资金分 = 0/3*30 = 0", sby["C"]["score_parts"]["flow"], 0.0)
check("E 垫底", scored[-1]["name"], "E")
check("pct_rank 有值", sby["A"]["pct_rank"], 80)
# 动量分
scored2 = A.score_boards([dict(x) for x in bs], zt={}, momentum={"A": 3.0})
check("动量 3.0 -> min(3*2,10)=6",
      {x["name"]: x for x in scored2}["A"]["score_parts"]["mom"], 6.0)
scored3 = A.score_boards([dict(x) for x in bs], zt={}, momentum={"A": -3.0})
check("负动量不加分",
      {x["name"]: x for x in scored3}["A"]["score_parts"]["mom"], 0.0)

print()
print("=" * 74)
print("7. _momentum 复利累计")
print("=" * 74)
hist = [{"date": f"2026-09-{d:02d}", "close": c}
        for d, c in zip((1, 2, 3, 4), (100.0, 110.0, 121.0, 133.1))]
check("3 段 10% 复利 ≈ 33.1%", A._momentum(hist), 33.1)
check("数据不足返回 None", A._momentum([{"date": "x", "close": 1.0}]), None)
check("空返回 None", A._momentum([]), None)

print()
print("=" * 74)
print("8. 涨停池聚合（akshare 重命名后的列名）")
print("=" * 74)
# 实测 akshare stock_zt_pool_em 的列：代码/名称/涨跌幅/连板数/炸板次数/所属行业...
recs = [{"代码": "600xxx", "名称": "A", "涨跌幅": 10.0, "连板数": 3,
         "炸板次数": 1, "所属行业": "化学制品"},
        {"代码": "600yyy", "名称": "B", "涨跌幅": 10.0, "连板数": 1,
         "炸板次数": 0, "所属行业": "化学制品"},
        {"代码": "300zzz", "名称": "C", "涨跌幅": 20.0, "连板数": 2,
         "炸板次数": 2, "所属行业": "电池"},
        {"代码": "600www", "名称": "D", "涨跌幅": 10.0, "连板数": 1,
         "炸板次数": 0, "所属行业": ""}]
s = S.ZtPoolSource._records_to_summary(recs, "20260930")
check("总数", s["total"], 4)
check("最高连板", s["max_lb"], 3)
check("炸板合计", s["sum_zbc"], 3)
check("按板块聚合", s["by_board"], {"化学制品": 2, "电池": 1})
check("空所属行业被跳过", "" in s["by_board"], False)
check("档 1 覆盖全市场", s["coverage"]["full_market"], True)
check("档 1 覆盖股票数=总数", s["coverage"]["stocks_seen"], 4)

print()
print("=" * 74)
print("8b. 涨停阈值：分板块 + ST（防把 20% 板误算成 10% 板）")
print("=" * 74)
L = S.ZtPoolSource._limit_pct
check("主板 10%", L("600519", "贵州茅台"), 9.8)
check("创业板 20%", L("300750", "宁德时代"), 19.5)
check("301 也是 20%", L("301047", "义翘神州"), 19.5)
check("科创板 20%", L("688331", "荣昌生物"), 19.5)
check("北交所 30%", L("830799", "艾融软件"), 29.5)
check("ST 主板 5%", L("600123", "*ST海航"), 4.8)
check("ST 创业板也是 5%（优先 ST 判断）", L("300123", "ST某某"), 4.8)
check("小写 st 也认", L("600123", "*st海航"), 4.8)

print()
print("=" * 74)
print("8c. 交易日感知：休市日不能拿今天入库")
print("=" * 74)
print("  背景（2026-10-01 国庆实测）：")
print("    push2ex 的 date 参数被**忽略**，固定返回最新交易日：")
print("      date=20261001 -> 52 条, qdate=20260930")
print("      date=20260929 -> 57 条, qdate=20260930  <- 传 0929 却给 0930 的数据")
print("    所以 qdate 才是唯一可信的真实数据日。")

from datetime import date as _date

# 日历函数本身（离线：注入假日历）
saved = S._TRADE_DAYS, S._TRADE_DAYS_DAY
S._TRADE_DAYS = {"2026-09-29", "2026-09-30", "2026-10-09"}
S._TRADE_DAYS_DAY = _date.today().isoformat()
check("交易日判 True", S.is_trading_day(_date(2026, 9, 30)), True)
check("休市日判 False（10-01 国庆）", S.is_trading_day(_date(2026, 10, 1)), False)
check("休市日前推最近交易日=09-30",
      S.last_trading_day(_date(2026, 10, 1)).isoformat(), "2026-09-30")
check("长假末端前推能跨过 09-30/10-01",
      S.last_trading_day(_date(2026, 10, 8)).isoformat(), "2026-09-30")
check("交易日当天前推是自己",
      S.last_trading_day(_date(2026, 10, 9)).isoformat(), "2026-10-09")

# 日历不可用 -> is_trading_day 必须返回 None（不是 False）
S._TRADE_DAYS, S._TRADE_DAYS_DAY = None, ""


def boom():
    raise RuntimeError("日历接口挂了")


real_td = S.trade_days
S.trade_days = boom
check("日历挂了 is_trading_day 返回 None 而非 False",
      S.is_trading_day(_date(2026, 10, 1)) is None)
check("日历挂了 last_trading_day 返回 None",
      S.last_trading_day(_date(2026, 10, 1)) is None)
S.trade_days = real_td
S._TRADE_DAYS, S._TRADE_DAYS_DAY = saved

# Snapshot 必须带 data_date / is_trading_day
sn = A.Snapshot()
check("Snapshot 有 is_trading_day 字段", hasattr(sn, "is_trading_day"))
check("Snapshot 有 data_date 字段", hasattr(sn, "data_date"))
d = sn.to_dict()
check("to_dict 输出 is_trading_day", "is_trading_day" in d)
check("to_dict 输出 data_date", "data_date" in d)

# 休市日时 data_date 必须 != trade_date（这是入库依据）
import unittest.mock as _mk
with _mk.patch.object(S, "is_trading_day", return_value=False), \
     _mk.patch.object(S, "last_trading_day",
                      return_value=_date(2026, 9, 30)), \
     _mk.patch.object(S.THS, "fetch", return_value=[]), \
     _mk.patch.object(S.KPH, "fetch", return_value=[]), \
     _mk.patch.object(S.SINA, "fetch", return_value=[]), \
     _mk.patch.object(S.ZT, "fetch", return_value={}):
    snap_hol = A.collect()
check("休市日 is_trading_day=False", snap_hol.is_trading_day, False)
check("休市日 data_date=最近交易日 09-30",
      snap_hol.data_date, "2026-09-30",
      note=f"实得 {snap_hol.data_date}")
check("休市日 notes 说明了",
      any("非交易日" in n for n in snap_hol.notes),
      note=f"notes={snap_hol.notes[:2]}")

# 交易日时 data_date == trade_date
with _mk.patch.object(S, "is_trading_day", return_value=True), \
     _mk.patch.object(S.THS, "fetch", return_value=[]), \
     _mk.patch.object(S.KPH, "fetch", return_value=[]), \
     _mk.patch.object(S.SINA, "fetch", return_value=[]), \
     _mk.patch.object(S.ZT, "fetch", return_value={}):
    snap_tr = A.collect()
check("交易日 data_date==trade_date",
      snap_tr.data_date, snap_tr.trade_date)

# 日历不可用时也要照常工作（不能崩）
with _mk.patch.object(S, "is_trading_day", return_value=None), \
     _mk.patch.object(S.THS, "fetch", return_value=[]), \
     _mk.patch.object(S.KPH, "fetch", return_value=[]), \
     _mk.patch.object(S.SINA, "fetch", return_value=[]), \
     _mk.patch.object(S.ZT, "fetch", return_value={}):
    snap_nk = A.collect()
check("日历不可用时不崩", snap_nk.is_trading_day is None)
check("日历不可用时 data_date 退回当天",
      snap_nk.data_date, snap_nk.trade_date)

print()
print("=" * 74)
print("9. _dedupe：重名板块去重（实测 969 个里有重名）")
print("=" * 74)
main9 = N.from_ths_industry({"板块": "电子", "涨跌幅": 3.07, "净流入": 54.06,
                             "总成交额": 800.0, "上涨家数": 30, "下跌家数": 20,
                             "领涨股": "A"}, "881157")
aux9 = [
    # 辅助源新浪的同名「电子」，无 code、缺成交额
    N.from_sina({"name": "电子", "avg_changeratio": "0.03",
                 "netamount": "5406000000", "turnover": "810.0",
                 "ts_name": "A"}, "industry"),
    # 同名但 kind 不同（地域），验证 kinds 被记录
    N.from_kph({"plate_id": "801770", "plate_name": "黑龙江", "change_pct": 1.1,
                "net_inflow_5d": 1.3e10, "buy_amount": 1e9, "sell_amount": -1e9,
                "amount": 62.0, "turnover_rate": 0.96, "stock_count": 40},
               "region"),
]
s9 = _Snap()
merged9 = A._merge([main9], aux9, s9)
# aux9 有 2 行：1 个与主源重名(电子) + 1 个独有(黑龙江)
# 去重后应为 2 行，不是 1 行 —— 我最初写 1 是把「重名」和「总量」搞混了
check("重名合并后剩 2 行(电子+黑龙江)", len(merged9) == 2,
      note=f"实得 {[r['name'] for r in merged9]}")
m9 = next(r for r in merged9 if r["name"] == "电子")
check("主源 code 保住", m9["code"] == "881157", note=f"实得 {m9['code']}")
check("主源 pct 未被新浪低精度覆盖", m9["pct"] == 3.07, note=f"实得 {m9['pct']}")
check("主源涨/跌家数保住", m9["up_count"] == 30 and m9["down_count"] == 20,
      note=f"实得 {m9['up_count']}/{m9['down_count']}")
check("新浪独有的地域板块被保留",
      any(r["name"] == "黑龙江" for r in merged9),
      note=f"names={[r['name'] for r in merged9]}")
# 注意：aux 里的同名行在 _merge 的「补空字段」循环里就被合并了（写进 idx），
# 所以不会走到 _dedupe，notes 里自然没有「重名」。
# _dedupe 是给「主源内部就有重名」兜底的 —— 那属于上游异常，测它要有意构造。
s9d = _Snap()
dup_in = [dict(main9), dict(main9)]   # 主源内部重复（模拟上游返回异常）
dedup_out = A._dedupe(dup_in, s9d)
check("_dedupe 兜住主源内部重名", len(dedup_out) == 1, note=f"实得 {len(dedup_out)}")
check("_dedupe 记了重名条数", any("重名" in n for n in s9d.notes),
      note=f"notes={s9d.notes}")
check("_merge 路径下不产生重名 note（aux 已就地合并）",
      not any("重名" in n for n in s9.notes), note=f"notes={s9.notes}")
print(f"      （本段实测 notes = {s9.notes}）")
print(f"      （本段实测 merged = {[(r['name'], r['source']) for r in merged9]}）")

# kind 不同的同名
s9b = _Snap()
same = [
    N.from_ths_industry({"板块": "银行", "涨跌幅": 1.46, "净流入": 56.26,
                         "总成交额": 265.1, "上涨家数": 42, "下跌家数": 0,
                         "领涨股": "B"}, "881155"),
    N.from_kph({"plate_id": "881155", "plate_name": "银行", "change_pct": 1.46,
                "net_inflow_5d": 5.6e9, "buy_amount": 1e10, "sell_amount": -4e9,
                "amount": 265.1, "turnover_rate": 1.5, "stock_count": 42},
               "industry"),
]
m9b = A._merge(same, [], s9b)
check("同名同 kind 只留一行", len(m9b) == 1, note=f"实得 {len(m9b)}")
check("换手率被补上", m9b[0]["turnover_rate"] == 1.5,
      note=f"实得 {m9b[0]['turnover_rate']}")

# 无重名时不应动数据（用固定名称，别用 f-string 里的循环变量）
s9c = _Snap()
uniq = [N.from_ths_industry({"板块": "唯一行业A", "涨跌幅": 1.0, "净流入": 1.0,
                             "总成交额": 1.0, "上涨家数": 1, "下跌家数": 1,
                             "领涨股": "X"}, "881100")]
check("无重名时行数不变", len(A._merge(uniq, [], s9c)) == 1,
      note=f"实得 {len(A._merge(uniq, [], s9c))}")
check("无重名时不产生去重 note",
      not any("重名" in n for n in s9c.notes), note=f"notes={s9c.notes}")

sys.exit(report())
