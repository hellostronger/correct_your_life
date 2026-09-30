"""端到端实测：真实抓来的策略源码 -> 切数据 -> 沙箱跑 -> 落库。

用**库里那篇多因子 LightGBM 策略**当主角 —— 它的源码里有
lgb_params、cross_section_preprocess、set_order_cost，正是对沙箱
和 API 适配层最狠的考验。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jq_sandbox as JS                                    # noqa: E402
from psycopg2 import connect                               # noqa: E402

DB = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
      "password": "", "dbname": "postgres"}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" not in line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
         "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    if k.strip() in m:
        val = v.strip()
        DB[m[k.strip()]] = int(val) if k.strip() == "DB_PORT" else val


def get_conn():
    c = connect(connect_timeout=30, **DB)
    c.autocommit = False
    return c


def scan_funcs(code: str) -> list[str]:
    import re
    return re.findall(r"^\s*def\s+(\w+)", code, re.M)


print("=== 1. 从库里挑一篇最完整的真实策略源码 ===")
# 挑「行数最多且含 initialize」的 —— 社区里很多帖只贴了片段（缺
# initialize/选股逻辑），那种本来就跑不起来，先用最完整的这篇验证链路。
with get_conn() as conn, conn.cursor() as cur:
    cur.execute("""SELECT a.post_id, a.title, a.url, s.code, s.n_blocks,
                          s.redacted, s.lines
                   FROM sa_strategy_article a
                   JOIN sa_strategy_source s ON s.post_id = a.post_id
                   WHERE NOT s.redacted AND s.code LIKE '%def initialize%'
                   ORDER BY s.lines DESC LIMIT 1""")
    row = cur.fetchone()
if not row:
    print("  库里没有含 def initialize 的完整策略")
    raise SystemExit(1)
pid, title, url, code, nblocks, redacted, lines = row
print("  %s" % title)
print("  %d 行 / %d 块 / 脱敏=%s / 含 initialize=是" % (lines, nblocks, redacted))
print("  定义了: %s" % ", ".join(scan_funcs(code)))
print("  源码前 3 行:")
for l in code.split("\n")[:3]:
    print("    " + l[:96])

print()
print("=== 2. 静态扫（先在本地抓明显的危险/缺失）===")
scan = JS.static_scan(code)
print("  %d 行 / %d 个函数" % (scan["n_lines"], scan["n_funcs"]))
print("  看起来被脱敏: %s（省略标记 %d 个）"
      % (scan["looks_redacted"], scan["n_stub_markers"]))
for r in scan["risks"][:6]:
    print("  风险[%s] %s %s" % (r["level"], r["kind"], r["detail"]))
if not scan["risks"]:
    print("  无风险命中")

print()
print("=== 3. 静态抠出策略要用哪些标的 ===")
with get_conn() as conn, conn.cursor() as cur:
    cur.execute("SELECT count(*) FROM sa_market_kline")
    total_rows = cur.fetchone()[0]
    cur.execute("""SELECT code, count(*) FROM sa_market_kline
                   GROUP BY code ORDER BY count(*) DESC LIMIT 40""")
    known = {r[0]: r[1] for r in cur.fetchall()}
print("  sa_market_kline 共 %d 行，%d 只票" % (total_rows, len(known)))
refs = JS.referenced_codes(code, set(known))
print("  源码里引用的代码: %s" % (refs[:12] or "（无硬编码）"))
allmkt = "__ALL_MARKET__" in refs
print("  是否全市场策略: %s" % allmkt)

print()
print("=== 4. 切一段真实行情 ===")
# 用本地有数据的那些票当标的（策略原文用的是全市场，这里替成等权的一篮子）
codes = [c for c in known if c][:12]
bench = codes[0] if codes else ""
with get_conn() as conn:
    with tempfile.TemporaryDirectory(prefix="jqslice_") as td:
        csv = Path(td) / "data.csv"
        info = JS.build_slice_csv(get_conn, codes, "2023-01-01", "2025-12-31",
                                  csv, benchmark=bench)
        print("  %d 只 x %d 天，%d 行，CSV %.0f KB"
              % (len(info["codes"]), info["days"], info["rows"],
                 info["bytes"] / 1024))
        # 看看 CSV 头几行，确认 pivot 格式对
        head = csv.read_text(encoding="utf-8").split("\n")[:2]
        print("  表头前 120 字符: %s" % head[0][:120])
        print("  首行前 120 字符: %s" % head[1][:120])

        print()
        print("=== 5. 本地干跑（无隔离，快速失败用）===")
        r = JS.run_local(code, csv, "2023-01-01", "2025-12-31", 1_000_000,
                         benchmark=bench, timeout=180)
        if not r.get("ok"):
            print("  失败: %s" % (r.get("error") or "?"))
            tb = (r.get("traceback") or "")[-1500:]
            if tb:
                for l in tb.split("\n")[-16:]:
                    print("    " + l)
            for key in ("stdout_tail", "stderr_tail"):
                v = r.get(key) or ""
                if v.strip():
                    print("  --- %s（最后 900 字符）---" % key)
                    for l in v.strip()[-900:].split("\n")[-16:]:
                        print("    " % "" if False else "    " + l)
            if r.get("callback_traceback"):
                for k, v in list(r["callback_traceback"].items())[:2]:
                    print("  回调 %s:" % k)
                    for l in v.split("\n")[-8:]:
                        print("    " + l)
        else:
            print("  OK  指标: %s" % json.dumps(r["metrics"], ensure_ascii=False))
            print("      交易: %s" % json.dumps(r["trade_stats"], ensure_ascii=False))
            print("      告警 %d 条，未实现API %s"
                  % (len(r["warnings"]), list(r["unknown_apis"])[:5]))
            for w in r["warnings"][:4]:
                print("        - " + w[:100])
        globals()["_CSV"] = str(csv)
        globals()["_CODE"] = code
        globals()["_BENCH"] = bench
        globals()["_LOCAL_RESULT"] = r
