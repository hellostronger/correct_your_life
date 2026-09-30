"""验证 sector.py 通过 data_service 取数（端到端接入测试）。"""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sector

URL = "http://127.0.0.1:8901"
from data_service.tests.asserts import FAIL, PASS, check, report


print("=" * 78)
print("1. 未配置时应回落东财（不改变原行为）")
print("=" * 78)
with mock.patch.object(sector, "_data_service_url", return_value=""):
    with mock.patch.object(sector, "_fetch_all_boards_eastmoney",
                           return_value=[{"code": "X", "name": "东财板块"}]) as em:
        rows = sector.fetch_all_boards()
check("无 URL -> 走东财回落", len(rows) == 1 and rows[0]["name"] == "东财板块")
check("东财回落被调用", em.called)

print()
print("=" * 78)
print("2. 服务不可达时应回落而不是抛错")
print("=" * 78)
with mock.patch.object(sector, "_data_service_url",
                       return_value="http://127.0.0.1:59999"):
    with mock.patch.object(sector, "_fetch_all_boards_eastmoney",
                           return_value=[{"code": "X", "name": "东财回落"}]) as em:
        rows = sector.fetch_all_boards()
check("服务挂掉 -> 自动回落东财", len(rows) == 1 and rows[0]["name"] == "东财回落")
check("确实回落了", em.called)

print()
print("=" * 78)
print("3. 服务正常时走服务（真打）")
print("=" * 78)
with mock.patch.object(sector, "_data_service_url", return_value=URL):
    with mock.patch.object(sector, "_fetch_all_boards_eastmoney",
                           side_effect=AssertionError("不该走东财")):
        rows = sector.fetch_all_boards()
check("拿到板块", len(rows) > 0, note=f"= {len(rows)}")
keys = set(rows[0].keys())
print(f"      字段: {sorted(keys)}")
for f in ("code", "name", "kind", "pct", "turnover", "turnover_rate",
          "main_inflow", "up_count", "down_count", "lead_stock", "score"):
    check(f"含字段 {f}", f in keys)

from collections import Counter
print(f"      类别: {dict(Counter(r['kind'] for r in rows))}")
print(f"      有 code 的: {sum(1 for r in rows if r['code'])}/{len(rows)}")
print(f"      有 pct 的: {sum(1 for r in rows if r['pct'] is not None)}/{len(rows)}")
print(f"      有 main_inflow 的: "
      f"{sum(1 for r in rows if r['main_inflow'] is not None)}/{len(rows)}")
print(f"      有 score 的: {sum(1 for r in rows if r.get('score') is not None)}/{len(rows)}")
print()
print("      Top8:")
for r in sorted(rows, key=lambda x: -(x.get("score") or 0))[:8]:
    print(f"        {r['name'][:16]:<18} {r['kind']:<9} "
          f"score={r['score']} pct={r['pct']} 净流入={r['main_inflow']}")

print()
print("=" * 78)
print("4. 服务返回 degraded=True 时仍能取数（不崩）")
print("=" * 78)
import requests as rq


class FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"boards": [{"code": "1", "name": "降级板块", "kind": "industry",
                            "pct": 1.0, "score": 10.0}],
                "degraded": True,
                "notes": ["同花顺源不可用：ConnectionError"],
                "sources_used": ["kph"]}


with mock.patch.object(sector, "_data_service_url", return_value=URL):
    with mock.patch.object(sector.requests, "get", return_value=FakeResp()):
        rows = sector.fetch_all_boards()
check("degraded 数据仍被接受", len(rows) == 1 and rows[0]["name"] == "降级板块")

print()
print("=" * 78)
print("5. 服务返回空 boards 时应回落（不写入空快照）")
print("=" * 78)


class EmptyResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"boards": [], "degraded": True, "notes": ["全挂"]}


with mock.patch.object(sector, "_data_service_url", return_value=URL):
    with mock.patch.object(sector.requests, "get", return_value=EmptyResp()):
        with mock.patch.object(sector, "_fetch_all_boards_eastmoney",
                               return_value=[{"name": "东财兜底"}]) as em:
            rows = sector.fetch_all_boards()
check("空结果 -> 回落东财", em.called and rows[0]["name"] == "东财兜底")

print()
print("=" * 78)
print("6. _attach_quotes 必须一次请求拿全部，不能逐只拉")
print("=" * 78)
items = [{"code": "600000", "name": "A"}, {"code": "000001", "name": "B"},
         {"code": "300750", "name": "C"}]
calls = []


class FakeQuotes:
    def __call__(self, codes):
        calls.append(list(codes))
        return {c: {"code": c, "price": 10.0, "change_pct": 1.5}
                for c in codes}


fake_app = type("M", (), {"fetch_quotes": FakeQuotes()})
import sys as _sys
_saved = _sys.modules.get("app")
_sys.modules["app"] = fake_app
try:
    out = sector._attach_quotes([dict(x) for x in items])
finally:
    if _saved is not None:
        _sys.modules["app"] = fake_app if _saved is fake_app else _saved
    else:
        _sys.modules.pop("app", None)

check("只调一次 fetch_quotes（关键：不能循环逐只打）", len(calls) == 1,
      note=f"实调 {len(calls)} 次")
check("一次带上全部代码", calls and len(calls[0]) == 3,
      note=f"实带 {calls[0] if calls else '无'}")
check("price/pct 被填上", all(r["price"] == 10.0 and r["pct"] == 1.5 for r in out))

# 非 6 位代码应被跳过，不进请求
calls.clear()
out2 = sector._attach_quotes([{"code": "881142", "name": "板块"}])
check("板块代码不进行情请求", calls == [], note=f"实调 {calls}")
check("无有效 code 时原样返回", out2 == [{"code": "881142", "name": "板块"}])

# 行情缺失时保持 None（不能填 0，前端会把 0 读成平盘）
sys.modules["app"] = type("M", (), {"fetch_quotes": lambda c: {
    k: {"code": k, "error": "行情未返回"} for k in c}})
try:
    out3 = sector._attach_quotes([{"code": "600000", "name": "A"}])
finally:
    if _saved is not None:
        _sys.modules["app"] = _saved
    else:
        _sys.modules.pop("app", None)
check("行情缺失保持 None 而非 0",
      "pct" not in out3[0] or out3[0].get("pct") is None,
      note=f"实得 {out3[0]}")

sys.exit(report())
