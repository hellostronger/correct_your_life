"""账户锚点离线测试：不联网、不连库、不 import app、不写生产侧车。

跑法：D:\\Python312\\python.exe account_anchors_test.py
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import account_anchors as AA

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


def close(a, b, tol=0.02, label=""):
    ck(abs(float(a) - float(b)) <= tol, f"{label} 期望 {b}，实际 {a}")


def tmp(name="aa_test.json"):
    p = Path(tempfile.gettempdir()) / name
    p.unlink(missing_ok=True)
    return p


# ---------------- 存储 ----------------
p = tmp()
d = AA.load_store(p)
ck(d["cash_cny"] is None and d["reference_pnl_cny"] is None, "初始为未录入，不是 0")
ck(not AA.status(p)["set"], "status 标记未设置")

s = AA.save(52800, -116000, "券商 11-13 收盘", asof_date="2026-11-13",
            system_pnl_snapshot_cny=-621859.83, path=p)
ck(s["cash_cny"] == 52800 and s["reference_pnl_cny"] == -116000, "保存两个锚点")
ck(s["asof_date"] == "2026-11-13", "基准日入库")
ck(s["system_pnl_snapshot_cny"] == -621859.83, "系统盈亏快照入库")
ck(AA.load_store(p)["asof_date"] == "2026-11-13"
   and AA.load_store(p)["system_pnl_snapshot_cny"] == -621859.83, "重读一致")
ck(s["updated_at"] and s["note"] == "券商 11-13 收盘", "时间戳与备注")
ck(AA.load_store(p)["cash_cny"] == 52800, "重新读取一致")
ck(AA.status(p)["set"] and AA.status(p)["exists"], "status 标记已设置")

for bad_asof in ("2026/11/13", "13-11-2026", "abc"):
    try:
        AA.save(1, 1, asof_date=bad_asof, path=p)
        raise AssertionError("应当拒绝非法基准日: " + bad_asof)
    except ValueError:
        pass
N += 1
print("  (3 组非法基准日已拒绝)")

s2 = AA.save(None, None, path=p)
ck(s2["cash_cny"] is None and s2["reference_pnl_cny"] is None, "可以清空回未录入")
ck(s2["system_pnl_snapshot_cny"] is None, "清空时快照一并清掉")

for bad in ((-1, 0), (100, "abc"), ("abc", 0)):
    try:
        AA.save(bad[0], bad[1], path=p)
        raise AssertionError("应当拒绝: " + repr(bad))
    except ValueError:
        pass
N += 1
print("  (3 组非法入参已拒绝)")

p2 = tmp("aa_broken.json")
p2.write_text("{broken", encoding="utf-8")
ck(AA.load_store(p2).get("error") and AA.load_store(p2)["cash_cny"] is None,
   "损坏文件不静默当成功")
p2.unlink(missing_ok=True)

# ---------------- compute 数学 ----------------
MV, SYSP = 627595.0, -430000.0
c = AA.compute({"cash_cny": 52800.0, "reference_pnl_cny": -116000.0}, MV, SYSP)
close(c["real_assets_cny"], MV + 52800, label="真实总资产 = 持仓市值 + 真实现金")
close(c["pnl_gap_cny"], -116000 - SYSP, label="未覆盖缺口 = 总参考收益 − 系统累计盈亏")
close(c["implied_principal_cny"], MV + 52800 - (-116000), label="反推本金 = 真实总资产 − 总参考收益")
close(c["holdings_mv_cny"], MV, label="持仓市值透传")
close(c["system_total_pnl_cny"], SYSP, label="系统累计盈亏透传")
ck(c["ready"], "两个锚点齐备 = ready")

c0 = AA.compute({"cash_cny": None, "reference_pnl_cny": None}, MV, SYSP)
ck(c0["real_assets_cny"] is None and c0["pnl_gap_cny"] is None
   and c0["implied_principal_cny"] is None and not c0["ready"],
   "未录入不拿 0 冒充真实数据")
c1 = AA.compute({"cash_cny": 0.0, "reference_pnl_cny": 0.0}, MV, SYSP)
close(c1["real_assets_cny"], MV, label="现金真的是 0 时总资产 = 持仓市值")
ck(c1["ready"], "显式填 0 也算已录入")
c2 = AA.compute({"cash_cny": 100.0, "reference_pnl_cny": 5000.0}, MV, SYSP)
close(c2["pnl_gap_cny"], 5000 - SYSP, label="正收益缺口")

# ---------------- 与实盘买卖联动（2026-11-14） ----------------
# 场景：11-13 录入锚点（现金 52800，参考收益 -116000，当时系统盈亏 -621859.83）；
# 之后记了一笔买入（净交易现金流 -30000）、一笔出金（净出入金 -8000），
# 系统盈亏现在涨到 -590000（涨了 31859.83）。
LK = {"cash_cny": 52800.0, "reference_pnl_cny": -116000.0,
      "asof_date": "2026-11-13", "system_pnl_snapshot_cny": -621859.83}
SYS_NOW = -590000.0
cl = AA.compute(LK, MV, SYS_NOW, net_trade_flow_cny=-30000.0, ext_flow_cny=-8000.0)
close(cl["cash_now_cny"], 52800 - 30000 - 8000, label="联动现金 = 锚点 + 交易净流 + 出入金")
close(cl["real_assets_cny"], MV + 52800 - 30000 - 8000,
      label="真实总资产用联动现金")
close(cl["real_pnl_now_cny"], -116000 + (SYS_NOW - (-621859.83)),
      label="实时参考收益 = 锚点 + (系统盈亏现在 − 快照)")
close(cl["pnl_gap_cny"], -116000 - (-621859.83),
      label="缺口钉死在录入时点，不随系统盈亏漂移")
close(cl["real_assets_cny"] - cl["implied_principal_cny"], cl["real_pnl_now_cny"],
      label="恒等式：真实总资产 − 反推本金 = 实时参考收益")
ck(cl["ready"], "联动后仍 ready")

# 只有一笔买入、没有出入金：现金流就是那笔买卖
cb = AA.compute(LK, MV, SYS_NOW, net_trade_flow_cny=-30000.0)
close(cb["cash_now_cny"], 22800, label="只有交易流的联动现金")
close(cb["ext_flow_cny"], 0, label="出入金缺省 0")

# 旧侧车兼容：没有快照 → 缺口退化为「参考 − 当前系统盈亏」，实时收益退化为手填值
c_old = AA.compute({"cash_cny": 52800.0, "reference_pnl_cny": -116000.0}, MV, SYS_NOW)
close(c_old["pnl_gap_cny"], -116000 - SYS_NOW, label="旧数据缺口兼容口径")
ck(c_old["real_pnl_now_cny"] == -116000, "旧数据实时收益退化为手填值")
close(c_old["real_assets_cny"], MV + 52800, label="旧数据现金不做联动叠加")

# 未录入锚点时给多少流水都不该冒出数字
cn = AA.compute({"cash_cny": None, "reference_pnl_cny": None}, MV, SYS_NOW,
                net_trade_flow_cny=-30000.0, ext_flow_cny=-8000.0)
ck(cn["cash_now_cny"] is None and cn["real_pnl_now_cny"] is None,
   "未录入时联动结果也是 None，不拿 0 冒充")

# asof 日期脏数据（load_store 兜底成 None）
p3 = tmp("aa_dirty_asof.json")
p3.write_text(json.dumps({"cash_cny": 100, "reference_pnl_cny": -50,
                          "asof_date": "2026/11/13", "system_pnl_snapshot_cny": "x"}),
              encoding="utf-8")
dd = AA.load_store(p3)
ck(dd["asof_date"] is None and dd["system_pnl_snapshot_cny"] is None,
   "脏基准日/脏快照兜底为 None")
p3.unlink(missing_ok=True)

# ---------------- 静态接线 ----------------
app = (BASE / "app.py").read_text(encoding="utf-8")
html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
ck("import account_anchors" in app, "app 导入模块")
ck('@app.get("/api/account-anchors")' in app and '@app.put("/api/account-anchors")' in app,
   "两个端点存在")
ck("_account_view()" in app and "account_anchors.compute(" in app, "端点走纯函数计算")
ck('"account": _account_view()' in app, "快照报告响应带账户锚点（联动）")
ck('p.get("market_value_cny") or 0 for p in rows' in app
   and 'p.get("total_pnl_cny") or 0 for p in rows' in app,
   "系统推算值取 /api/holdings 同一口径")
seg = app[app.index("class AccountAnchorIn"):app.index("@app.get(\"/api/trades\")")]
ck("CREATE TABLE" not in seg and "INSERT INTO" not in seg, "不改 schema / 不写交易表")

ck("id=\"pnl-summary\"" in html and "设置账户基准" in html, "持仓页汇总与设置入口")
for label in ("真实现金", "总参考收益", "账户真实总资产", "未覆盖缺口"):
    ck(label in html, f"持仓页字段 {label}")
ck("/api/account-anchors" in html and "editAccountAnchors" in html, "编辑函数与端点接线")
ck("jsonPut('/api/account-anchors'" in html, "保存走 PUT")
ck("账户真实口径（基准 + 交易联动）" in html and "不代表账户资产" in html,
   "快照对账行用真实现金替换推算现金的口径说明")
ck("系统未覆盖收益缺口" in html and "renderSnapshotAttribution(r.attribution, r.account)" in html,
   "归因卡联动账户锚点")
ck("rep.account = d.account" in html and "let _ACCOUNT = null" in html,
   "报告响应回写全局缓存，后续功能可复用")
js = html[html.index("const _SNAP ="):html.index("// ---- 标签页 ----")]
ck("editAccountAnchors" in js and "_SNAP.reportDate" in js, "保存后刷新持仓与已加载的快照报告")

# ---------------- 买卖联动前端接线（2026-11-14） ----------------
ck("_net_trade_flow_cny" in app and "net_in_range" in app,
   "后端真的汇总基准日后交易净流与出入金")
ck("asof_date" in app and "body.note, asof, sysp)" in app,
   "PUT 端点接基准日与系统盈亏快照")
ck("cash_now_cny" in html and "real_pnl_now_cny" in html,
   "持仓汇总优先用联动值（旧数据兜底手填值）")
ck('id="t-cash-hint"' in html and "function updateTradeCashHint" in html,
   "交易表单现金联动预览")
ck("updateTradeCashHint()" in html, "输入事件绑定预览刷新")
ck("已联动扣减" in html and "已联动增加" in html, "记账成功提示带联动语义")

# ---------------- 公众号关键词检索接线（2026-11-14） ----------------
wmp = (BASE / "wechat_mp.py").read_text(encoding="utf-8")
ck("def list_articles" in wmp and "q: str = \"\"" in wmp, "list_articles 支持关键词参数")
ck("content_text ILIKE" in wmp and "s.name ILIKE" in wmp, "检索扫标题/摘要/正文/作者/公众号名")
ck('replace("%", r"\\%")' in wmp, "检索转义 % 和 _ 通配符")
ck("q: str = \"\"" in app and "200 if (q or \"\").strip()" in app,
   "/api/mp/articles 透传 q 且检索放宽上限")
ck('id="mp-search"' in html and "function mpClearSearch" in html,
   "前端检索框与清除函数")
ck("encodeURIComponent(q)" in html and "命中" in html, "检索词编码并显示命中数")

# ---------------- 新闻关键词检索接线（2026-11-14） ----------------
ck("q: str = \"\"" in app and "n.title ILIKE" in app, "/api/news 支持关键词并扫标题/媒体/来源")
ck('replace("%", r"\\%")' in app, "新闻检索同样转义 % 和 _")
ck('id="news-search"' in html and "function newsClearSearch" in html,
   "新闻 tab 检索框与清除函数")
ck("'&q=' + encodeURIComponent(q)" in html and "命中" in html,
   "新闻检索词编码并显示命中数")

p.unlink(missing_ok=True)
print(f"account_anchors_test: {N}/{N} 全过")
