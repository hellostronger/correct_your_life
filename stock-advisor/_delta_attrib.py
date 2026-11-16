"""一次性归因：某份快照起「实际操作比不动少赚」是哪些交易造成的。

只读：sa_trades 流水 + 8686 快照报告（现价/汇率），不写库、不改任何文件。
跑法：D:\\Python312\\python.exe _delta_attrib.py 2026-09-10
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request
from collections import defaultdict

import psycopg2
import trade_snapshot as TS

DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-10"
BASE = pathlib.Path(__file__).resolve().parent
API = "http://127.0.0.1:8686"


def load_db_conf():
    conf = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
            "password": "", "dbname": "postgres"}
    mapping = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
               "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    for line in (BASE.parent / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        if k.strip() in mapping:
            key = mapping[k.strip()]
            conf[key] = int(v.strip()) if key == "port" else v.strip()
    return conf


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


conn = psycopg2.connect(connect_timeout=15, **load_db_conf())
cur = conn.cursor()
cur.execute("""SELECT id, code, trade_date, side, shares, price, note
               FROM sa_trades ORDER BY trade_date, id""")
trades = [{"id": i, "code": c, "trade_date": d.isoformat(), "side": s,
           "shares": int(n), "price": float(p), "note": note or ""}
          for i, c, d, s, n, p, note in cur.fetchall()]
cur.execute("SELECT code, name FROM sa_watchlist")
names = dict(cur.fetchall())
conn.close()

rep = get(f"{API}/api/trade-snapshots/report?date={DATE}")["report"]
fx = rep["fx"]
prices = {r["code"]: r["price_now"] for r in rep["by_code"] if r["price_now"]}
after = [t for t in trades if t["trade_date"] > DATE]
sc = TS.score_trades(after, prices, fx, names)
rows = [x for x in sc["trades"] if x["priced"]]
total = sum(x["contribution_cny"] for x in rows)

print(f"=== 基准 {DATE} → 现在 | fx={fx} | 区间 {len(after)} 笔 | 现价覆盖 {len(prices)} 只 ===")
print(f"操作差额(实际-不动) = {total:+,.0f} CNY   (报告 delta {rep['totals']['delta_cny']:+,.0f})")
print(f"  买入端 {sc['win_rate']['buy']['contribution_cny']:+,.0f}  "
      f"卖出端 {sc['win_rate']['sell']['contribution_cny']:+,.0f}")
print(f"  择时胜率 {sc['win_rate']['win_rate']}%  ({sc['win_rate']['wins']}胜/{sc['win_rate']['losses']}负)")

print("\n=== 按股票归因（净贡献，负=拖累） ===")
by_code = defaultdict(lambda: {"v": 0.0, "n": 0, "buy": 0.0, "sell": 0.0, "shares_b": 0, "shares_s": 0})
for x in rows:
    d = by_code[x["code"]]
    d["v"] += x["contribution_cny"]
    d["n"] += 1
    d["buy" if x["side"] == "buy" else "sell"] += x["contribution_cny"]
    d["shares_b" if x["side"] == "buy" else "shares_s"] += x["shares"]
for c, d in sorted(by_code.items(), key=lambda kv: kv[1]["v"]):
    print(f"{c} {names.get(c,c):<12} 净 {d['v']:>+12,.0f}  ({d['n']:>2}笔) "
          f"买 {d['buy']:>+11,.0f} 卖 {d['sell']:>+11,.0f} "
          f"买{d['shares_b']}股/卖{d['shares_s']}股")

print("\n=== 最伤的 12 笔 ===")
for x in sorted(rows, key=lambda x: x["contribution_cny"])[:12]:
    tag = "卖飞" if x["side"] == "sell" and x["price"] < x["price_now"] else (
        "买贵" if x["side"] == "buy" and x["price"] > x["price_now"] else "")
    print(f"{x['date']} {x['code']} {names.get(x['code'],x['code']):<10} "
          f"{'买' if x['side']=='buy' else '卖':>1}{x['shares']:>6}@{x['price']:>8g} "
          f"现价{x['price_now']:>8g}  贡献 {x['contribution_cny']:>+11,.0f} {tag}")

print("\n=== 最赚的 8 笔 ===")
for x in sorted(rows, key=lambda x: -x["contribution_cny"])[:8]:
    print(f"{x['date']} {x['code']} {names.get(x['code'],x['code']):<10} "
          f"{'买' if x['side']=='buy' else '卖':>1}{x['shares']:>6}@{x['price']:>8g} "
          f"现价{x['price_now']:>8g}  贡献 {x['contribution_cny']:>+11,.0f}")

print("\n=== 按交易日 ===")
by_day = defaultdict(float)
for x in rows:
    by_day[x["date"]] += x["contribution_cny"]
for d, v in sorted(by_day.items()):
    print(f"{d}  {v:>+12,.0f}")

print("\n=== 分类 ===")
sold_fly = [(x, x["contribution_cny"]) for x in rows if x["side"] == "sell" and x["win"] is False]
bought_high = [(x, x["contribution_cny"]) for x in rows if x["side"] == "buy" and x["win"] is False]
sell_good = sum(x["contribution_cny"] for x in rows if x["side"] == "sell" and x["win"] is True)
buy_good = sum(x["contribution_cny"] for x in rows if x["side"] == "buy" and x["win"] is True)
print(f"卖飞(卖完还涨) {len(sold_fly):>2}笔 合计 {sum(v for _,v in sold_fly):>+12,.0f}")
print(f"买贵(买完就跌) {len(bought_high):>2}笔 合计 {sum(v for _,v in bought_high):>+12,.0f}")
print(f"卖对(躲跌)     合计 {sell_good:>+12,.0f}")
print(f"买对(买完就涨) 合计 {buy_good:>+12,.0f}")

worst = sorted(rows, key=lambda x: x["contribution_cny"])[:5]
print(f"\n剔除最伤的 3 笔后差额 = {total - sum(x['contribution_cny'] for x in worst[:3]):+,.0f}")
print(f"剔除最伤的 5 笔后差额 = {total - sum(x['contribution_cny'] for x in worst):+,.0f}")
print(f"剔除全部新建仓票(基准日无持仓)的贡献后 = "
      f"{total - sum(x['contribution_cny'] for x in rows if x['code'] not in {r['code'] for r in rep['by_code'] if r['shares_base']>0}):+,.0f}")
