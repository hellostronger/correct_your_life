# -*- coding: utf-8 -*-
"""探两件事，决定全市场数据怎么灌最快。

1) 估值能不能**批量**取。
   逐只调 RPT_VALUEANALYSIS_DET 是 5000 次请求、每次 ~5-20 秒 = 十几小时。
   但这个报表**不带 SECURITY_CODE 过滤**、只按 TRADE_DATE 过滤的话，
   一页 500 条，5000 只只要 10 个请求。这个差别太大，必须先确认。

2) 全市场到底有多少只可交易的票（排除 ETF/可转债/B 股/退市）。
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from psycopg2 import connect                      # noqa: E402
import valuation_data as VD                       # noqa: E402

UA = VD.UA

print("=== 1. RPT_VALUEANALYSIS_DET 能不能按日期取全市场 ===")
# 不加 SECURITY_CODE 过滤，只按 TRADE_DATE 取最新一个交易日
for day in ("2026-09-30", "2026-09-29", "2026-09-28"):
    flt = urllib.parse.quote('(TRADE_DATE=\'%s\')' % day, safe="()='")
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_VALUEANALYSIS_DET&columns=SECURITY_CODE,"
           "SECURITY_NAME_ABBR,TRADE_DATE,TOTAL_MARKET_CAP,"
           "NOTLIMITED_MARKETCAP_A,PE_TTM,PB_MRQ,PS_TTM,TOTAL_SHARES,"
           "FREE_SHARES_A,CLOSE_PRICE&filter=%s&pageNumber=1&pageSize=500"
           "&sortColumns=SECURITY_CODE&sortTypes=1&source=WEB&client=WEB" % flt)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Referer": "https://data.eastmoney.com/"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        res = d.get("result") or {}
        rows = res.get("data") or []
        total = res.get("count")
        print("  %s -> %.1fs  本页 %d 条  报表声称总数 %s"
              % (day, time.time() - t0, len(rows), total))
        if rows:
            s = rows[0]
            print("     样例: %s %s 总市值%s 流通%s PE=%s PB=%s"
                  % (s.get("SECURITY_CODE"), s.get("SECURITY_NAME_ABBR"),
                     s.get("TOTAL_MARKET_CAP"), s.get("NOTLIMITED_MARKETCAP_A"),
                     s.get("PE_TTM"), s.get("PB_MRQ")))
            codes = [x.get("SECURITY_CODE") for x in rows]
            print("     代码样例: %s" % codes[:8])
    except Exception as exc:                    # noqa: BLE001
        print("  %s 失败: %s" % (day, str(exc)[:110]))

print()
print("=== 2. 名册里有多少只可交易的 A 股 ===")
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
cur.execute("SELECT count(*) FROM sa_stock_roster")
print("  sa_stock_roster 总数: %d" % cur.fetchone()[0])
cur.execute("SELECT column_name FROM information_schema.columns "
            "WHERE table_name='sa_stock_roster'")
print("  列: %s" % ", ".join(r[0] for r in cur.fetchall()))
cur.execute("SELECT * FROM sa_stock_roster LIMIT 2")
cols = [d[0] for d in cur.description]
for r in cur.fetchall():
    print("  样例: %s" % json.dumps(
        {k: str(v)[:22] for k, v in zip(cols, r)}, ensure_ascii=False))
cur.execute("""SELECT code, name FROM sa_stock_roster
               ORDER BY code LIMIT 5""")
for r in cur.fetchall():
    print("    %s %s" % (r[0], r[1]))
conn.close()
