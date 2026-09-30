# -*- coding: utf-8 -*-
"""删脏数据前的体检：先列清楚，再备份，最后才删。

不直接删的原因：这两条是回测引擎 cash bug 的产物（年化 1710%/4050%），
我得先确认它们确实是坏的、而不是「策略真的赚了 150 倍」。
判断依据：同期基准 510300 只有 45.6%/49.4%，超额 15646%/10796% ——
这个量级不可能是真实策略表现。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from psycopg2 import connect                      # noqa: E402

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

conn = connect(connect_timeout=30, **DB)
conn.autocommit = True
cur = conn.cursor()

print("=== 1. sa_backtest_run 全部 ===")
cur.execute("""SELECT id, name, start_date, end_date, init_cash, total_return,
                     annual_return, max_drawdown, sharpe, win_rate,
                     trade_count, benchmark, bench_return, excess,
                     left(error, 60), created_at
              FROM sa_backtest_run ORDER BY id""")
runs = cur.fetchall()
hdr = ("id", "name", "区间", "初始资金", "总收益%", "年化%", "回撤%", "夏普",
       "胜率%", "交易", "基准", "基准收益%", "超额%", "error", "时间")
print("  " + " ".join("%-9s" % h for h in hdr))
for r in runs:
    print("  " + " ".join("%-9s" % str(x)[:9] for x in r))

print()
print("=== 2. 逐条判断是否脏 ===")
# 判断规则（写清楚，免得下次靠感觉删）：
#   a) 超额收益 > 500% -> 不可能，任何真实 A 股策略做不到
#   b) error 非空 -> 跑失败的
#   c) 交易日数 < 50 -> 样本太短，指标没意义
dirty = []
for r in runs:
    (rid, name, sd, ed, cash, tr, ar, mdd, sh, wr, tc, bm, br, ex,
     err, ca) = r
    reasons = []
    if ex is not None and float(ex) > 500:
        reasons.append("超额 %.0f%% 远超任何真实策略可能" % float(ex))
    if err:
        reasons.append("带 error（跑失败）")
    cur.execute("SELECT count(*) FROM sa_backtest_daily WHERE run_id=%s", (rid,))
    nd = cur.fetchone()[0]
    if nd and nd < 50:
        reasons.append("逐日只有 %d 天，样本太短" % nd)
    if reasons:
        dirty.append((rid, name, reasons, nd))
        print("  #%-3s %-30s -> %s（逐日 %d 行）"
              % (rid, (name or "")[:30], "；".join(reasons), nd))

print()
print("=== 3. 其他表里的可疑数据 ===")
cur.execute("""SELECT id, post_id, left(strategy_name,30), ok, n_days, n_trades,
                      total_return, left(error,60), created_at
               FROM sa_sandbox_run ORDER BY id""")
for r in cur.fetchall():
    print("  sandbox#%s %-30s ok=%s 天%s 交易%s %s%% %s"
          % (r[0], (r[2] or "")[:30], r[3], r[4], r[5], r[6],
             (r[7] or "")[:40]))
cur.execute("""SELECT article_id, left(title_zh,30), portable_score,
                      completeness, extract_at FROM sa_strategy_digest""")
for r in cur.fetchall():
    print("  digest  %-30s %s 分 完整度%s" % (r[1], r[2], r[3]))
cur.execute("""SELECT syntax_state, count(*) FROM sa_strategy_source
               GROUP BY syntax_state""")
print("  源码可运行性: %s" % dict(cur.fetchall()))
cur.execute("SELECT crawl_state, count(*) FROM sa_crawl_queue GROUP BY crawl_state")
print("  抓取队列: %s" % dict(cur.fetchall()))

print()
print("=== 4. 待删清单 ===")
if not dirty:
    print("  （无）")
for rid, name, reasons, nd in dirty:
    print("  sa_backtest_run #%s  %s  逐日 %d 行（外键级联删）"
          % (rid, name, nd))
conn.close()
