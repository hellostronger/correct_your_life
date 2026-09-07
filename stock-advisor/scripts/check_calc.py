"""单元验证 calc_position（平均成本法）：
复现用户场景 —— 1 号买入一笔、2 号卖出一笔、3 号买入一笔，核对累计盈亏。

场景：1 号买 100 股 @10，2 号卖 50 股 @12，3 号买 50 股 @9，现价 11。
  摊薄成本   = (10*50 + 9*50) / 100 = 9.50
  已实现盈亏 = (12 - 10) * 50 = +100
  浮动盈亏   = (11 - 9.5) * 100 = +150
  累计总盈亏 = 100 + 150 = +250
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import calc_position

trades = [
    {"id": 1, "trade_date": "2026-09-01", "side": "buy",  "shares": 100, "price": 10},
    {"id": 2, "trade_date": "2026-09-02", "side": "sell", "shares": 50,  "price": 12},
    {"id": 3, "trade_date": "2026-09-03", "side": "buy",  "shares": 50,  "price": 9},
]
pos = calc_position(trades)
print("回放结果:", pos)

assert pos["net_shares"] == 100, "持股应为 100"
assert abs(pos["avg_cost"] - 9.5) < 1e-6, "摊薄成本应为 9.50"
assert abs(pos["realized_pnl"] - 100) < 1e-6, "已实现盈亏应为 +100"
assert abs(pos["total_buy"] - 1450) < 1e-6, "累计买入金额应为 1450"
assert abs(pos["total_sell"] - 600) < 1e-6, "累计卖出金额应为 600"

float_pnl = (11 - pos["avg_cost"]) * pos["net_shares"]      # +150
assert abs(float_pnl - 150) < 1e-6
total_pnl = pos["realized_pnl"] + float_pnl                 # +250
assert abs(total_pnl - 250) < 1e-6

# 超卖拦截
try:
    calc_position(trades + [{"id": 4, "trade_date": "2026-09-04",
                             "side": "sell", "shares": 999, "price": 11}])
    raise AssertionError("超卖应抛 ValueError")
except ValueError as e:
    print("超卖拦截 OK:", e)

# 全清仓：realized 保留
all_sold = calc_position([
    {"id": 1, "trade_date": "2026-09-01", "side": "buy",  "shares": 100, "price": 10},
    {"id": 2, "trade_date": "2026-09-02", "side": "sell", "shares": 100, "price": 11},
])
assert all_sold["net_shares"] == 0
assert abs(all_sold["realized_pnl"] - 100) < 1e-6
print("全清仓保留已实现盈亏 OK:", all_sold["realized_pnl"])

# 乱序输入按日期回放
shuffled = [trades[2], trades[0], trades[1]]
assert abs(calc_position(shuffled)["realized_pnl"] - 100) < 1e-6
print("乱序流水回放 OK")

print("\n全部断言通过 ✔  场景合计：已实现 +100，浮动 +150，累计 +250")
