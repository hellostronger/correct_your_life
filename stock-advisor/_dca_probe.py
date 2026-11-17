"""反事实：11-10 那笔 51.3 万港元若改成「每天最多买 10 万」分批买入，今天值多少。

只读：101 行情（日K收盘/现价/汇率）+ 本机 /api/holdings，不写库不改文件。
跑法：D:\\Python312\\python.exe _dca_probe.py
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

SVC = "http://101.43.25.101:8080"
API = "http://127.0.0.1:8686"
CAP = 2940 * 174.6          # 513,324 HKD，11-10 实际一次性投入
ANCHOR_PX = 174.6
DAYS = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
LOT = 100                   # 00100 流水全是 100 股整数倍


def get(base, path, params):
    url = base + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


ks = get(SVC, "/kline", {"secid": "116.00100", "days": 60,
                         "fields": "f51,f52,f53,f54,f55", "source": "auto"})["klines"]
closes = {}
for k in ks:
    p = k.split(",")
    if len(p) >= 3 and p[0] in DAYS:
        closes[p[0]] = float(p[2])
closes["2026-11-10"] = ANCHOR_PX   # 首日沿用实际成交价，不给自己挑更便宜的收盘价
q = get(SVC, "/quote", {"symbols": "hk00100,whHKDCNY"})["quotes"]
now = float(q["hk00100"]["price"])
fx = float(q["whHKDCNY"]["price"])
print(f"现价 {now} HKD / 汇率 {fx} / 本金 {CAP:,.0f} HKD / 每天上限 100,000 HKD")
print("买入日价格：", closes)

# 一次性全买（基准）
one_shares = 2940
one_value = one_shares * now
print(f"\n[A] 实际一次性：2940 股 @{ANCHOR_PX}，现值 {one_value:,.0f} HKD，"
      f"盈亏 {one_value-CAP:+,.0f} HKD（{(one_value/CAP-1)*100:+.2f}%）")


def dca(daily_cap, lot=None, price_map=None):
    price_map = price_map or closes
    cash, shares, rows = CAP, 0, []
    for d in DAYS:
        if cash <= 0:
            break
        px = price_map[d]
        budget = min(daily_cap, cash)
        n = budget / px
        if lot:
            n = int(n // lot) * lot
        cost = n * px
        cash -= cost
        shares += n
        rows.append((d, px, n, cost, cash))
    value = shares * now + cash
    avg = (CAP - cash) / shares if shares else 0
    return rows, shares, cash, value, avg


for label, cap, lot in (("10万 HKD/天 · 整手100股", 100000, LOT),
                        ("10万 HKD/天 · 不整手", 100000, None),
                        ("10万 CNY/天 · 整手100股", 100000 / fx, LOT)):
    rows, shares, cash, value, avg = dca(cap, lot)
    print(f"\n[B] {label}")
    for d, px, n, cost, left in rows:
        print(f"   {d} @{px:>7.1f} 买 {n:>8.1f} 股  花 {cost:>9,.0f} HKD  剩现金 {left:>9,.0f}")
    print(f"   合计 {shares:,.1f} 股，均价 {avg:.2f} HKD，剩余现金 {cash:,.0f} HKD")
    print(f"   今天资产 = 市值 {shares*now:,.0f} + 现金 {cash:,.0f} = {value:,.0f} HKD"
          f"（盈亏 {value-CAP:+,.0f} HKD / {(value/CAP-1)*100:+.2f}%）")
    print(f"   vs 一次性 差额 {value-one_value:+,.0f} HKD = {(value-one_value)*fx:+,.0f} CNY")

# 账户总价值（静态替代口径）
rows, shares, cash, value, avg = dca(100000, LOT)
h = get(API, "/api/holdings", {})
held = [x for x in h if x.get("net_shares", 0) > 0]
mv_cny = sum(x.get("market_value_cny") or 0 for x in held)
gain_cny = (value - one_value) * fx
print(f"\n当前系统持仓市值 ¥{mv_cny:,.0f}（{len(held)} 只）")
print(f"静态替代估算：账户约 ¥{mv_cny + gain_cny:,.0f}（原 ¥{mv_cny:,.0f} + 少亏 ¥{gain_cny:,.0f}）")
print("注：只替换这一笔的买入节奏，其余持仓按现值不变；后续做T的持股数会变，"
      "因此不是完整交易回放，不可用来说「精确账户余额」。")
