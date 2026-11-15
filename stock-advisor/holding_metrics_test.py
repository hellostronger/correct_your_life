"""累计回本价离线测试：不联网、不连库、不 import app。

跑法：D:\\Python312\\python.exe holding_metrics_test.py
"""
from __future__ import annotations

import re
from pathlib import Path

import holding_metrics as HM

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


def close(a, b, tol=0.02, label=""):
    ck(abs(float(a) - float(b)) <= tol, f"{label} 期望 {b}，实际 {a}")


# ---------------- 数学 ----------------
# 真实数据：00100 MINIMAX 2100 股 / 成本 167.7193 / 已实现 −27797.49 / 现价 152.4
r = HM.breakeven(167.7193, -27797.49, 2100, 152.4, 0.85)
close(r["price"], 180.9562, tol=0.001, label="累计回本价 HKD")
close(r["price_cny"], 153.8128, tol=0.001, label="累计回本价折 CNY")
close(r["pct"], 18.74, tol=0.02, label="距现价还需涨")
close((r["price"] - 167.7193) * 2100 + (-27797.49), 0, tol=0.02,
      label="★恒等式：(回本价−成本)×股数 + 已实现 = 0")
ck(r["price"] > 167.7193, "已实现亏损会把回本价抬到摊薄成本之上")

a = HM.breakeven(10, -200, 100, 9, 1.0)
close(a["price"], 12, label="A股：已实现亏损摊进股价")
close(a["pct"], 33.33, label="A股：还需涨")
close(a["price_cny"], 12, label="A股不折算")

b = HM.breakeven(10, 500, 100, 15, 1.0)
close(b["price"], 5, label="累计盈利票：回本价低于成本")
ck(b["pct"] < 0, "累计盈利票标记已回本")

ck(HM.breakeven(10, -200, 0, 9)["price"] is None, "无持股不算回本价")
ck(HM.breakeven(0, -200, 100, 9)["price"] is None, "成本缺失不算回本价")
ck(HM.breakeven(10, -200, 100, None)["pct"] is None, "缺现价仍给回本价但不给百分比")
ck(HM.breakeven(None, None, None, None)["price"] is None, "脏数据不抛异常")
ck(HM.breakeven_price(10, 5000, 100) == 0.0, "极端盈利时回本价不为负")

# ---------------- 静态接线 ----------------
app = (BASE / "app.py").read_text(encoding="utf-8")
html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
ck("import holding_metrics" in app, "app 导入模块")
ck("holding_metrics.breakeven(" in app, "_holdings_with_pnl 调用纯函数")
for f in ("breakeven_price", "breakeven_price_cny", "breakeven_pct", "currency"):
    ck(f'p["{f}"]' in app, f"后端字段 {f}")
ck("_market_of(p[\"code\"]) == \"hk\"" in app, "币种按代码判定，汇率失败也标 HKD")
ck("累计回本价(¥)" in html and "<th" in html, "表头新增列")
seg = html[html.index("const beCny ="):html.index("const mvCny =") + 200]
ck("i.breakeven_price_cny" in seg and "i.breakeven_pct" in seg, "前端读取新字段")
ck("原${fmtPx(i.breakeven_price)}HKD" in html, "港股下方显示 HKD 原币回本价")
ck("i.currency === 'HKD' || !!i.fx_rate" in html, "港股判定不依赖汇率是否取到")
ck("${beCell}" in html, "回本价单元格真的插进表格行")
ck("还需 +" in html and "已回本" in html, "距现价涨幅 / 已回本提示")
ck("股摊）" in html and "breakeven_pct > 100" in html,
   "已实现亏损摊到少量剩余股时给出解释，避免极端回本价被误读")

# ---------------- 回摊薄需涨 recover_to_avg_cost（2026-11-16 用户要求） ----------------
ck(HM.recover_to_avg_cost(100.0, 80.0) == 25.0, "跌 20% → 需涨 25%（除法不对称）")
ck(HM.recover_to_avg_cost(167.72, 150.0) == 11.81,
   f"00100 样例 167.72/150 → 11.81（实际 {HM.recover_to_avg_cost(167.72, 150.0)}）")
ck(HM.recover_to_avg_cost(100.0, 100.0) == 0.0, "现价==成本 → 0.0（前端显示已到）")
ck(HM.recover_to_avg_cost(100.0, 120.0) == -16.67, "现价在成本上方 → 负值（前端显示成本下方）")
ck(HM.recover_to_avg_cost(50.0, 25.0) == 100.0, "腰斩 → 需涨 100%（让百分比错觉现形）")
ck(HM.recover_to_avg_cost(None, 80.0) is None, "无成本 → None")
ck(HM.recover_to_avg_cost(100.0, None) is None, "无现价 → None（不编造）")
ck(HM.recover_to_avg_cost(0, 80.0) is None, "成本 0 → None")
ck(HM.recover_to_avg_cost("abc", 80.0) is None, "脏成本 → None")
# 与 breakeven 口径的差异钉死：同输入，两个 pct 不同（除非 realized=0）
be = HM.breakeven(100.0, -500.0, 100, 80.0)
rc = HM.recover_to_avg_cost(100.0, 80.0)
ck(be["pct"] > rc, "有已实现亏损时累计口径 pct > 摊薄口径 pct")

# 静态接线
app = (BASE / "app.py").read_text(encoding="utf-8")
ck("recover_to_avg_cost(" in app and 'p["recover_pct"]' in app, "app.py 写出 recover_pct")
html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
ck("回摊薄需涨" in html, "表头新增列")
ck("i.recover_pct" in html and "recCell" in html, "前端读取新字段并插入行")
ck("成本下方" in html, "现价在成本上方有独立显示（不是负百分比裸奔）")

print(f"holding_metrics_test: {N}/{N} 全过")
