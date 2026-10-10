# -*- coding: utf-8 -*-
"""cost_estimate 离线断言（纯函数，不联网不连库）。
跑法：python stock-advisor/_cost_estimate_test.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost_estimate as C

N = 0
def ok(cond, label):
    global N
    N += 1
    if not cond:
        print(f"  FAIL #{N}: {label}")

FEE = C.FeeConf()

print("=== 1) 账面摊薄与 calc_position 同公式 ===")
# app.calc_position: avg = (avg*net + price*n) / (net + n)
e = C.estimate(1000, 20.0, 15.0, 1000)
expect = (20.0 * 1000 + 15.0 * 1000) / 2000
ok(abs(e.new_avg_cost_gross - expect) < 1e-9,
   f"账面成本 = {e.new_avg_cost_gross}，期望 {expect}")
ok(e.new_shares_total == 2000, "总股数 2000")
ok(e.cost_delta == round(17.5 - 20.0, 4), f"成本降 2.5：{e.cost_delta}")
ok(abs(e.cost_delta_pct - (-12.5)) < 1e-6, f"降幅 -12.5%：{e.cost_delta_pct}")

print("\n=== 1b) 佣金按「实际成交额」而非我以为的 30000 ===")
# 成交额 = 15 × 1000 = 15000，不是 30000。
# 15000 × 万2.5 = 3.75 < 最低 5 元 → 佣金取 5。第一版断言按 30000 算成 7.5，
# 是我算错了成交额，代码是对的。
ok(e.buy_commission == 5.0,
   f"15000 元成交额 → 佣金取最低 5 元（不是 3.75）：{e.buy_commission}")
ok(abs(e.buy_transfer - 0.15) < 1e-9, f"过户费 = 15000×0.001% = {e.buy_transfer}")
ok(abs(e.buy_fees_total - 5.15) < 1e-6, f"买入费合计 5.15：{e.buy_fees_total}")
ok(abs(e.cash_needed - 15005.15) < 0.01, f"现金需求 15005.15：{e.cash_needed}")
# 反过来：成交额足够大时按费率走，不再受最低 5 元约束
e_big = C.estimate(1000, 20.0, 15.0, 10000)   # 15 万
ok(abs(e_big.buy_commission - 37.5) < 1e-9,
   f"15 万 → 按万2.5 = 37.5（>5，费率生效）：{e_big.buy_commission}")

print("\n=== 2) 真实成本含买入费，且高于账面 ===")
ok(e.new_avg_cost_net > e.new_avg_cost_gross,
   f"含费 {e.new_avg_cost_net} > 账面 {e.new_avg_cost_gross}")
# 30000 元成交额 → 佣金 max(7.5, 5)=7.5，过户费 0.3
f = C.buy_fees(30000, FEE)
ok(abs(f["commission"] - 7.5) < 1e-9, f"佣金 = 30000×万2.5 = {f['commission']}")
ok(abs(f["transfer"] - 0.3) < 1e-9, f"过户费 = 30000×0.001% = {f['transfer']}")
ok(f["stamp_duty"] == 0.0, "买入无印花税")
ok(abs(e.buy_transfer - 0.15) < 1e-9, f"过户费 = 15000×0.001% = {e.buy_transfer}")
ok(abs(e.buy_fees_total - 5.15) < 1e-6, f"买入费合计 5.15：{e.buy_fees_total}")
ok(abs(e.cash_needed - 15005.15) < 0.01, f"现金需求 15005.15：{e.cash_needed}")

print("\n=== 3) 最低佣金 5 元（小额单）===")
f2 = C.buy_fees(2000, FEE)
ok(f2["commission"] == 5.0, f"2000 元按最低 5 元：{f2['commission']}")
ok(abs(f2["commission"] / 2000 - 0.0025) < 1e-9,
   f"占成交额 0.25%：{f2['commission']/2000}")
f3 = C.buy_fees(0, FEE)
ok(f3["total"] == 0.0, "金额 0 → 费用全 0")

print("\n=== 4) 卖出费含印花税；基金免 ===")
s = C.sell_fees(100000, FEE)
ok(abs(s["stamp_duty"] - 50.0) < 1e-9, f"卖出印花税 50：{s['stamp_duty']}")
sf = C.sell_fees(100000, C.FeeConf(is_fund=True))
ok(sf["stamp_duty"] == 0.0, "场内基金免印花税")
sh = C.sell_fees(100000, FEE, market="HK")
ok(sh["stamp_duty"] > 0, "港股卖出也收印花税（与 A 股不同）")
ok(C.buy_fees(100000, FEE, market="HK")["stamp_duty"] > 0,
   "港股买入也收印花税（双向）")

print("\n=== 5) 空仓首建（old_shares=0）===")
e0 = C.estimate(0, 0.0, 10.0, 1000)
ok(e0.new_avg_cost_gross == 10.0, f"空仓建仓成本=买价：{e0.new_avg_cost_gross}")
ok(e0.cost_delta_pct == 0.0, "空仓时 cost_delta_pct 为 0（不除以 0）")
ok(e0.new_avg_cost_net > 10.0, f"含费后回本价 >10：{e0.new_avg_cost_net}")
ok(e0.dilution_ratio == 0.0, "空仓时摊薄比为 0")

print("\n=== 6) 不买入的边界 ===")
z = C.estimate(1000, 20.0, 15.0, 0)
ok(z.new_shares_total == 1000, "买 0 股 → 持仓不变")
ok(z.new_avg_cost_gross == 20.0, "买 0 股 → 成本不变")
ok(z.buy_fees_total == 0.0, "买 0 股 → 无费用")
ok(any("无法预估" in l or "未指定" in l for l in C.render_lines(z)),
   "渲染明说未指定股数")

print("\n=== 7) 零持仓零成本（不能崩）===")
n = C.estimate(0, 0.0, 0.0, 0)
ok(n.new_shares_total == 0, "全 0 不崩")
ok(n.new_avg_cost_gross == 0.0, "全 0 成本 0")
ok(isinstance(C.render_lines(n), list) and C.render_lines(n),
   "全 0 也能渲染")

print("\n=== 8) 价格更高时成本被抬高（方向不能反）===")
up = C.estimate(1000, 20.0, 25.0, 1000)
ok(up.new_avg_cost_gross == 22.5, f"买贵了成本升到 22.5：{up.new_avg_cost_gross}")
ok(up.cost_delta > 0, f"成本升：{up.cost_delta}")
ok(up.cost_delta_pct > 0, f"升幅为正：{up.cost_delta_pct}")

print("\n=== 9) 加仓量对降幅的单调性 ===")
prev = None
monotone = True
for n_buy in (100, 500, 1000, 2000, 5000):
    r = C.estimate(1000, 20.0, 15.0, n_buy)
    if prev is not None and not (r.new_avg_cost_gross < prev):
        monotone = False
    prev = r.new_avg_cost_gross
ok(monotone, "买得越多，成本降得越低（单调递减）")
# 数学上限：无限量加仓趋近 15
big = C.estimate(1000, 20.0, 15.0, 10_000_000)
ok(big.new_avg_cost_gross < 15.001,
   f"无限加仓趋近买价 15：{big.new_avg_cost_gross}")
ok(big.new_avg_cost_gross > 15.0, "永远达不到买价（除非无限量）")

print("\n=== 10) 回本涨幅（含卖出费，必须 > 0）===")
b = C.estimate(1000, 20.0, 15.0, 1000, current_price=16.0)
ok(b.break_even_from_now > 0, f"现价 16 低于回本价 → 需上涨：{b.break_even_from_now}")
# 现价已高于回本价 → 应为负
b2 = C.estimate(1000, 20.0, 15.0, 1000, current_price=19.0)
ok(b2.break_even_from_now < 0, f"现价 19 高于回本价 → 已盈利：{b2.break_even_from_now}")
ok(b.exit_cost_at_new_avg > 0, "给出未来卖出费用")

print("\n=== 11) 回本价闭式校验（不动点迭代收敛）===")
fee = FEE
shares, total_cost = 2000, 20.0 * 1000 + 15.0 * 1000 + 5.15
g = C._gross_price_for_net(total_cost, shares, fee, "A")
net = g * shares - C.sell_fees(g * shares, fee, "A")["total"]
ok(abs(net - total_cost) < 0.01,
   f"解出的 gross={g:.6f} → 净收入 {net:.4f} ≈ 总投入 {total_cost:.4f}")
ok(g > total_cost / shares, "回本 gross 必须高于简单均价（卖出费使然）")

print("\n=== 12) 小额分批：最低佣金吃掉降幅 ===")
# ⚠️ 第一版这里的反例**不成立**，是我参数选错了：
#   分 5 笔 × 2000 元：每笔 max(2000×万2.5=2.5, 最低 5) = 5 → 共 25
#   1 笔 × 10000 元：max(10000×万2.5=25, 最低 5) = 25
#   两者佣金恰好相同，过户费也相同（都是总额×0.001%），净成本一模一样。
#   要构造真正的反例，得让**笔数×最低佣金 > 单笔费率佣金**，即单笔金额小、
#   笔数多。用 10 笔 × 1000 元：每笔 max(2.5, 5) = 5 → 共 50，
#   而 1 笔 10000 元只要 25。
ten = C.simulate([C.Leg(10.0, 100)] * 10, fee=fee)
one_big = C.estimate(0, 0.0, 10.0, 1000, fee=fee)
ok(abs(ten[-1].new_avg_cost_gross - one_big.new_avg_cost_gross) < 1e-9,
   "账面成本：分 10 笔 = 1 笔（都 10.0）—— 账面不受笔数影响")
ten_fees = sum(s.buy_fees_total for s in ten)
ok(abs(ten_fees - one_big.buy_fees_total) > 0.001,
   f"真实费用：10 笔({ten_fees:.4f}) > 1 笔({one_big.buy_fees_total:.4f})"
   " —— 最低佣金按笔收")
ok(abs(one_big.buy_fees_total - (5.0 + 0.1)) < 0.01,
   f"1 笔 10000 元：max(2.5, 最低5)=5 + 过户费 0.1 = {one_big.buy_fees_total}")
ok(abs(ten_fees - (50.0 + 0.1)) < 0.01,
   f"10 笔各 1000 元：佣金 10×5=50 + 过户费 0.1 = {ten_fees}")
ok(ten[-1].new_avg_cost_net > one_big.new_avg_cost_net,
   f"净成本：10 笔({ten[-1].new_avg_cost_net}) > 1 笔({one_big.new_avg_cost_net})")
ok(round(one_big.new_avg_cost_net - 10.0, 4) == round((5.0 + 0.1) / 1000, 4),
   f"1 笔含费回本价 = 10 + 5.1/1000 = {one_big.new_avg_cost_net}")
ok(round(ten[-1].new_avg_cost_net - 10.0, 4) == round((50.0 + 0.1) / 1000, 4),
   f"10 笔含费回本价 = 10 + 50.1/1000 = {ten[-1].new_avg_cost_net}")

print("\n=== 12c) 历史费用不能被逐轮忘掉（真实 bug 回归）===")
# simulate() 早期版本每步把「不含费的账面成本」当下一轮基数，
# 50 元佣金被反复摊薄后收敛到 10.005（正确值 10.0501），即 90% 的费用
# 凭空消失。现在的实现每步传递 total_cash_paid，必须单调上升地收敛。
prev_net, mono_net = 0.0, True
for k in (1, 2, 4, 10, 20):
    legs = [C.Leg(10.0, 1000 // k)] * k
    net = C.simulate(legs, fee=fee)[-1].new_avg_cost_net
    if k == 1:
        prev_net = net
    else:
        if net < prev_net - 1e-9:      # 笔数越多费用越高 → 净成本单调升
            mono_net = False
        prev_net = net
ok(mono_net, "同样 1000 股：笔数越多净成本越高（费用没被摊掉）")
last20 = C.simulate([C.Leg(10.0, 50)] * 20, fee=fee)[-1]
ok(last20.new_avg_cost_net > C.simulate([C.Leg(10.0, 100)] * 10,
                                        fee=fee)[-1].new_avg_cost_net,
   f"20 笔({last20.new_avg_cost_net}) > 10 笔"
   f"({C.simulate([C.Leg(10.0,100)]*10, fee=fee)[-1].new_avg_cost_net})")
ok(last20.new_shares_total == 1000, "20 笔各 50 股 = 1000 股")
ok(abs(last20.new_avg_cost_gross - 10.0) < 1e-9, "账面成本仍是 10.0")
# 容差说明：20 次浮点累加的误差量级 ~1e-13，但打印成 100.100000 是因为
# 浮点表示（真实值 100.09999999999999...）。取 1e-9 —— 足够严到能抓住
# 「round 到 2 位逐笔丢 0.005 → 累计少 0.1」这个真实 bug（差 0.1 >> 1e-9），
# 又不会被浮点表示噪声误伤。
# 20 笔各 50 股 @10 元 → 每笔成交额 500 元，总成交额 10000 元
# 佣金：每笔 max(500×万2.5=0.125, 最低 5) = 5 → 共 100
# 过户费：10000 × 0.001% = 0.1
ok(abs(last20.total_fees_paid - (20 * 5 + 10000 * 0.00001)) < 1e-9,
   f"累计费用 = 20×5 + 10000×0.001% = {last20.total_fees_paid!r}")
# 精度回归：total_cash_paid 内部若 round 到 2 位，20 笔会逐笔丢 0.005，
# 累计少 0.1（第一版实测 100.07 / 10100.07，正确值 100.1 / 10100.1）
ok(abs(last20.total_cash_paid - 10100.1) < 1e-9,
   f"累计现金 = 20×505.005 = 10100.1：{last20.total_cash_paid!r}")

print("\n=== 12d) old_cash_paid 显式传入时历史费用被计入 ===")
# 起始持仓账面成本 10.0 但实际花了 10050（含 50 元历史费用）
wit = C.estimate(100, 10.0, 9.0, 100, old_cash_paid=10050.0, fee=fee)
wo = C.estimate(100, 10.0, 9.0, 100, fee=fee)      # 不传 = 假设无历史费用
ok(wit.new_avg_cost_gross == wo.new_avg_cost_gross,
   "账面成本与历史费用无关")
ok(wit.new_avg_cost_net > wo.new_avg_cost_net,
   f"显式传入时净成本更高({wit.new_avg_cost_net} > {wo.new_avg_cost_net})")
# total_fees_paid = 历史已付(10050-1000=9050) + 本次
ok(abs(wit.total_fees_paid - (9050.0 + wit.buy_fees_total)) < 1e-9,
   f"累计费用 = 历史 9050 + 本次 {wit.buy_fees_total} = {wit.total_fees_paid!r}")
# 自洽恒等式：现金 = 账面成本×股数 + 累计费用
ok(abs(wit.total_cash_paid - (wit.new_avg_cost_gross * wit.new_shares_total
                              + wit.total_fees_paid)) < 1e-9,
   f"恒等式成立：{wit.total_cash_paid!r} = "
   f"{wit.new_avg_cost_gross * wit.new_shares_total!r} + {wit.total_fees_paid!r}")

print("\n=== 12e) old_cash_paid < 账面价值（脏输入）不产生负费用 ===")
dirty = C.estimate(100, 10.0, 9.0, 100, old_cash_paid=500.0, fee=fee)
ok(dirty.total_fees_paid >= 0.0,
   f"脏输入下 total_fees_paid 不为负：{dirty.total_fees_paid}")

print("\n=== 12b) 笔数越多、每笔越小，费用越高（单调）===")
prev_fee, mono = -1.0, True
for k in (1, 2, 5, 10, 20):
    shares_each = 1000 // k
    if shares_each <= 0:
        continue
    legs = [C.Leg(10.0, shares_each)] * k
    last = C.simulate(legs, fee=fee)[-1]
    if last.buy_fees_total < 0 and prev_fee >= 0:
        pass
    if k == 1:
        prev_fee = C.estimate(0, 0.0, 10.0, 1000, fee=fee).buy_fees_total
    else:
        tot = sum(s.buy_fees_total for s in C.simulate(legs, fee=fee))
        if tot < prev_fee - 1e-9:
            mono = False
        prev_fee = tot
ok(mono, "同样 1000 股，分的笔数越多累计费用不降")

print("\n=== 13) simulate 逐步 ===")
steps = C.simulate([C.Leg(15.0, 500), C.Leg(14.0, 500), C.Leg(13.0, 500)],
                   start_shares=1000, start_avg_cost=20.0)
ok(len(steps) == 3, "3 笔 → 3 步")
ok(steps[0].new_shares_total == 1500, f"第1步后 1500：{steps[0].new_shares_total}")
ok(steps[1].new_shares_total == 2000, f"第2步后 2000")
ok(steps[2].new_shares_total == 2500, f"第3步后 2500")
ok(steps[0].new_avg_cost_gross > steps[1].new_avg_cost_gross
   > steps[2].new_avg_cost_gross, "越买越便宜")

print("\n=== 14) ladder 预算档位（按 100 股整手向下取整）===")
rows = C.ladder(1000, 20.0, 15.0, [1000, 5000, 50000])
ok(len(rows) == 3, "3 档")
ok(rows[0]["shares"] == 0, f"1000 元买不起 100 股（需 1500）：{rows[0]['shares']}")
ok(rows[0]["note"] == "预算不足一手", f"给了说明：{rows[0]['note']}")
ok(rows[1]["shares"] == 300, f"5000 元 → 300 股：{rows[1]['shares']}")
ok(rows[2]["shares"] == 3300, f"50000 元 → 3300 股：{rows[2]['shares']}")
ok(all(r["shares"] % 100 == 0 for r in rows), "都是 100 的整数倍")
# 档位是累计的：第 2 档的 avg_after 建立在第 1 档基础上
ok(rows[2]["avg_after"] < rows[1]["avg_after"], "档位递增 → 成本递减")
# 边界：预算刚好够一手
edge = C.ladder(0, 0.0, 10.0, [1000])
ok(edge[0]["shares"] == 100, f"1000 元买 10 元票 = 100 股：{edge[0]['shares']}")

print("\n=== 15) 渲染层不能因脏数据崩（AGENTS.md 纪律）===")
for bad in (C.estimate(0, 0.0, 0.0, 0),
            C.estimate(1000, 0.0, 15.0, 100),
            C.estimate(1000, 20.0, 0.0, 100),
            C.estimate(-5, -1.0, -2.0, -3)):
    try:
        L = C.render_lines(bad)
        ok(isinstance(L, list) and L, "脏输入也能渲染")
    except Exception as exc:
        ok(False, f"脏输入崩了：{type(exc).__name__}: {exc}")

print("\n=== 16) 精度：结果已 round，不产生 17.499999 ===")
# 第一版这里写的是 expect 7.0，实际 (3*10 + 7*5)/10 = 65/10 = 6.5 —— 我算错了。
p = C.estimate(3, 10.0, 5.0, 7)
ok(abs(p.new_avg_cost_gross - 6.5) < 1e-9,
   f"(3×10 + 7×5)/10 = 6.5：{p.new_avg_cost_gross}")
# 用一个会产生浮点毛刺的组合，确认输出已 round
q = C.estimate(1000, 20.0, 15.0, 333)
ok(q.new_avg_cost_gross == round(q.new_avg_cost_gross, 4),
   f"账面成本 round 到 4 位：{q.new_avg_cost_gross}")
ok(q.new_avg_cost_net == round(q.new_avg_cost_net, 4),
   f"净成本 round 到 4 位：{q.new_avg_cost_net}")
# 三分之一这类不可整除的情况
r = C.estimate(1, 10.0, 11.0, 1)
ok(abs(r.new_avg_cost_gross - 10.5) < 1e-9, f"(10+11)/2 = 10.5：{r.new_avg_cost_gross}")
s = C.estimate(2, 3.33, 3.34, 1)
ok(isinstance(s.new_avg_cost_gross, float), "三分位也返回 float")
ok(len(str(s.new_avg_cost_gross).split(".")[-1]) <= 4,
   f"小数位不超过 4：{s.new_avg_cost_gross}")

print("\n=== 17) 渲染层回归：股数不能被 float 打回破折号 ===")
# 2026-10-06 实测踩中：接口层把股数传成 float(1000.0) 时，
# `format(1000.0, 'd')` 抛 ValueError → 整行渲染成「— 股」，
# 而股数和成本正是这个功能的核心信息。
for shares_val in (1000, 1000.0, "1000", 1000.5):
    est = C.estimate(shares_val, 20.0, 15.0, shares_val)
    lines = C.render_lines(est)
    ok(all("\u2014 股" not in l for l in lines),
       f"shares={shares_val!r} 渲染出股数而非破折号")
# 直接验渲染文本里有真实数字
txt = "\n".join(C.render_lines(C.estimate(1000, 20.0, 15.0, 1000)))
ok("1000 股" in txt or "2000 股" in txt, f"渲染含真实股数：{txt.splitlines()[0]}")
ok("\u2014" not in txt, f"正常输入不产生破折号：{txt.splitlines()[0]}")
# float 股数应四舍五入到整数展示
txt2 = "\n".join(C.render_lines(C.estimate(1000.4, 20.0, 15.0, 1000.4)))
ok("1000 股" in txt2 or "2000 股" in txt2, f"小数股数取整展示：{txt2.splitlines()[0]}")

print("\n=== 18) 金额输出不得泄漏浮点噪声 ===")
# 2026-10-06 实测踩中：ladder 输出 total_fees_paid=10.040000000000145
rows = C.ladder(1000, 20.0, 15.0, [8000, 30000])
for r in rows:
    if not r["shares"]:
        continue
    fee_txt = repr(r["total_fees_paid"])
    ok(len(fee_txt.split(".")[-1]) <= 4 if "." in fee_txt else True,
       f"total_fees_paid 已收敛：{fee_txt}")
    ok(r["total_fees_paid"] == round(r["total_fees_paid"], 2),
       f"精确到分：{r['total_fees_paid']!r}")
    ok(r["cash_needed"] == round(r["cash_needed"], 2),
       f"cash_needed 精确到分：{r['cash_needed']!r}")

print("\n=== 19) to_dict 对外收敛精度 ===")
d = C.estimate(0, 0.0, 10.0, 1000).to_dict()
for k in ("total_cash_paid", "total_fees_paid", "cash_needed",
          "exit_cost_at_new_avg"):
    v = d[k]
    ok(v == round(v, 2), f"to_dict[{k}] = {v!r} 已 round 到分")
ok(isinstance(d["fee"], dict) and "commission_rate" in d["fee"],
   "to_dict 含 fee 子结构")

print(f"\n=== 共 {N} 条 ===")