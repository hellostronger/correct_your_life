# -*- coding: utf-8 -*-
"""把上一篇策略丢进 101 的真容器里跑，验证隔离环境下也通。

前面那些都是「本地干跑」（无隔离），只能验证 API 层对不对。
这一步才验证：--network=none / 只读根 / 512m 内存 / 非 root 之下，
策略还能不能正常跑完、结果是否一致。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jq_sandbox as JS                              # noqa: E402
from psycopg2 import connect                         # noqa: E402

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


def gc():
    c = connect(connect_timeout=30, **DB)
    c.autocommit = False
    return c


print("=== 0. 先查沙箱那边通不通 ===")
h = JS.sandbox_health()
print("  ok=%s image=%r 磁盘可用=%s 内存可用=%sMB"
      % (h.get("ok"), h.get("image"), h.get("disk_free"),
         h.get("mem_available_mb")))
if not h.get("ok"):
    print("  %s" % h.get("hint"))
    raise SystemExit(1)

with gc() as conn, conn.cursor() as cur:
    cur.execute("""SELECT s.post_id, a.title, s.code
                   FROM sa_strategy_source s
                   JOIN sa_strategy_article a ON a.post_id=s.post_id
                   WHERE s.syntax_ok ORDER BY s.lines DESC LIMIT 1""")
    pid, title, code = cur.fetchone()
    cur.execute("SELECT DISTINCT code FROM sa_market_kline ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
print("  策略: %s（%s）  标的 %d 只" % (title[:34], pid[:10], len(codes)))

td = tempfile.mkdtemp(prefix="jqdock_")
w = Path(td)
k_csv = w / "data.csv"
v_csv = w / "val.csv"
JS.build_slice_csv(gc, codes, "2024-01-01", "2026-09-30", k_csv,
                   benchmark="510300")
JS.build_valuation_csv(gc, codes, "2024-01-01", "2026-09-30", v_csv)

print()
print("=== 1. 本地干跑（作为对照基准）===")
t0 = time.time()
local = JS.run_local(code, k_csv, "2024-01-01", "2026-09-30", 1_000_000,
                     benchmark="510300", timeout=600, val_csv=v_csv)
print("  %.0fs ok=%s  指标=%s"
      % (time.time() - t0, local.get("ok"),
         json.dumps(local.get("metrics"), ensure_ascii=False)))
if not local.get("ok"):
    print("  本地都跑失败，先修那个: %s" % local.get("error"))
    raise SystemExit(1)

print()
print("=== 2. 101 容器里跑（完整隔离）===")
print("  隔离参数: %s" % json.dumps(JS.SANDBOX_DEFAULTS, ensure_ascii=False))
t0 = time.time()
dock = JS.run_in_docker(code, k_csv, "2024-01-01", "2026-09-30", 1_000_000,
                        benchmark="510300", timeout=300, val_csv=v_csv)
el = time.time() - t0
print("  %.0fs（容器内耗时 %.0fs）ok=%s" % (el, dock.get("elapsed", -1),
                                           dock.get("ok")))
if not dock.get("ok"):
    print("  error: %s" % (dock.get("error") or "?"))
    tb = dock.get("traceback") or ""
    for l in tb.split("\n")[-14:]:
        print("    " + l[:130])
    print("  stdout 尾部: %s" % (dock.get("stdout_tail") or "")[-500:])
    print("  stderr 尾部: %s" % (dock.get("stderr_tail") or "")[-500:])
    raise SystemExit(1)

print("  指标: %s" % json.dumps(dock["metrics"], ensure_ascii=False))
print("  交易: %s" % json.dumps(dock["trade_stats"], ensure_ascii=False))
print("  天数 %d  成交 %d  被拒 %d  回调错误 %d"
      % (dock["n_days"], dock["n_trades_total"], dock.get("n_rejected", 0),
         dock.get("n_callback_errors", 0)))

print()
print("=== 3. 两边结果对比（容器 vs 本地，应该一致）===")
lm, dm = local["metrics"], dock["metrics"]
same = True
for k in ("total_return", "annual_return", "max_drawdown", "sharpe",
          "trading_days"):
    a, b = lm.get(k), dm.get(k)
    ok = (a == b)
    same &= ok
    print("  %-14s 本地 %-12s 容器 %-12s %s"
          % (k, a, b, "一致" if ok else "!! 不一致"))
lt, dt = local["trade_stats"]["n_trades"], dock["trade_stats"]["n_trades"]
print("  %-14s 本地 %-12s 容器 %-12s %s"
      % ("n_trades", lt, dt, "一致" if lt == dt else "!! 不一致"))

print()
print("=== 4. 落 sa_sandbox_run（留痕：隔离参数/镜像/被拒订单都要能查）===")
with gc() as conn, conn.cursor() as cur:
    cur.execute(
        """INSERT INTO sa_sandbox_run
           (article_id, post_id, strategy_name, syntax_state, host, image,
            limits, ok, n_days, n_trades, n_rejected, n_callback_errors,
            total_return, annual_return, max_drawdown, sharpe, win_rate,
            warnings, rejected, result, elapsed)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (pid, pid, title[:200], "ok", JS.DEFAULT_HOST, JS.DOCKER_IMAGE,
         json.dumps(JS.SANDBOX_DEFAULTS, ensure_ascii=False), True,
         dock["n_days"], dock["n_trades_total"], dock.get("n_rejected", 0),
         dock.get("n_callback_errors", 0),
         dock["metrics"]["total_return"] * 100,
         dock["metrics"]["annual_return"] * 100,
         dock["metrics"]["max_drawdown"] * 100,
         dock["metrics"]["sharpe"], dock["trade_stats"]["win_rate"] * 100,
         json.dumps(dock.get("warnings") or [], ensure_ascii=False),
         json.dumps((dock.get("rejected") or [])[:50], ensure_ascii=False),
         json.dumps({k: v for k, v in dock.items()
                     if k not in ("daily", "trades")}, ensure_ascii=False,
                     default=str)[:60000],
         dock.get("elapsed", 0)))
    rid = cur.fetchone()[0]
    conn.commit()
    run_id = JS.save_run(gc, dock, title[:150], "2024-01-01", "2026-09-30",
                         codes[:40], {}, 1_000_000, benchmark="510300",
                         article_id=pid)
    cur.execute("""SELECT id, total_return, annual_return, max_drawdown,
                          sharpe, trade_count FROM sa_backtest_run WHERE id=%s""",
                (run_id,))
    row = cur.fetchone()
    print("  sa_sandbox_run   id=%d" % rid)
    print("  sa_backtest_run  id=%d 总收益%.2f%% 年化%.2f%% 回撤%.2f%% 夏普%.2f 交易%d"
          % (row[0], row[1], row[2], row[3], row[4], row[5]))
print()
print("=== 结论：容器与本地%s ===" % ("完全一致" if same else "有差异（见上）"))
