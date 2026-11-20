# -*- coding: utf-8 -*-
"""模拟盘懒加载离线测试（2026-11-20，用户报「模拟持仓展示为空」）。

根因链：/api/paper/rounds 的 SELECT 整包回传 decision_raw（jsonb 大文本，
200 行 1.7MB）跨公网（DB 在 101 VPS，实测 ~150KB/s）要 13~18s → 前端
loadPaper 的 Promise.all 整体超时 → 持仓表永远渲染不出来。

修法：列表只传 160 字预览 + 长度；全文经 GET /api/paper/trade-text 按 id
懒加载。本文件全部离线（Fake 连接，不碰库、不联网、不烧 LLM）。
"""
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import paper_trading  # noqa: E402

N = 0


def ck(cond, label):
    global N
    if not cond:
        print(f"FAIL: {label}")
        sys.exit(1)
    N += 1
    print(f"  ok: {label}")


# ---------------- Fake 连接（tuple 游标 + description，模拟 psycopg2） ----------------

class FakeCur:
    def __init__(self, cols, rows):
        self.description = [(c,) for c in cols]
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, cols, rows):
        self._cur = FakeCur(cols, rows)

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _row(**kw):
    base = dict(id=1, code="600036", name="招商银行", trade_date=datetime(2026, 11, 10, 10, 0, 0),
                slot="10:00", side="buy", shares=100, price=10.0, value=1000.0,
                fee_total=5.0, confidence=7, stop_loss_pct=5.0,
                reasoning_head="近20日涨幅强势", reasoning_len=320, report_len=4136,
                status="resolved", auto_closed=False,
                settle_date=None, settle_price=None, raw_return=None,
                alpha_return=None, benchmark="沪深300",
                created_at=datetime(2026, 11, 10, 10, 5, 0))
    base.update(kw)
    return base


ROUNDS_COLS = ["id", "code", "name", "trade_date", "slot", "side", "shares", "price",
               "value", "fee_total", "confidence", "stop_loss_pct", "reasoning_head",
               "reasoning_len", "report_len", "status", "auto_closed", "settle_date",
               "settle_price", "raw_return", "alpha_return", "benchmark", "created_at"]

# ---------------- rounds()：SQL 不再回传大文本 ----------------

buy = _row(id=1, side="buy")
sell = _row(id=2, side="sell", status="closed",
            trade_date=datetime(2026, 11, 17, 14, 0, 0), settle_date=datetime(2026, 11, 17))


def _tup(d):
    return tuple(d[c] for c in ROUNDS_COLS)   # rounds() 按 tuple 行 zip 列名


conn = FakeConn(ROUNDS_COLS, [_tup(buy), _tup(sell)])
deps = {"get_conn": lambda: conn}
out = paper_trading.rounds(deps)
ck("decision_raw" not in conn._cur.sql, "rounds SQL 不再拉 decision_raw（1.7MB jsonb 元凶）")
ck("left(COALESCE(reasoning" in conn._cur.sql, "rounds SQL 只取 160 字预览")
ck("char_length(report)" in conn._cur.sql, "report 长度服务端计算（不回传全文）")
ck(len(out) == 1 and out[0]["exit"], "一买一卖配成一个已平仓回合")
r = out[0]
ck(r["entry"]["reasoning_head"] == "近20日涨幅强势" and r["entry"]["reasoning_len"] == 320,
   "回合 entry 带 reasoning_head + reasoning_len")
ck(r["entry"]["report_len"] == 4136, "回合 entry 带 report_len（弹窗按钮标注全文 N 字）")
ck("reasoning" not in r["entry"] and "report" not in r["entry"],
   "回合 entry 不再携带全文字段（键名不存在）")
ck(r["exit"]["reasoning_len"] == 320 and r["exit"]["report_len"] == 4136,
   "回合 exit 同款懒加载字段")

# 持有中回合（无卖出）
conn2 = FakeConn(ROUNDS_COLS, [_tup(buy)])
out2 = paper_trading.rounds({"get_conn": lambda: conn2})
ck(len(out2) == 1 and out2[0]["exit"] is None, "无卖出 → 持有中回合")
ck(out2[0]["entry"]["reasoning_len"] == 320 and out2[0]["entry"]["report_len"] == 4136,
   "持有中回合 entry 也是懒加载字段")

# ---------------- trade_texts()：按 id 取全文 ----------------

tt_conn = FakeConn(["id", "reasoning", "report"],
                   [(2, "卖出理由全文", "卖出报告全文"), (1, "买入理由全文", "买入报告全文")])
tt_deps = {"get_conn": lambda: tt_conn}
items = paper_trading.trade_texts(tt_deps, ["2", 1, "abc", "-3", "2"])
ck("ANY(%s)" in tt_conn._cur.sql, "trade_texts 用 ANY(%s) 一次取多行")
ck(list(tt_conn._cur.params[0]) == [2, 1],
   "非法/重复 id 被清洗，参数只留正整数（保请求顺序）")
ck([i["id"] for i in items] == [2, 1], "返回顺序跟随请求顺序（不是库顺序）")
ck(items[0]["report"] == "卖出报告全文" and items[0]["report_len"] == len("卖出报告全文"),
   "trade_texts 返回全文且 report_len 由全文算出")
ck(all("reasoning" in i and "report" in i for i in items), "全文字段名与旧行为一致")

boom = {"get_conn": lambda: (_ for _ in ()).throw(AssertionError("不该碰库"))}
ck(paper_trading.trade_texts(boom, []) == [], "空 ids 不碰库")
ck(paper_trading.trade_texts(boom, ["abc", "", "-1"]) == [], "全是非法 id 不碰库")
many = [str(i) for i in range(1, 150)]
cap_conn = FakeConn(["id", "reasoning", "report"], [])
paper_trading.trade_texts({"get_conn": lambda: cap_conn}, many)
ck(len(list(cap_conn._cur.params[0])) == 100, "单次最多 100 个 id（防滥用）")

# ---------------- app.py 静态接线 ----------------

app_src = (BASE / "app.py").read_text(encoding="utf-8")
ck('@app.get("/api/paper/trade-text")' in app_src, "懒加载端点已注册")
ck("paper_trading.trade_texts(" in app_src, "端点真的调用 trade_texts")
pt_seg = app_src[app_src.index("def paper_trades"):app_src.index("def paper_rounds")]
ck("reasoning_head" in pt_seg and "decision_raw" not in pt_seg,
   "/api/paper/trades 列表只传预览（原 SQL 无 decision_raw，保持）")
pr_seg = app_src[app_src.index("def paper_rounds"):app_src.index("def paper_trade_text")]
ck("paper_trading.rounds(" in pr_seg, "rounds 端点仍走 paper_trading.rounds")

# ---------------- 前端静态接线 ----------------

html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
for fn in ("fetchPaperText", "lazyReasonBtn", "expandPaperReason", "openPaperReportLazy"):
    ck(re.search(r"(async\s+)?function\s+" + fn + r"\s*\(", html) is not None,
       f"前端懒加载函数 {fn}")
ck("/api/paper/trade-text" in html, "前端真的调用懒加载端点")
ck("reasonCell" in html and "t.reasoning_head" in html, "回合理由单元格用预览字段")
ck(re.search(r"\bt\.reasoning(?!_head|_len)", html) is None,
   "前端不再有 t.reasoning 全文直引（renderAnchorRecent 的 r.reasoning、expandPaperReason 的 it.reasoning 不受影响）")
ck(re.search(r"\bt\.report(?!_len)", html) is None, "前端不再有 t.report 全文直引")
ck("openPaperReportLazy(this, ${t.id}" in html, "报告按钮带行 id 懒加载")
ck("report_len" in html, "报告按钮标注全文 N 字（不静默丢数据）")
ck("fetchPaperText" in html and "_paperTextCache" in html, "全文有本地缓存（重复展开不重复请求）")

# ---------------- JS 语法（两块 script 全部 node --check） ----------------

blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
ck(len(blocks) >= 2, f"index.html 有 {len(blocks)} 个 script 块")
for i, b in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(b)
        tmp = f.name
    p = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    Path(tmp).unlink(missing_ok=True)
    ck(p.returncode == 0, f"script 块 {i + 1} node --check 通过"
       + ("" if p.returncode == 0 else f": {p.stderr[:300]}"))

print(f"paper_lazy_test: {N}/{N} 全过")
