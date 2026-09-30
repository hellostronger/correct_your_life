# -*- coding: utf-8 -*-
"""批量把 sa_market_kline 里有的票都灌上估值数据。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import valuation_data as VD                      # noqa: E402
from psycopg2 import connect                     # noqa: E402

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


def main():
    with gc() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM sa_market_kline ORDER BY code")
        codes = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT count(DISTINCT code) FROM sa_stock_valuation")
        done = cur.fetchone()[0]
    print("=== K 线里的 %d 只票，已有估值 %d 只 ===" % (len(codes), done))
    print("=== 开始灌估值 ===")
    ok = fail = 0
    for i, code in enumerate(codes, 1):
        t0 = time.time()
        try:
            r = VD.sync_valuation(gc, code, years=4.0)
        except Exception as exc:                # noqa: BLE001
            print("  [%2d/%d] %s 抛异常 %s" % (i, len(codes), code,
                                             str(exc)[:60]))
            fail += 1
            continue
        n = r.get("fetched", 0)
        if n:
            print("  [%2d/%d] %-8s %5d 行 %s~%s (%.1fs)"
                  % (i, len(codes), code, n, r.get("oldest"), r.get("newest"),
                     time.time() - t0))
            ok += 1
        else:
            print("  [%2d/%d] %-8s 无新增 %s" % (i, len(codes), code,
                                                r.get("why") or "已是最新"))
            fail += 1
        time.sleep(0.3)
    with gc() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT code) FROM sa_stock_valuation")
        rows, cs = cur.fetchone()
        cur.execute("""SELECT count(*) FROM sa_stock_valuation
                       WHERE total_market_cap IS NOT NULL""")
        withcap = cur.fetchone()[0]
    print("\n=== 完成：成功 %d / 失败 %d ===" % (ok, fail))
    print("  sa_stock_valuation %d 行 / %d 只，其中有市值 %d 行" % (rows, cs, withcap))


if __name__ == "__main__":
    main()
