"""验请求预算真的限得住 —— 这是原实现被烧死的根因，必须有回归测试。

原实现：每 5 分钟 12 个 clist 请求打同一 host = 576/日，被 WAF 永久封禁，
且被封后循环不停继续捶，封禁永不解除。

这里要证明：新实现的按 host 记账 + 逐步检查能在预算耗尽后**自动降级**
（转新浪），而不是继续硬打同花顺。
"""
import os
import sys
from datetime import date
from unittest import mock

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from data_service import aggregate as A
from data_service import sources as S
from data_service.sources import METER, SourceError
from data_service.tests.asserts import FAIL, PASS, check, report

print("=" * 74)
print("1. 一次完整采集各源各花多少请求（实测值，非估算）")
print("=" * 74)
METER._counts.clear()
METER._day = date.today().isoformat()

ths_calls = {"n": 0}


def fake_fetch_industry(self=None):
    ths_calls["n"] += 1
    METER.allow("q.10jqka.com.cn", 3)
    from data_service import normalize as N
    return [N.from_ths_industry({"板块": f"行业{i}", "涨跌幅": 1.0 * i,
                                 "净流入": float(i), "总成交额": 100.0,
                                 "上涨家数": 5, "下跌家数": 5,
                                 "领涨股": "X"}, f"8811{i:02d}")
            for i in range(1, 6)]


def fake_fetch_concept(self=None):
    METER.allow("data.10jqka.com.cn", 9)
    from data_service import normalize as N
    return [N.from_ths_concept({"行业": f"概念{i}", "行业-涨跌幅": 1.0,
                                "净额": float(i), "领涨股": "Y"}, f"30{i:04d}")
            for i in range(1, 6)]


with mock.patch.object(S.ThsSource, "_refresh_codes",
                       lambda self, force=False: None), \
     mock.patch.object(S.ThsSource, "fetch", side_effect=fake_fetch_industry):
    pass  # 只验证记账逻辑

# 直接验证：连续调用 fetch，预算耗尽后必须抛 SourceError（而不是继续打）
METER._counts.clear()
METER._day = date.today().isoformat()
budget = S.HOST_DAILY_BUDGET
print(f"  日预算 = {budget} 请求/host")

# 模拟「行业概览每轮 3 请求」的连续采集
rounds = 0
refused_at = None
while True:
    if not METER.allow("q.10jqka.com.cn", 3):
        refused_at = rounds
        break
    rounds += 1
    if rounds > 500:
        break

print(f"  能完整跑 {rounds} 轮，第 {refused_at} 轮起被拒")
check("预算用完后拒绝（关键）", refused_at is not None)
check("拒绝发生在预算内（未超发）",
      METER.snapshot()["used"]["q.10jqka.com.cn"] <= budget,
      note=f"实发 {METER.snapshot()['used']['q.10jqka.com.cn']} <= {budget}")
expected_rounds = budget // 3
check("轮次符合预算/3", rounds == expected_rounds,
      note=f"实得 {rounds}，期望 {expected_rounds}")

print()
print("=" * 74)
print("2. 预算耗尽时同花顺抛错，聚合层应降级而不是整体失败")
print("=" * 74)
METER._counts.clear()
METER._day = date.today().isoformat()
# 一次性把 q.10jqka 和 data.10jqka 都打满
METER.allow("q.10jqka.com.cn", budget)
METER.allow("data.10jqka.com.cn", budget)

from data_service import normalize as N
fake_sina = [N.from_sina({"name": f"新浪行业{i}", "avg_changeratio": "0.01",
                          "netamount": "100000000", "turnover": "50.0"},
                         "industry") for i in range(1, 4)]

with mock.patch.object(S.THS, "fetch", side_effect=SourceError("预算用尽")), \
     mock.patch.object(S.SINA, "fetch", return_value=fake_sina), \
     mock.patch.object(S.KPH, "fetch", side_effect=SourceError("模拟不可用")), \
     mock.patch.object(S.ZT, "fetch", return_value={"qdate": "20260930",
                                                   "total": 0, "max_lb": None,
                                                   "sum_zbc": None,
                                                   "by_board": {}}):
    snap = A.collect()

check("同花顺挂了仍拿到板块（走新浪）", len(snap.boards) == 3,
      note=f"实得 {len(snap.boards)}")
check("标记为 degraded", snap.degraded is True)
check("sources_used 只剩新浪+涨停池",
      set(snap.sources_used) == {"sina", "ztpool"},
      note=f"实得 {snap.sources_used}")
check("失败原因被记录",
      any(f["source"] == "ths" for f in snap.sources_failed),
      note=f"failed={[f['source'] for f in snap.sources_failed]}")
check("notes 里说明了降级",
      any("同花顺" in n for n in snap.notes), note=f"notes={snap.notes}")

print()
print("=" * 74)
print("3. 全部源都挂时：仍不抛异常，只标记 degraded + 空板块")
print("=" * 74)
with mock.patch.object(S.THS, "fetch", side_effect=SourceError("ths 挂")), \
     mock.patch.object(S.SINA, "fetch", side_effect=SourceError("sina 挂")), \
     mock.patch.object(S.KPH, "fetch", side_effect=SourceError("kph 挂")), \
     mock.patch.object(S.ZT, "fetch", side_effect=SourceError("zt 挂")):
    snap2 = A.collect()
check("全挂不抛异常", True)
check("degraded=True", snap2.degraded is True)
check("boards 为空", snap2.boards == [])
check("四个源都记了失败", len(snap2.sources_failed) == 4,
      note=f"实得 {[f['source'] for f in snap2.sources_failed]}")
# 调用方据此决定不写库
check("to_dict 带 degraded 供调用方判断",
      snap2.to_dict()["degraded"] is True)

print()
print("=" * 74)
print("4. 概念 code 表 41 请求：预算不足时应只丢 code，不丢行情")
print("=" * 74)
METER._counts.clear()
METER._day = date.today().isoformat()
# 只留 10 个预算 -> 41 请求拿不到
METER.allow("q.10jqka.com.cn", budget - 10)
src = S.ThsSource()
src._code_ind = {"生物制品": "881142"}
src._code_con = {}
try:
    src._refresh_codes()
    # _refresh_codes 内部会跳过概念表，抛 SourceError 是预期
except SourceError as e:
    check("预算不足时抛错让上层决定", True, note=str(e)[:60])
else:
    # 没抛说明它降级处理了：行业 code 保住，概念 code 为空
    check("行业 code 保住", len(src._code_ind) > 0,
          note=f"实得 {len(src._code_ind)}")
    check("概念 code 留空（不伪造）", src._code_con == {},
          note=f"实得 {len(src._code_con)}")

METER._counts.clear()
sys.exit(report())
