"""一次性排查：00100 MINIMAX 11-10 买 2940@174.6 之后的交易与出逃窗口。只读。"""
from __future__ import annotations

import json
import pathlib
import urllib.parse
import urllib.request
import psycopg2

BASE = pathlib.Path(__file__).resolve().parent
SVC = "http://101.43.25.101:8080"
ANCHOR = "2026-11-10"
ANCHOR_PX = 174.6


def svc(path, params):
    url = SVC + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def db_conf():
    conf = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
            "password": "", "dbname": "postgres"}
    m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
         "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    for line in (BASE.parent / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        if k.strip() in m:
            key = m[k.strip()]
            conf[key] = int(v.strip()) if key == "port" else v.strip()
    return conf


conn = psycopg2.connect(connect_timeout=15, **db_conf())
cur = conn.cursor()
cur.execute("""SELECT id, trade_date, side, shares, price FROM sa_trades
               WHERE code='00100' ORDER BY trade_date, id""")
trades = [{"id": r[0], "date": r[1].isoformat(), "side": r[2],
           "shares": int(r[3]), "price": float(r[4])} for r in cur.fetchall()]
conn.close()

q = svc("/quote", {"symbols": "hk00100"})["quotes"]["hk00100"]
now = float(q["price"])
fx = float(svc("/quote", {"symbols": "whHKDCNY"})["quotes"]["whHKDCNY"]["price"])
print(f"现价 {now} HKD（{q['updated_at']}），当日 high {q['high']} / low {q['low']}，HKD/CNY {fx}")

ks = svc("/kline", {"secid": "116.00100", "days": 60,
                    "fields": "f51,f52,f53,f54,f55", "source": "auto"})["klines"]
print("\n== 11-06 之后日K（date, open, close, high, low） ==")
bars = []
for k in ks:
    p = k.split(",")
    if len(p) >= 5 and p[0] >= "2026-11-06":
        bars.append((p[0], *map(float, p[1:5])))
        print(" ", p[0], p[1], p[2], p[3], p[4])
print("  (11-13 以实时行情为准：close %.1f high %.1f low %.1f)" % (now, q["high"], q["low"]))

# 平均成本法回放
net, avg, realized = 0, 0.0, 0.0
after_sells, after_buys, ledger = [], [], []
for t in trades:
    n, p = t["shares"], t["price"]
    if t["side"] == "buy":
        avg = (avg * net + p * n) / (net + n)
        net += n
        if t["date"] >= ANCHOR:
            after_buys.append((t["date"], p, n, avg, net))
    else:
        r = (p - avg) * n
        realized += r
        net -= n
        ledger.append((t["date"], p, n, avg, r, net))
        if t["date"] >= ANCHOR:
            after_sells.append((t["date"], p, n, avg, r))
    if t["date"] == ANCHOR and t["side"] == "buy" and n == 2940:
        print(f"\n== 这笔买入落地后：持股 {net} 股，摊薄成本 {avg:.2f} HKD ==")

print("\n== 11-10（含）之后每笔卖出：按当时摊薄成本 ==")
for d, p, n, c, r in after_sells:
    print(f"  {d} 卖 {n:>5}@{p:>7g}  当时成本 {c:>7.2f}  已实现 {r:>+11,.0f} HKD  "
          f"相对 174.6 {(p-ANCHOR_PX)*n:>+10,.0f} HKD")
print(f"  合计已实现 {sum(x[4] for x in after_sells):+,.0f} HKD；"
      f"相对 174.6 的差价合计 {sum((x[1]-ANCHOR_PX)*x[2] for x in after_sells):+,.0f} HKD")

print("\n== 11-10 之后买入（摊低成本） ==")
for d, p, n, c, cur_net in after_buys:
    if p == ANCHOR_PX:
        continue
    print(f"  {d} 买 {n:>5}@{p:>7g}  之后成本 {c:>7.2f}  持股 {cur_net}")

print(f"\n== 全历史 00100：已实现 {realized:+,.0f} HKD，当前持股 {net} 股，"
      f"摊薄成本 {avg:.2f}，现价 {now} → 浮动 {(now-avg)*net:+,.0f} HKD，"
      f"总盈亏 {realized+(now-avg)*net:+,.0f} HKD（折 CNY {(realized+(now-avg)*net)*fx:+,.0f}）")

# 出逃窗口
print("\n== 出逃窗口（11-10 买入之后，盘中最高价 > 174.6 的交易日） ==")
for d, o, cl, hi, lo in bars:
    if d <= ANCHOR:
        continue
    c = now if d == "2026-11-13" else cl
    h = max(hi, q["high"] if d == "2026-11-13" else hi)
    mark = "✅ 高于成本" if h > ANCHOR_PX else "❌ 未回到成本"
    print(f"  {d} high {h:>7.1f}  close {c:>7.1f}  {mark}  "
          f"若 2940 股全在 high 卖出 = {h*2940:,.0f} HKD vs 成本 {ANCHOR_PX*2940:,.0f} → {(h-ANCHOR_PX)*2940:+,.0f} HKD")
print(f"\n回本参考：现价 {now} → 需涨到摊薄成本 {avg:.2f}（+{(avg/now-1)*100:.1f}%）；"
      f"那笔 174.6 需涨 +{(ANCHOR_PX/now-1)*100:.1f}%")
