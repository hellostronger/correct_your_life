"""反事实：11-13 卖 00100 1680@153 的钱转买 02513，到 11-13 收盘划不划算。

口径与快照归因一致：每笔相对「不动」的贡献
  买 = 股数×(现价−买价)，卖 = 股数×(卖价−现价)，港股 HKD。
只读 DB 流水 + 101 行情，不写库。
"""
from __future__ import annotations

import json
import pathlib
import urllib.parse
import urllib.request

import psycopg2

BASE = pathlib.Path(__file__).resolve().parent
SVC = "http://101.43.25.101:8080"


def get(path, params):
    url = SVC + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


conf = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
        "password": "", "dbname": "postgres"}
m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
     "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
for line in (BASE.parent / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        if k.strip() in m:
            key = m[k.strip()]
            conf[key] = int(v.strip()) if key == "port" else v.strip()

conn = psycopg2.connect(connect_timeout=15, **conf)
cur = conn.cursor()
cur.execute("""SELECT id, code, trade_date, side, shares, price FROM sa_trades
               WHERE code IN ('00100','02513') AND trade_date >= DATE '2026-11-12'
               ORDER BY trade_date, id""")
rows = [{"id": r[0], "code": r[1], "date": r[2].isoformat(), "side": r[3],
         "shares": int(r[4]), "price": float(r[5])} for r in cur.fetchall()]
conn.close()

q = get("/quote", {"symbols": "hk00100,hk02513,whHKDCNY"})["quotes"]
px = {"00100": float(q["hk00100"]["price"]), "02513": float(q["hk02513"]["price"])}
fx = float(q["whHKDCNY"]["price"])
print(f"11-13 收盘：00100 {px['00100']} / 02513 {px['02513']} HKD，汇率 {fx}")

print("\n== 11-12 / 11-13 两票流水与逐笔贡献（相对不动） ==")
groups = {"2026-11-12": 0.0, "2026-11-13": 0.0}
by_code = {"00100": 0.0, "02513": 0.0}
for t in rows:
    now = px[t["code"]]
    c = t["shares"] * (now - t["price"]) if t["side"] == "buy" else t["shares"] * (t["price"] - now)
    groups[t["date"]] += c
    by_code[t["code"]] += c
    print(f"  {t['date']} {t['code']} {'买' if t['side']=='buy' else '卖'} "
          f"{t['shares']:>5}@{t['price']:>7g}  贡献 {c:>+10,.0f} HKD")

print("\n== 汇总 ==")
for d, v in groups.items():
    print(f"  {d} 合计 {v:>+11,.0f} HKD（折 CNY {v*fx:>+11,.0f}）")
print(f"  11-13 当天换仓合计 {groups['2026-11-13']:>+11,.0f} HKD（{groups['2026-11-13']*fx:+,.0f} CNY）")
print(f"  含 11-12 智谱高位加仓   {sum(groups.values()):>+11,.0f} HKD（{sum(groups.values())*fx:+,.0f} CNY）")
print("  分票：", {k: round(v) for k, v in by_code.items()})

# 资金量核对
sell = [t for t in rows if t["date"] == "2026-11-13" and t["code"] == "00100" and t["side"] == "sell"]
proceeds = sum(t["shares"] * t["price"] for t in sell)
buy13 = [t for t in rows if t["date"] == "2026-11-13" and t["code"] == "02513" and t["side"] == "buy"]
buy12 = [t for t in rows if t["date"] == "2026-11-12" and t["code"] == "02513" and t["side"] == "buy"]
sell13_zp = [t for t in rows if t["date"] == "2026-11-13" and t["code"] == "02513" and t["side"] == "sell"]
a13 = sum(t["shares"] * t["price"] for t in buy13)
a12 = sum(t["shares"] * t["price"] for t in buy12)
s13 = sum(t["shares"] * t["price"] for t in sell13_zp)
print(f"\n== 资金量核对 ==")
print(f"  11-13 卖 00100 1680@153 回款        {proceeds:>10,.0f} HKD")
print(f"  11-13 买 02513 400 股              {a13:>10,.0f} HKD（同日卖 100 股回款 {s13:,.0f}，净买 {a13-s13:,.0f}）")
print(f"  11-12 买 02513 200 股（在卖出之前） {a12:>10,.0f} HKD")
print(f"  两天买智谱合计 {a12+a13:,.0f} HKD，与回款 {proceeds:,.0f} 相差 {proceeds-a12-a13:+,.0f} HKD")

# 不换仓对照
keep = 1680 * px["00100"]
print(f"\n== 不换仓对照 ==")
print(f"  若 1680 股 00100 留到收盘：{keep:,.0f} HKD；实际卖出回款 {proceeds:,.0f} → 卖价高 0.6/股，多留 {proceeds-keep:+,.0f} HKD")
print(f"  11-13 换仓净贡献 {groups['2026-11-13']:+,.0f} HKD ≈ 不换仓基础上多出这些（逐笔贡献口径）")
print("\n注：这些是「相对不动」的贡献，不能冲销 00100 已实现 −24,728 HKD 的会计亏损；"
      "且只是 11-13 收盘快照，智谱当天从 461 跌到 408.4，397~406 的买入浮盈很薄。")
