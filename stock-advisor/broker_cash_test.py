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
# 2026-11-20 时间戳升级：date 统一存 `YYYY-MM-DD HH:MM:SS`，纯日期落 00:00:00
ck([f["date"] for f in rows] == ["2026-11-13 00:00:00", "2026-11-12 00:00:00", "2026-11-10 00:00:00"],
   "按时间倒序（纯日期入参归一成 00:00:00 全串）")
ck(len(BC.list_flows(path=p, start="2026-11-12")) == 2, "列表筛选 start 为闭区间 >=")
ck(len(BC.list_flows(path=p, end="2026-11-12")) == 2, "列表筛选 end 为闭区间 <=")

# ---------------- 时刻支持（2026-11-20 时间戳升级） ----------------
p4 = tmp("bc_time.json")
BC.add_flow("2026-01-12 14:22:55", "in", 53161.87, "CNY", "下午那笔", path=p4)
BC.add_flow("2026-01-12 09:00", "in", 91582.51, "CNY", "上午那笔", path=p4)  # HH:MM 补 :00
r4 = BC.list_flows(path=p4)
ck([f["date"] for f in r4] == ["2026-01-12 14:22:55", "2026-01-12 09:00:00"],
   "同日两笔按时刻排序（旧口径 _date10+id 会按 uuid 打乱）")
for bad in ("2026-01-12 24:00:00", "2026-01-12 10:60:00", "2026-01-12 10:00:60"):
    try:
        BC.add_flow(bad, "in", 1, "CNY", path=p4)
        raise AssertionError("应当拒绝: " + bad)
    except ValueError:
        pass
N += 1
print("  (3 条非法时刻已拒绝)")
mig = BC.migrate_note_times(path=p4)
ck(mig["changed"] == 0 and mig["already_ts"] == 2, "迁移幂等：已是全串的行跳过")
p4.unlink(missing_ok=True)

# 备注尾部时刻回填（存量数据迁移：旧惯例时刻只存在于备注）
p5 = tmp("bc_migrate.json")
import json as _json
p5.write_text(_json.dumps({"version": 1, "flows": [
    {"id": "m1", "date": "2026-09-07", "side": "out", "amount": 99609.0, "currency": "CNY",
     "account": "", "note": "截图导入 近1年人民币 证券转银行 08:58:35", "created_at": "x"},
    {"id": "m2", "date": "2026-09-08", "side": "in", "amount": 500.0, "currency": "CNY",
     "account": "", "note": "无时刻备注", "created_at": "x"},
]}, ensure_ascii=False), encoding="utf-8")
mig = BC.migrate_note_times(path=p5)
ck(mig["changed"] == 1 and mig["no_time_in_note"] == 1, "迁移：备注尾部时刻回填 1 条")
rows5 = {f["id"]: f for f in BC.list_flows(path=p5)}
ck(rows5["m1"]["date"] == "2026-09-07 08:58:35" and rows5["m1"]["note"].endswith("08:58:35"),
   "date 升级全串且备注原文保留")
ck(rows5["m2"]["date"] == "2026-09-08", "解析不出时刻的行保持纯日期（不编造）")
ck(BC.migrate_note_times(path=p5)["changed"] == 0, "迁移幂等：第二次跑零改动")
p5.unlink(missing_ok=True)

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

# ---------------- 修改（update_flow，2026-11-19 用户要求「转账流水支持修改」） ----------------
# 用独立台账，避免改动上面汇总断言依赖的金额。
p3 = tmp("bc_update.json")
u1 = BC.add_flow("2026-11-10", "in", 50000, "CNY", "招行转银河", "A股", path=p3)
u2 = BC.add_flow("2026-11-12", "out", 8000, "HKD", "转回银行", "港股", path=p3)

u1n = BC.update_flow(u1["id"], {"date": "2026-11-09", "side": "out", "amount": 12345.67,
                                "currency": "HKD", "account": "港股", "note": "改过了"}, path=p3)
ck(u1n["id"] == u1["id"] and u1n["created_at"] == u1["created_at"], "修改保留 id / created_at")
ck(u1n.get("updated_at"), "写 updated_at")
ck(u1n["date"] == "2026-11-09 00:00:00" and u1n["side"] == "out"
   and u1n["amount"] == 12345.67 and u1n["currency"] == "HKD"
   and u1n["account"] == "港股" and u1n["note"] == "改过了", "全字段生效、金额归一到分")
stored = [f for f in BC.list_flows(path=p3) if f["id"] == u1["id"]][0]
ck(stored["side"] == "out" and stored["amount"] == 12345.67, "真的落盘")
ck(len(BC.list_flows(path=p3)) == 2, "修改是原地改，不新增记录")

# 只传一个字段：备注/账户/日期/币种一个都不能被清空（这是「删了重记」最容易踩的坑）
u1p = BC.update_flow(u1["id"], {"amount": 20000}, path=p3)
ck(u1p["amount"] == 20000 and u1p["date"] == "2026-11-09 00:00:00" and u1p["side"] == "out"
   and u1p["currency"] == "HKD" and u1p["account"] == "港股"
   and u1p["note"] == "改过了", "部分修改只动传入字段")
ck(BC.update_flow(u1["id"], {}, path=p3)["amount"] == 20000, "空 changes 幂等")
ck(BC.update_flow(u1["id"], {"note": ""}, path=p3)["note"] == "", "传空串可清空备注")

# 对账口径随修改自动重算
s3 = BC.summarize(BC.list_flows(path=p3), fx=0.9)
close(s3["total_out_cny"], 20000 * 0.9 + 8000 * 0.9, label="汇总随修改重算")
ck(BC.net_in_range(BC.list_flows(path=p3), start="2026-11-10", fx=0.9)["count"] == 1,
   "日期改到基准日之前 → 不再计入期间净出入金")

before = BC.list_flows(path=p3)
for bad in ({"date": "2026/11/10"}, {"side": "hold"}, {"amount": 0},
            {"amount": -1}, {"currency": "USD"}, {"id": "不许改"}):
    try:
        BC.update_flow(u1["id"], bad, path=p3)
        raise AssertionError("应当拒绝: " + repr(bad))
    except ValueError:
        pass
N += 1
print("  (6 条非法修改已拒绝)")
ck(BC.list_flows(path=p3) == before, "非法修改一个字节都不写（先校验后落盘）")
ck(BC.update_flow("no_such_id", {"amount": 1}, path=p3) is None, "不存在的 id 返回 None")
ck(BC.list_flows(path=p3) == before, "找不到记录也不写盘")
ck(u2["id"] != u1["id"], "另一笔不受影响")
p3.unlink(missing_ok=True)

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
           '@app.put("/api/cash-flows")', '@app.delete("/api/cash-flows")',
           '@app.get("/api/cash-flows/summary")'):
    ck(ep in app, f"端点存在 {ep}")
ck("class CashFlowUpdate" in app and "broker_cash.update_flow" in app,
   "PUT 端点真的调用台账 update_flow（不是删了重记）")
ck("body.model_dump(exclude_unset=True" in app, "只把真正传了的字段交给台账")
ck("def update_flow" in (BASE / "broker_cash.py").read_text(encoding="utf-8"),
   "台账侧有 update_flow")
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
           "cashFlowAdd", "cashFlowEdit", "cashFlowDelete", "_cfAmt"):
    ck(re.search(r"(async\s+)?function\s+" + fn + r"\s*\(", html) is not None, f"函数定义 {fn}")
js = html[html.index("const _SNAP ="):html.index("// ---- 标签页 ----")]
ck("/api/cash-flows" in js and "formDlg(" in js and "confirmDlg(" in js,
   "前端真的调用端点且走统一弹窗")
ck("jsonPut('/api/cash-flows'" in js and "cashFlowEdit('" in js,
   "流水行有修改按钮、编辑走 PUT")
ck("_CF_ROWS" in js and "保存修改" in js, "编辑弹窗按 id 预填当前行")
ck("期间净出入金" in js and "调整后账户资产" in js, "报告对账行含出入金调整")
ck("_SNAP.reportDate" in js, "记账后刷新已加载的快照报告")

# ---------------- 2026-11-20 时间戳升级静态接线 ----------------
ck("fmtTs(f.date)" in html, "转账列表时间戳显示走 fmtTs")
ck("{ key: 'time', label: '时刻', type: 'time', step: '1'," in html, "录入表单带时刻输入（秒精度）")
ck("broker_cash.norm_ts" in app, "交易时间归一复用台账同一套规则（不写两份）")
ck("trade_date  TIMESTAMP NOT NULL," in app, "sa_trades DDL 升级为 timestamp")
ck("trade_date    TIMESTAMP NOT NULL," in app, "sa_paper_trades DDL 升级为 timestamp")
ck("_migrate_ts_columns" in app and "_TS_MIGRATIONS" in app, "启动幂等迁移存在")
ck("AT TIME ZONE 'Asia/Shanghai'" in app, "created_at 时刻按上海墙钟回填（不偏 8 小时）")

# ---------------- 2026-11-20 按月统计（用户要求：看哪个月入金最多） ----------------
ck('id="cf-monthly"' in html, "按月统计 DOM 容器")
ck(re.search(r"function\s+renderCashFlowMonthly\s*\(", html) is not None,
   "按月统计渲染函数定义")
ck("renderCashFlowMonthly((d.summary || {}).by_month || [])" in html,
   "loadCashFlows 真的调用了按月渲染（数据来自后端 by_month）")
ck("入金最多" in html and "topKeys.has(r.month)" in html,
   "入金最多月份高亮 + 徽标")
ck("by_month" in (BASE / "broker_cash.py").read_text(encoding="utf-8"),
   "后端 summarize 持续输出 by_month")

print(f"broker_cash_test: {N}/{N} 全过")
