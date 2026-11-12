"""trade_snapshot 离线测试：不联网、不连库、不 import app、不写生产侧车。

跑法：D:\\Python312\\python.exe trade_snapshot_test.py
覆盖：流水归一化 / 持仓回放 / 侧车幂等与损坏容错 / 择时胜率口径 / 已实现胜率 /
      差额恒等式 / 币种折算 / 缺价不猜 / 静态接线 / 与 11 月一次性分析结果交叉校验。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import trade_snapshot as TS

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


def close(a, b, tol=0.02, label=""):
    ck(abs(float(a) - float(b)) <= tol, f"{label} 期望 {b}，实际 {a}")


def tx(i, d, code, side, n, p, **kw):
    return {"id": i, "trade_date": d, "code": code, "side": side,
            "shares": n, "price": p, **kw}


def tmp_store(name="ts_test.json"):
    p = Path(__import__("tempfile").gettempdir()) / name
    p.unlink(missing_ok=True)
    return p


# ---------------- 0. 日K解析（真机翻车点：顺序是「日期,收盘价」） ----------------
ks = ["2026-10-28,20.1", "2026-10-29,20.5", "2026-10-30,20.0",
      "2026-11-02,21.0", "bad", "2026-11-03,NaN", "2026-10-30,0"]
ck(TS.parse_close_on(ks, "2026-10-30") == (20.0, "2026-10-30"), "当日收盘价")
ck(TS.parse_close_on(ks, "2026-10-31") == (20.0, "2026-10-30"), "周末落到前一交易日")
ck(TS.parse_close_on(ks, "2026-10-27") == (None, None), "基准日之前无数据")
ck(TS.parse_close_on(["2026-11-02,21", "2026-10-30,20"], "2026-11-01") == (20.0, "2026-10-30"),
   "乱序输入也要按日期取最后一根")
ck(TS.parse_close_on([], "2026-10-30") == (None, None), "空输入")

# ---------------- 1. 归一化与回放 ----------------
mixed = [
    tx(2, "2026-11-06", "600406", "SELL", 50, 24),
    {"id": "x1", "date": "2026-10-30", "code": "600406", "side": "buy", "shares": 100, "price": 20},
    {"id": 3, "trade_date": "2026-11-07", "code": "", "side": "buy", "shares": 1, "price": 1},
    {"id": 4, "trade_date": "2026-11-07", "code": "000001", "side": "hold", "shares": 1, "price": 1},
]
norm = TS.normalize_trades(mixed)
ck(len(norm) == 2, "坏行/非买卖行应被过滤")
ck(norm[0]["date"] == "2026-10-30" and norm[0]["id"] == "x1", "混合 id 类型排序不炸且按日期")
ck(norm[1]["side"] == "sell", "side 大小写归一")

hist = [tx(1, "2026-09-09", "600406", "buy", 100, 20),
        tx(2, "2026-10-30", "600406", "sell", 40, 22),
        tx(3, "2026-11-06", "600406", "buy", 10, 23),
        tx(4, "2026-09-09", "02513", "buy", 300, 500),
        tx(5, "2026-10-30", "02513", "sell", 300, 480)]
ck(TS.replay_positions(hist, "2026-10-30") == {"600406": 60}, "基准日含当日、清仓票不留在持仓")
ck(TS.replay_positions(hist, "2026-09-08") == {}, "首笔之前无持仓")
ck(TS.replay_positions(hist, "2026-11-30") == {"600406": 70}, "全区间回放")

# ---------------- 2. 侧车存取 ----------------
p = tmp_store()
ck(TS.store_status(p)["count"] == 0 and not TS.store_status(p)["exists"], "空库状态")
s1 = TS.build_snapshot("2026-10-30", {"600406": 100, "02513": 10},
                       {"600406": 20, "02513": 500}, 0.85,
                       names={"600406": "国电南瑞"}, label="10月底", path=p)
close(s1["baseline_value_cny"], 2000 + 5000 * 0.85, label="基准市值含港股折算")
cur = {r["code"]: r["currency"] for r in s1["positions"]}
ck(cur == {"600406": "CNY", "02513": "HKD"}, "币种判定（按 code 取，别假设 positions 顺序）")
TS.build_snapshot("2026-10-30", {"600406": 100}, {"600406": 21}, 0.85, path=p)
snaps = TS.list_snapshots(p)
ck(len(snaps) == 1, "同日重复打 = 覆盖，不堆重复快照")
close(snaps[0]["baseline_value_cny"], 2100, label="覆盖后基准市值更新")
TS.build_snapshot("2026-11-01", {"600406": 100}, {"600406": 22}, 0.85, path=p)
ck([s["date"] for s in TS.list_snapshots(p)] == ["2026-11-01", "2026-10-30"], "列表按日期倒序")
ck(TS.delete_snapshot("2026-10-30", p) and not TS.delete_snapshot("2026-10-30", p), "删除幂等")
p.write_text("{broken", encoding="utf-8")
ck(TS.store_status(p)["error"] and TS.list_snapshots(p) == [], "损坏文件不静默当成功")
p.unlink(missing_ok=True)

# ---------------- 3. 择时胜率口径 ----------------
trades = [
    tx(1, "2026-10-30", "600406", "buy", 100, 20),     # +300 赢（基准日买入，全历史口径算）
    tx(2, "2026-11-06", "600406", "sell", 50, 24),     # 现价23 → +50 赢（卖完跌了）
    tx(3, "2026-11-10", "000001", "buy", 100, 10),     # 现价12 → +200 赢
    tx(4, "2026-11-10", "000002", "buy", 100, 10),     # 现价 8 → -200 输
    tx(5, "2026-11-11", "02513", "buy", 10, 100),      # 现价110 HKD → +85 CNY 赢
    tx(6, "2026-11-11", "000003", "buy", 100, 10),     # 无现价 → 不判
    tx(7, "2026-11-11", "000004", "buy", 100, 10),     # 现价10 → 平手，不进分母
]
prices = {"600406": 23, "000001": 12, "000002": 8, "02513": 110, "000004": 10}
sc = TS.score_trades(trades, prices, 0.85)
w = sc["win_rate"]
ck(w["total"] == 7 and w["priced"] == 6, "缺价那笔不进 priced")
ck(w["wins"] == 4 and w["losses"] == 1 and w["flat"] == 1, "胜负平计数")
close(w["win_rate"], 80.0, label="胜率 = 4/(4+1)")
close(w["contribution_cny"], 435.0, label="净贡献 = 300+50+200-200+85")
ck(w["buy"]["wins"] == 3 and w["buy"]["losses"] == 1, "买入分桶")
ck(w["sell"]["wins"] == 1, "卖出分桶")
hk = next(x for x in sc["trades"] if x["code"] == "02513")
close(hk["contribution_local"], 100.0, label="港股原币贡献 HKD")
close(hk["contribution_cny"], 85.0, label="港股折算 CNY")
ck(next(x for x in sc["trades"] if x["code"] == "000003")["win"] is None, "缺价不判胜负")
ck("000003" in sc["missing_prices"], "缺价代码要被点名")
ck(sc["top_winners"][0]["code"] == "600406" and sc["top_winners"][0]["contribution_cny"] == 300.0,
   "最赚的笔按贡献降序")
ck(sc["top_losers"][0]["code"] == "000002", "最亏的笔按贡献升序")
ck(any(m["month"] == "2026-10" and m["count"] == 1 for m in sc["by_month"]), "按月分组")
ck(any(c["code"] == "600406" and c["count"] == 2 for c in sc["by_code"]), "按股票分组")

after = TS.score_trades(trades, prices, 0.85, since="2026-10-30")
ck(after["win_rate"]["total"] == 6 and after["win_rate"]["wins"] == 3, "since 严格排除基准日及之前")
close(after["win_rate"]["contribution_cny"], 135.0, label="区间净贡献")

rz = TS.sell_realized_map([
    tx(1, "2026-01-01", "000001", "buy", 100, 10),
    tx(2, "2026-01-02", "000001", "buy", 100, 20),
    tx(3, "2026-01-03", "000001", "sell", 50, 25),
    tx(4, "2026-01-04", "000001", "sell", 50, 8),
    tx(5, "2026-01-05", "000002", "sell", 10, 5),   # 超卖（删账）不抛异常
])
close(rz["3"], 500.0, label="摊薄成本15 → 卖25 实现 +500")
close(rz["4"], -350.0, label="摊薄成本15 → 卖8 实现 -350")
ck(rz["5"] == 0.0, "超卖按实际持股封顶且不抛异常")
ck(sc["realized"]["count"] == 1 and sc["realized"]["wins"] == 1, "已实现胜率只数卖出笔")
close(sc["realized"]["pnl_cny"], 200.0, label="已实现盈亏 (24-20)*50")

# ---------------- 4. 快照报告：差额恒等式 ----------------
p2 = tmp_store("ts_report.json")
snap = TS.build_snapshot("2026-10-30", {"600406": 100}, {"600406": 20}, 0.85,
                         label="10月底", path=p2)
rep = TS.build_report(snap, trades, prices, 0.85)
t = rep["totals"]
close(t["baseline_cny"], 2000.0, label="基准市值")
close(t["flat_end_cny"], 2300.0, label="躺平期末 100×23")
close(t["real_end_cny"], 2435.0, label="实际期末 2350+200-200+85")
close(t["flat_gain_cny"], 300.0, label="躺平收益")
close(t["delta_cny"], 135.0, label="差额")
close(t["delta_cny"], after["win_rate"]["contribution_cny"], label="★差额恒等式 = Σ每笔贡献")
ck(t["flat_pct"] == 15.0 and t["real_pct"] is not None, "百分比口径")
close(rep["by_currency"]["HKD"]["real_end"], 100.0, label="港币池不被混加")
ck(rep["trade_count"] == 6, "区间流水笔数")
ck(rep["incomplete_count"] == 1 and "000003" in rep["missing_prices"],
   "只有真缺现价的那只算不全；新建仓票不因无基准价被误报")
ck(all(r["complete"] for r in rep["by_code"] if r["code"] in ("000001", "000002", "02513")),
   "基准日无持仓的票 complete=True")
ck(len(rep["caveats"]) >= 4, "口径说明不能省")

rep2 = TS.build_report(snap, [tx(9, "2026-12-31", "600406", "buy", 1, 20)], {}, 0.85)
ck(rep2["totals"]["delta_cny"] != 0 or rep2["totals"]["flat_end_cny"] == 0,
   "现价全缺时不编造价格")
rep3 = TS.build_report(snap, trades[:1], prices, 0.85)
close(rep3["totals"]["delta_cny"], 0.0, label="快照后无交易 → 躺平=实际")

snap_bad = TS.build_snapshot("2026-10-29", {"600406": 100}, {}, 0.85, path=p2)
ck(snap_bad["missing_prices"] == ["600406"], "建快照时缺价也不丢弃该票")
rep_bad = TS.build_report(snap_bad, trades[:1], prices, 0.85)
ck(rep_bad["incomplete_count"] >= 1, "缺基准价标记 incomplete")
p2.unlink(missing_ok=True)

# ---------------- 5. 静态接线（防止「后端落了盘前端没人读」） ----------------
app_src = (BASE / "app.py").read_text(encoding="utf-8")
html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
ck("import trade_snapshot" in app_src, "app 导入模块")
for ep in ('@app.get("/api/trade-snapshots")', '@app.post("/api/trade-snapshots")',
           '@app.delete("/api/trade-snapshots")',
           '@app.get("/api/trade-snapshots/summary")',
           '@app.get("/api/trade-snapshots/report")'):
    ck(ep in app_src, f"端点存在 {ep}")
seg = app_src[app_src.index("class SnapshotIn"):app_src.index("# ---------------- 加仓摊薄成本预估")]
ck("INSERT INTO sa_trades" not in seg and "DELETE FROM sa_trades" not in seg,
   "快照段绝不写交易流水")
ck("CREATE TABLE" not in seg and "ALTER TABLE" not in seg, "快照段不改 schema")
ck("trade_snapshot.parse_close_on(" in seg and "parts[0], float(parts[1])" not in seg,
   "日K解析必须走有单测的纯函数，别在端点里自己解包")

ck('data-tab="snapshot"' in html, "tab 按钮")
ck('id="tab-snapshot"' in html, "tab section")
ck(re.search(r"const TABS = \[[^\]]*'snapshot'", html) is not None, "TABS 注册")
ck("snapshot:  loadSnapshotTab" in html, "TAB_LOADERS 注册")
for ident in ("snap-meta", "snap-summary", "snap-list", "snap-report",
              "snap-date", "snap-label", "snap-pick", "snap-report-title"):
    ck(f'id="{ident}"' in html, f"DOM id {ident}")
ck('type="date" id="snap-date"' in html, "基准日必须是可选日期控件")
ck("按此日期打快照" in html and "对比到现在" in html, "两步式入口文案齐全")
ck("const wd = p.getDay()" in html and "p.setDate(p.getDate() - 1)" in html,
   "上月末快捷落到工作日，避免存出重复快照")
for fn in ("loadSnapshotTab", "loadSnapshotSummary", "loadSnapshotList",
           "loadSnapshotReport", "renderSnapshotReport", "snapshotCreate", "snapshotDelete",
           "snapshotQuickDate", "snapshotCompareSelected"):
    ck(re.search(r"(async\s+)?function\s+" + fn + r"\s*\(", html) is not None, f"函数定义 {fn}")
js_seg = html[html.index("const _SNAP ="):html.index("// ---- 标签页 ----")]
ck("confirmDlg(" in js_seg, "删除确认走统一组件")
ck(not re.search(r"(?<![A-Za-z])(prompt|confirm)\s*\(", js_seg), "不用原生 prompt/confirm")
ck("snap-date" in js_seg and "snap-pick" in js_seg, "JS 真的读取日期框与快照下拉框")
ck("snapshotCompareSelected()" in html and "sel.value" in js_seg, "选中的快照能触发对比")
ck("/api/trade-snapshots/summary" in js_seg and "/api/trade-snapshots/report" in js_seg,
   "前端真的调用两个只读端点")
ck("一动不动拿到现在" in js_seg and "实际操作到现在" in js_seg,
   "报告结论条把「不动到现在 vs 现在」写清楚")

# ---------------- 6. 与 11 月一次性分析结果交叉校验 ----------------
cross = BASE / "data" / "_nov_compare_result.json"
if cross.exists():
    d = json.loads(cross.read_text(encoding="utf-8"))
    px = {r["code"]: r["pn"] for r in d["rows"]}
    chk = TS.score_trades(d["nov_trades"], px, d["fx"])
    close(chk["win_rate"]["contribution_cny"], d["totals_cny"]["delta"], tol=2.0,
          label="★与一次性分析的操作差额一致")
    print(f"[cross-check] 11月操作差额 {chk['win_rate']['contribution_cny']:+,.0f} CNY，"
          f"胜率 {chk['win_rate']['win_rate']}%")
else:
    print("[cross-check] 跳过（data/_nov_compare_result.json 不存在）")

print(f"trade_snapshot_test: {N}/{N} 全过")
