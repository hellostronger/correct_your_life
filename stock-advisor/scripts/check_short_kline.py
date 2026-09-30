# -*- coding: utf-8 -*-
"""查那 103 只 K 线覆盖不足 300 天的代码：是合理还是漏了。

为什么要在意
------------
截面里混着「只有几十天数据」的票有两个害处：
  1) 策略回测时它们大部分时间是空的，get_price 返回 NaN，
     选股逻辑会把它们当成「没有数据」或「不可交易」而排除/或误判
  2) 更阴的是：如果按市值排序取最小 N 只，它们可能因为市值缺失或异常
     被当成最便宜，于是策略优先买入最缺数据的那批票
所以要分清「刚上市（合理）」和「应该有却没有（漏了）」。
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

conn = connect(connect_timeout=60, **DB)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""SELECT k.code, count(*) days, min(k.trade_date) d0, max(k.trade_date) d1,
                      r.list_date, r.name, r.stale
               FROM sa_market_kline k
               LEFT JOIN sa_stock_roster r ON r.code = k.code
               GROUP BY k.code, r.list_date, r.name, r.stale
               HAVING count(*) < 300
               ORDER BY count(*)""")
rows = cur.fetchall()
print("=== K 线不足 300 天的 %d 只 ===" % len(rows))
print("  %-8s %-6s %-12s %-12s %-12s %-10s %s" %
      ("代码", "天数", "K线起", "K线止", "名册上市日", "名称", "stale"))
for c, d, d0, d1, ld, nm, st in rows[:40]:
    print("  %-8s %-6d %-12s %-12s %-12s %-10s %s"
          % (c, d, d0, d1, ld or "-", (nm or "-")[:8], st))
if len(rows) > 40:
    print("  ... 还有 %d 只" % (len(rows) - 40))

print()
print("=== 分类：K线起始日 vs 名册上市日 ===")
# 上市日晚于 2024-03 的，是合理的新股（数据本来就只有这么多）
cur.execute("""SELECT count(*) FILTER (WHERE r.list_date >= '2024-03-01'),
                      count(*) FILTER (WHERE r.list_date IS NULL),
                      count(*) FILTER (WHERE r.list_date < '2024-03-01'),
                      count(*) FILTER (WHERE r.list_date <= k.d0::date)
               FROM (SELECT code, min(trade_date) d0, count(*) n
                     FROM sa_market_kline GROUP BY code HAVING count(*) < 300) k
               LEFT JOIN sa_stock_roster r ON r.code = k.code""")
newish, nolist, oldlisted, startsame = cur.fetchone()
print("  名册上市日 >= 2024-03-01（新股，合理）: %d" % newish)
print("  名册里查不到这个代码:                    %d" % nolist)
print("  名册上市日 < 2024-03-01（早该有数据）:   %d" % oldlisted)
print("  上市日 == K线首日（说明确实刚上市）:     %d" % startsame)

print()
print("=== 这 %d 只在名册里吗 ===" % len(rows))
cur.execute("""SELECT count(*) FROM (SELECT code FROM sa_market_kline
               GROUP BY code HAVING count(*) < 300) k
               JOIN sa_stock_roster r ON r.code = k.code""")
print("  在名册里: %d / %d" % (cur.fetchone()[0], len(rows)))

print()
print("=== 它们有没有估值数据（决定会不会被当成最便宜）===")
cur.execute("""SELECT count(DISTINCT k.code) FROM (SELECT code FROM sa_market_kline
               GROUP BY code HAVING count(*) < 300) k
               JOIN sa_stock_valuation v ON v.code = k.code
               WHERE v.total_market_cap > 0""")
print("  有正市值估值: %d 只" % cur.fetchone()[0])

print()
print("=== 结论 ===")
print("  若「新股，合理」占绝大多数 -> 不用管，截面本来就该排除它们")
print("  若「早该有数据」很多 -> 说明腾讯对某些代码没给数据，需要补源")
conn.close()
