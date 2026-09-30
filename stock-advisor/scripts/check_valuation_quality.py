# -*- coding: utf-8 -*-
"""全市场估值数据质量体检。

为什么要专门体检
----------------
「表里有 360 万行」不等于「数据是对的」。这类表最容易出的问题都不会报错，
只会让回测结果悄悄变成另一回事：
  - 每日代码数应该是平滑增长（新公司上市），如果某天突然掉一半，
    说明那天只灌了一半 —— 而按市值取最小 N 只的策略会因此选错票
  - 代码必须和 K 线表**字面一致**，否则两边对不上，市值策略看到「零重合」
  - 总市值必须和「收盘价 x 总股本」对得上，不对就说明字段映射错了
  - 极小值要合理：市值 0 或 NULL 的票会被「取最小市值」当成最便宜优先买入
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg2 import connect                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
      "password": "", "dbname": "postgres"}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
             "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
        if k.strip() in m:
            val = v.strip()
            DB[m[k.strip()]] = int(val) if k.strip() == "DB_PORT" else val


def main():
    conn = connect(connect_timeout=60, **DB)
    conn.autocommit = True
    cur = conn.cursor()

    print("=== 1. 总量 ===")
    cur.execute("SELECT count(*), count(DISTINCT code), min(trade_date),"
                " max(trade_date) FROM sa_stock_valuation")
    n, c, d0, d1 = cur.fetchone()
    print("  %d 行 / %d 只 / %s ~ %s" % (n, c, d0, d1))
    cur.execute("SELECT count(DISTINCT trade_date) FROM sa_stock_valuation")
    print("  覆盖 %d 个交易日" % cur.fetchone()[0])

    print()
    print("=== 2. 每日代码数：有没有「某天只灌了一半」的坑 ===")
    cur.execute("SELECT min(k), max(k), avg(k)::numeric(10,1) FROM ("
                "SELECT trade_date, count(*) k FROM sa_stock_valuation"
                " WHERE trade_date >= '2024-01-01' GROUP BY trade_date) t")
    lo, hi, av = cur.fetchone()
    # avg() 返回 Decimal，乘 float 会 TypeError。统一转 float。
    av = float(av)
    print("  2024 起每日代码数: min %d / max %d / avg %s" % (lo, hi, av))
    cur.execute("""SELECT trade_date, count(*) k FROM sa_stock_valuation
                   WHERE trade_date >= '2024-01-01' GROUP BY trade_date
                   HAVING count(*) < %s ORDER BY 2 LIMIT 10""" % (int(av * 0.8),))
    thin = cur.fetchall()
    if thin:
        print("  !! 明显偏少的日子（低于均值 80%%）:")
        for d, k in thin:
            print("     %s  %d 只" % (d, k))
    else:
        print("  没有异常偏少的日子")

    print()
    print("=== 3. 代码口径是否和 K 线表一致 ===")
    cur.execute("SELECT DISTINCT code FROM sa_stock_valuation")
    v = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT code FROM sa_market_kline")
    k = {r[0] for r in cur.fetchall()}
    print("  估值 %d 只 / K线 %d 只 / 交集 %d 只" % (len(v), len(k), len(v & k)))
    print("  估值有、K线无: %d 只（K线还在灌，正常）" % len(v - k))
    print("  K线有、估值无: %d 只" % len(k - v))
    badfmt = [x for x in list(v)[:99999] if not (isinstance(x, str) and len(x) == 6 and x.isdigit())]
    print("  代码格式异常: %d 个 %s" % (len(badfmt), badfmt[:5]))

    print()
    print("=== 4. 抽查：总市值 == 收盘价 x 总股本？（映射对不对）===")
    cur.execute("""SELECT v.code, v.trade_date, v.total_market_cap, v.last_price,
                          v.total_shares
                   FROM sa_stock_valuation v
                   JOIN sa_market_kline k
                     ON k.code = v.code AND k.trade_date = v.trade_date
                   WHERE v.last_price > 0 AND v.total_shares > 0
                     AND v.trade_date = '2026-09-30'
                   LIMIT 8""")
    rows = cur.fetchall()
    for code, d, cap, px, sh in rows:
        calc = float(px) * float(sh)
        dev = (float(cap) - calc) / calc * 100 if calc else 0
        flag = "OK " if abs(dev) < 1.0 else "!! "
        print("  %s %s  表内市值 %14.2f  收盘x股本 %14.2f  偏差 %+.3f%%"
              % (flag, code, float(cap), calc, dev))

    print()
    print("=== 5. 有没有市值为 0/缺失的行（会被当成最便宜）===")
    cur.execute("""SELECT count(*) FROM sa_stock_valuation
                   WHERE total_market_cap IS NULL OR total_market_cap <= 0""")
    print("  市值 <=0 或 NULL: %d 行" % cur.fetchone()[0])
    cur.execute("""SELECT count(*) FROM sa_stock_valuation
                   WHERE last_price IS NULL OR last_price <= 0""")
    print("  收盘价 <=0 或 NULL: %d 行" % cur.fetchone()[0])
    cur.execute("""SELECT min(total_market_cap), max(total_market_cap)
                   FROM sa_stock_valuation WHERE trade_date='2026-09-30'""")
    a, b = cur.fetchone()
    print("  2026-09-30 市值区间: %.2f 亿 ~ %.0f 亿"
          % (float(a) / 1e8, float(b) / 1e8))

    print()
    print("=== 6. 换手率这一列（本地已知缺口，如实记录）===")
    cur.execute("""SELECT count(*) FROM sa_stock_valuation
                   WHERE turnover_rate IS NOT NULL""")
    print("  有换手率的行: %d / %d" % (cur.fetchone()[0], n))

    conn.close()


if __name__ == "__main__":
    main()
