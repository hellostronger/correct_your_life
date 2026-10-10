# -*- coding: utf-8 -*-
"""端到端：真打 HTTP 到已在跑的服务（8686）。

⚠️ 服务是 2026-10-07 19:01 起的，跑的是**改动前**的代码，所以本脚本先
打一次看是不是 404；若是 404 说明新端点还没被加载，需要重启服务后再跑。
跑法：python stock-advisor/_cost_e2e.py
"""
import json
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8686"
OUT = []
def p(*a): OUT.append(" ".join(str(x) for x in a))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _as_dict(body):
    """非 dict 响应（错误字符串）转成可安全取值的 dict，别让断言层崩。"""
    return body if isinstance(body, dict) else {"_raw": body}


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")[:200]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


p("=" * 72)
p("0) 服务是否加载了新端点")
st, body = post("/api/cost-estimate",
                {"price": 15.0, "shares": 1000, "old_shares": 1000,
                 "old_avg_cost": 20.0, "current_price": 16.5})
p(f"  POST /api/cost-estimate → HTTP {st}")
Path(r"C:\Users\Strong\AppData\Local\Temp\opencode\cost_http.txt").write_text(
    "\n".join(OUT), encoding="utf-8")
if st == 404:
    p("  ⚠ 服务跑的是旧代码，新端点未加载 —— 需重启后重跑本脚本")
    raise SystemExit(1)
if st != 200:
    p(f"  响应：{body}")
    raise SystemExit(1)
body = _as_dict(body)

p("\n" + "=" * 72)
p("1) 显式给持仓")
d = body
p(f"  账面成本 {d['old_avg_cost']} → {d['new_avg_cost_gross']} "
   f"({d['cost_delta']:+}, {d['cost_delta_pct']:+}%)")
p(f"  真实回本价 {d['new_avg_cost_net']}，累计费用 {d['total_fees_paid']}")
p(f"  现金需求 {d['cash_needed']}，现价 {d['current_price']} ({d['price_source']})")
p(f"  回本需涨 {d['break_even_from_now']:+}%")
for l in d.get("lines", []):
    p("   ", l)
for cv in d.get("caveats", []):
    p("    ⚠", cv)

p("\n" + "=" * 72)
p("2) 走账本：真实持仓 + 现价 + 历史流水回放")
st, holds = get("/api/holdings")
p(f"  GET /api/holdings → HTTP {st}, {len(holds) if isinstance(holds, list) else holds} 只")
if isinstance(holds, list) and holds:
    h = holds[0]
    p(f"  取 {h['code']} {h.get('name','')}：{h['net_shares']} 股 @ {h['avg_cost']}"
       f" 现价 {h.get('price')}")
    st2, d2 = post("/api/cost-estimate", {
        "code": h["code"], "price": round(float(h["price"] or h["avg_cost"]) * 0.9, 3),
        "shares": 100})
    p(f"  POST → HTTP {st2}")
    d2 = _as_dict(d2)
    if st2 == 200:
        p(f"  账面 {d2['old_avg_cost']} → {d2['new_avg_cost_gross']}"
           f" ({d2['cost_delta_pct']:+}%)")
        p(f"  现价 {d2['current_price']} ({d2['price_source']})")
        p(f"  累计费用 {d2['total_fees_paid']}")
        p(f"  回本需涨 {d2['break_even_from_now']:+}%")

p("\n" + "=" * 72)
p("3) 预算档位计划")
st3, d3 = post("/api/cost-plan", {
    "price": 15.0, "old_shares": 1000, "old_avg_cost": 20.0,
    "budgets": [2000, 8000, 30000]})
d3 = _as_dict(d3)
p(f"  HTTP {st3}, mode={d3.get('mode')}")
if st3 != 200:
    p(f"  ⚠ 响应：{d3.get('_raw')}")
for row in d3.get("rows", []):
    p(f"   预算 {row['budget']:>8} → {row['shares']:>5} 股  "
       f"账面 {row['avg_after']:.4f}  真实 {row['avg_after_net']:.4f}  "
       f"费用 {row.get('total_fees_paid', 0)}  {row['note']}")

p("\n" + "=" * 72)
p("4) 分批 legs：费用必须按笔累积")
st4, d4 = post("/api/cost-plan", {"legs": [{"price": 10.0, "shares": 100}] * 10})
d4 = _as_dict(d4)
p(f"  HTTP {st4}, {len(d4.get('steps', []))} 步")
if st4 != 200:
    p(f"  ⚠ 响应：{d4.get('_raw')}")
fin = d4.get("final") or {}
p(f"  账面成本 {fin.get('new_avg_cost_gross')}（不受笔数影响）")
p(f"  真实回本价 {fin.get('new_avg_cost_net')}，累计费用 {fin.get('total_fees_paid')}")

p("\n" + "=" * 72)
p("5) 参数校验")
for b, label in [({"price": 0, "shares": 100}, "price=0"),
                 ({"price": 15.0, "shares": 0}, "shares=0"),
                 ({"price": -5.0, "shares": 100}, "price 负数"),
                 ({"price": 15.0, "shares": 100, "old_shares": -1}, "old_shares 负数")]:
    stx, _ = post("/api/cost-estimate", b)
    p(f"  {label:20s} → HTTP {stx} {'✓已拒绝' if stx == 422 else '⚠未拒绝!'}")

p("\n" + "=" * 72)
p("6) 不存在的 code")
st6, d6 = post("/api/cost-estimate", {"code": "999999", "price": 10.0, "shares": 100})
d6 = _as_dict(d6)
p(f"  HTTP {st6}, code={d6.get('code')}, 账面 {d6.get('new_avg_cost_gross')}"
   f"（空仓建仓应=买价 10）")

Path(r"C:\Users\Strong\AppData\Local\Temp\opencode\cost_http.txt").write_text(
    "\n".join(OUT), encoding="utf-8")
print("ok")