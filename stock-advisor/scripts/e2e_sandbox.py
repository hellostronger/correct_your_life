# -*- coding: utf-8 -*-
"""端到端：这篇微盘股策略要市值选股，跑通它就算整条链路验证完。

《万得微盘股指数复刻策略》选股逻辑就一句：
    q = query(valuation.code, valuation.market_cap)
    df = get_fundamentals(q, date=signal_date)
    df.sort_values('market_cap').head(400)      # 取市值最小的 400 只
它正好是最吃「估值数据」的一类策略，所以最适合当验收样本。
"""
import json
import sys
import tempfile
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


print("=== 1. 取真实源码 ===")
with gc() as conn, conn.cursor() as cur:
    cur.execute("""SELECT s.post_id, a.title, s.code, s.syntax_state
                   FROM sa_strategy_source s
                   JOIN sa_strategy_article a ON a.post_id=s.post_id
                   WHERE s.syntax_ok ORDER BY s.lines DESC LIMIT 1""")
    pid, title, code, syn = cur.fetchone()
print("  %s（%s）" % (title, pid[:12]))

print()
print("=== 2. 静态扫 ===")
scan = JS.static_scan(code)
print("  %d 行 / %d 函数 / 有 initialize=%s / 风险 %d 项"
      % (scan["n_lines"], scan["n_funcs"], scan["has_initialize"],
         len(scan["risks"])))

print()
print("=== 3. 切行情 + 估值两个切片（全 36 只）===")
with gc() as conn, conn.cursor() as cur:
    cur.execute("SELECT DISTINCT code FROM sa_market_kline ORDER BY code")
    codes = [r[0] for r in cur.fetchall()]
td = tempfile.mkdtemp(prefix="jqe2e_")
w = Path(td)
k_csv = w / "data.csv"
v_csv = w / "val.csv"
ki = JS.build_slice_csv(gc, codes, "2024-01-01", "2026-09-30", k_csv,
                        benchmark="510300")
vi = JS.build_valuation_csv(gc, codes, "2024-01-01", "2026-09-30", v_csv)
print("  行情: %d 只 x %d 天, %.0f KB" % (len(ki["codes"]), ki["days"],
                                        ki["bytes"] / 1024))
print("  估值: %d 只 x %d 天, %.0f KB  %s"
      % (len(vi["codes"]), vi["days"], vi.get("bytes", 0) / 1024,
         vi.get("note", "")))

print()
print("=== 4. 本地干跑 ===")
r = JS.run_local(code, k_csv, "2024-01-01", "2026-09-30", 1_000_000,
                 benchmark="510300", timeout=600, val_csv=v_csv)
print("  ok = %s" % r.get("ok"))
if not r.get("ok"):
    print("  error: %s" % (r.get("error") or "?"))
    tb = r.get("traceback") or ""
    for l in tb.split("\n")[-14:]:
        print("    " + l[:130])
    for e in (r.get("callback_errors") or [])[:5]:
        print("    CB %s" % str(e)[:130])
    for k2, v2 in list((r.get("callback_traceback") or {}).items())[:1]:
        for l in str(v2).split("\n")[-10:]:
            print("      " + l[:128])
else:
    m = r["metrics"]
    ts = r["trade_stats"]
    print("  指标: 总收益 %.2f%%  年化 %.2f%%  最大回撤 %.2f%%  夏普 %.2f"
          % (m["total_return"] * 100, m["annual_return"] * 100,
             m["max_drawdown"] * 100, m["sharpe"]))
    print("  交易: %d 笔（卖出 %d）胜率 %.1f%%  平均每笔盈亏 %.0f"
          % (ts["n_trades"], ts["n_sell_trades"], ts["win_rate"] * 100,
             ts["avg_pnl_per_sell"]))
    print("  交易日 %d  被拒 %d  回调错误 %d  用时 %.0fs"
          % (r["n_days"], r.get("n_rejected", 0),
             r.get("n_callback_errors", 0), r.get("elapsed", 0)))
    print("  前 3 笔成交:")
    for t in r["trades"][:3]:
        print("    %s %s %s x%d @%.2f" % (t["date"], t["side"], t["code"],
                                           t["shares"], t["price"]))
    print("  告警:")
    for x in (r.get("warnings") or [])[:5]:
        print("    - %s" % x[:120])
    print("  策略日志:")
    for x in (r.get("log_tail") or [])[:5]:
        print("    " + str(x)[:120])

print()
print("=== 5. 落库（sa_backtest_run / sa_backtest_daily / sa_sandbox_run）===")
if not r.get("ok"):
    print("  跑失败，不落库（落一个空结果进回测表只会污染列表）")
    conn_close = True
else:
    run_id = JS.save_run(gc, r, title[:150], "2024-01-01", "2026-09-30",
                         codes[:40], {}, 1_000_000, benchmark="510300",
                         article_id=pid)
    with gc() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, name, total_return, annual_return,
                              max_drawdown, sharpe, win_rate, trade_count
                       FROM sa_backtest_run WHERE id=%s""", (run_id,))
        row = cur.fetchone()
        print("  sa_backtest_run: id=%s %s" % (row[0], row[1][:36]))
        print("    总收益 %.2f%% 年化 %.2f%% 回撤 %.2f%% 夏普 %.2f 胜率 %.1f%% 交易 %d"
              % (row[2], row[3], row[4], row[5], row[6], row[7]))
        cur.execute("SELECT count(*) FROM sa_backtest_daily WHERE run_id=%s",
                    (run_id,))
        print("    逐日净值 %d 行" % cur.fetchone()[0])
conn.close()
print()
print("=== 本地干跑结果留存: %s ===" % json.dumps(
    {"metrics": r.get("metrics"), "trade_stats": r.get("trade_stats")},
    ensure_ascii=False))
