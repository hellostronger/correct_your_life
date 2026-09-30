# -*- coding: utf-8 -*-
"""删除脏的回测记录（先备份，可回滚）。

删的是哪两条、为什么删，规则都写在下面而不是靠印象：
    sa_backtest_run #1 / #2 —— 2026-09-30 03:57 的两条
    它们是回测引擎 `_trade` 里 cash 传值 bug 的产物（cash -= cost 只改
    局部变量 -> 复利式无限加仓）。表现就是超额收益 1686% / 4013%，
    而同期 510300 基准只有 45.6% / 49.4%。这个量级不可能是真实策略。

**先备份再删**，备份写到 stock-advisor/_dirty_backup_<时间戳>.json，
里面有完整字段 + 对应的逐日净值。这样即使判断错了也能一条条捞回来。

判据（自动化，不靠「看着不对」）：
  - 超额 > 500%      -> 判脏
  - error 非空        -> 判脏（跑失败的）
  - 逐日 < 50 天      -> 判脏（样本太短，指标没意义）
"""
import json
import sys
from datetime import datetime
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
conn.autocommit = False
cur = conn.cursor()

cur.execute("""SELECT id, strategy_id, name, start_date, end_date, universe,
                      params, init_cash, total_return, annual_return,
                      max_drawdown, sharpe, win_rate, trade_count, turnover,
                      benchmark, bench_return, excess, error, elapsed_ms,
                      created_at
               FROM sa_backtest_run ORDER BY id""")
cols = [c[0] for c in cur.description]
runs = [dict(zip(cols, r)) for r in cur.fetchall()]

dirty, reasons_of = [], {}
for r in runs:
    why = []
    ex = r.get("excess")
    if ex is not None and float(ex) > 500:
        why.append("超额 %.0f%% 远超任何真实策略可能" % float(ex))
    if r.get("error"):
        why.append("带 error（跑失败）")
    cur.execute("SELECT count(*) FROM sa_backtest_daily WHERE run_id=%s",
                (r["id"],))
    nd = cur.fetchone()[0]
    if nd and nd < 50:
        why.append("逐日只有 %d 天，样本太短" % nd)
    if why:
        dirty.append(r)
        reasons_of[r["id"]] = why

if not dirty:
    print("没有脏数据，不动。")
    conn.close()
    raise SystemExit(0)

print("=== 判定为脏的 %d 条 ===" % len(dirty))
for r in dirty:
    print("  #%-4s %-28s %s" % (r["id"], (r["name"] or "")[:28],
                                 "；".join(reasons_of[r["id"]])))

backup = {"deleted_at": datetime.now().isoformat(timespec="seconds"),
          "reason_rules": ["excess > 500%", "error != ''", "daily < 50 days"],
          "runs": [], "daily": {}}
for r in dirty:
    rid = r["id"]
    backup["runs"].append({**{k: str(v) for k, v in r.items()},
                           "_why": reasons_of[rid]})
    cur.execute("SELECT trade_date, equity, cash, position_value, drawdown "
                "FROM sa_backtest_daily WHERE run_id=%s ORDER BY trade_date",
                (rid,))
    dcols = [c[0] for c in cur.description]
    backup["daily"][str(rid)] = [
        {k: str(v) for k, v in zip(dcols, row)} for row in cur.fetchall()]
    print("  #%s 逐日 %d 行" % (rid, len(backup["daily"][str(rid)])))

bp = ROOT / ("_dirty_backup_%s.json"
             % datetime.now().strftime("%Y%m%d_%H%M%S"))
bp.write_text(json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n=== 备份已写：%s（%.0f KB）===" % (bp.name, bp.stat().st_size / 1024))

ids = [r["id"] for r in dirty]
cur.execute("DELETE FROM sa_backtest_run WHERE id = ANY(%s)", (ids,))
print("已删 sa_backtest_run %d 条（逐日净值由外键 ON DELETE CASCADE 清掉）"
      % cur.rowcount)
conn.commit()

cur.execute("SELECT count(*) FROM sa_backtest_run")
print("sa_backtest_run 剩 %d 条" % cur.fetchone()[0])
cur.execute("SELECT count(*) FROM sa_backtest_daily")
print("sa_backtest_daily 剩 %d 行" % cur.fetchone()[0])
cur.execute("SELECT id, name, total_return, trade_count FROM sa_backtest_run "
            "ORDER BY id")
for r in cur.fetchall():
    print("  #%s %-30s %s%% %s 笔" % (r[0], (r[1] or "")[:30], r[2], r[3]))
print("\n如需回滚：把 %s 里的 runs/daily 插回 sa_backtest_run / sa_backtest_daily"
      % bp.name)
conn.close()
