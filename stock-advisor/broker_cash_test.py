"""broker_cash 离线测试：不联网、不连库、不 import app、不写生产侧车。

跑法：D:\\Python312\\python.exe broker_cash_test.py
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import broker_cash as BC

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


def close(a, b, tol=0.02, label=""):
    ck(abs(float(a) - float(b)) <= tol, f"{label} 期望 {b}，实际 {a}")


def tmp(name="bc_test.json"):
    p = Path(tempfile.gettempdir()) / name
    p.unlink(missing_ok=True)
    return p


# ---------------- CRUD 与校验 ----------------
p = tmp()
ck(BC.store_status(p)["count"] == 0 and not BC.store_status(p)["exists"], "空台账")
f1 = BC.add_flow("2026-11-10", "in", 50000, "CNY", "招行转银河", "A股", path=p)
f2 = BC.add_flow("2026-11-13", "out", 78700, "CNY", "转回银行", "A股", path=p)
f3 = BC.add_flow("2026-11-12", "in", 10000, "HKD", "港股入金", "港股", path=p)
f4 = BC.add_flow("2026-11-12", "in", 10000, "HKD", "同额第二笔（不该被去重）", "港股", path=p)
ck(len(BC.list_flows(path=p)) == 4, "同额同日两笔是两笔真转账，不自动去重")
ck(f1["side"] == "in" and f2["side"] == "out" and f1["currency"] == "CNY", "字段归一")
ck(all(f["id"] for f in (f1, f2, f3, f4)) and len({f["id"] for f in (f1, f2, f3, f4)}) == 4, "id 唯一")
ck(BC.delete_flow(f4["id"], path=p) and not BC.delete_flow(f4["id"], path=p), "删除幂等")

for bad in (("2026/11/10", "in", 1, "CNY"), ("2026-11-10", "hold", 1, "CNY"),
            ("2026-11-10", "in", 0, "CNY"), ("2026-11-10", "in", -5, "CNY"),
            ("2026-11-10", "in", 1, "USD")):
    try:
        BC.add_flow(*bad, path=p)
        raise AssertionError("应当拒绝: " + repr(bad))
    except ValueError:
        pass
N += 1
print("  (5 条非法入参已拒绝)")

rows = BC.list_flows(path=p)
ck([f["date"] for f in rows] == ["2026-11-13", "2026-11-12", "2026-11-10"], "按日期倒序")
ck(len(BC.list_flows(path=p, start="2026-11-12")) == 2, "列表筛选 start 为闭区间 >=")
ck(len(BC.list_flows(path=p, end="2026-11-12")) == 2, "列表筛选 end 为闭区间 <=")

# ---------------- 汇总与区间净额 ----------------
s = BC.summarize(rows, fx=0.85)
close(s["total_in_cny"], 50000 + 10000 * 0.85, label="累计入金折 CNY")
close(s["total_out_cny"], 78700, label="累计出金折 CNY")
close(s["net_cny"], 50000 + 8500 - 78700, label="净入金折 CNY")
close(s["by_currency"]["HKD"]["in"], 10000, label="HKD 入金原币")
close(s["by_currency"]["CNY"]["out"], 78700, label="CNY 出金原币")
ck([m["month"] for m in s["by_month"]] == ["2026-11"], "按月分组")

rng = BC.net_in_range(rows, start="2026-11-10", fx=0.85)
ck(rng["count"] == 2, "区间严格大于基准日（11-10 那笔不算）")
close(rng["net_cny"], -78700 + 8500, label="区间净额折 CNY")
ck(BC.net_in_range(rows, start="2026-11-10", end="2026-11-12", fx=0.85)["count"] == 1,
   "区间 end 闭区间")

# ---------------- 侧车容错 ----------------
p2 = tmp("bc_broken.json")
p2.write_text("{broken", encoding="utf-8")
ck(BC.store_status(p2)["error"] and BC.list_flows(path=p2) == [], "损坏文件不静默当成功")
p2.unlink(missing_ok=True)
p.unlink(missing_ok=True)

# ---------------- 静态接线 ----------------
app = (BASE / "app.py").read_text(encoding="utf-8")
html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
ck("import broker_cash" in app, "app 导入模块")
for ep in ('@app.get("/api/cash-flows")', '@app.post("/api/cash-flows")',
           '@app.delete("/api/cash-flows")', '@app.get("/api/cash-flows/summary")'):
    ck(ep in app, f"端点存在 {ep}")
seg = app[app.index("class CashFlowIn"):app.index("# ---------------- 加仓摊薄成本预估")]
ck("INSERT INTO" not in seg and "CREATE TABLE" not in seg, "出入金段不碰数据库表/流水")
ck("broker_cash.load_store().get(\"flows\", [])" in app, "快照报告真的注入了出入金流水")
ck("cash_flows=flows" in app, "build_report 收到 cash_flows")

ck('data-tab="cashflow"' in html and 'id="tab-cashflow"' in html, "tab 按钮与 section")
ck(re.search(r"const TABS = \[[^\]]*'cashflow'", html) is not None, "TABS 注册")
ck("cashflow:  loadCashFlows" in html, "TAB_LOADERS 注册")
for ident in ("cf-meta", "cf-summary", "cf-list"):
    ck(f'id="{ident}"' in html, f"DOM id {ident}")
for fn in ("loadCashFlows", "renderCashFlowSummary", "renderCashFlowList",
           "cashFlowAdd", "cashFlowDelete", "_cfAmt"):
    ck(re.search(r"(async\s+)?function\s+" + fn + r"\s*\(", html) is not None, f"函数定义 {fn}")
js = html[html.index("const _SNAP ="):html.index("// ---- 标签页 ----")]
ck("/api/cash-flows" in js and "formDlg(" in js and "confirmDlg(" in js,
   "前端真的调用端点且走统一弹窗")
ck("期间净出入金" in js and "调整后账户资产" in js, "报告对账行含出入金调整")
ck("_SNAP.reportDate" in js, "记账后刷新已加载的快照报告")

print(f"broker_cash_test: {N}/{N} 全过")
