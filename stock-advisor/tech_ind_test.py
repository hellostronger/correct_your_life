"""tech_ind（RSI）离线测试：不联网、不连库、不 import app。

跑法：D:\\Python312\\python.exe tech_ind_test.py
"""
from __future__ import annotations

from pathlib import Path

import tech_ind as TI

BASE = Path(__file__).resolve().parent
N = 0


def ck(cond, label):
    global N
    N += 1
    if not cond:
        raise AssertionError(f"#{N} {label}")


# ---------------- Wilder RSI 数值（手工验算 period=3） ----------------
# closes [1,2,3,2]：变化 +1,+1,−1 → avg_gain=2/3, avg_loss=1/3 → RS=2 → RSI≈66.67
s = TI.rsi_series([1, 2, 3, 2], period=3)
ck(s[:3] == [None, None, None], "预热位全 None（period 位不足不编造）")
ck(s[3] == 66.67, f"Wilder 种子值 66.67（实际 {s[3]}）")
# [1,2,3,2,2]：下一变化 0 → avg_gain=(2/3×2+0)/3=4/9, avg_loss=2/9 → RS=2 → 仍 66.67
s = TI.rsi_series([1, 2, 3, 2, 2], period=3)
ck(s[4] == 66.67, f"Wilder 平滑递推 66.67（实际 {s[4]}）")

# ---------------- 极端形态 ----------------
up = [float(i) for i in range(1, 30)]          # 连涨 29 根
s = TI.rsi_series(up, period=14)
ck(s[-1] == 100.0, "连涨 → RSI 100")
down = [float(30 - i) for i in range(1, 30)]   # 连跌 29 根
ck(TI.rsi_series(down, period=14)[-1] == 0.0, "连跌 → RSI 0")
flat = [50.0] * 20
ck(TI.rsi_series(flat, period=14)[-1] == 50.0, "全平 → 中性 50")

# ---------------- rsi_state（bars 形态） ----------------
bars = [{"date": f"2026-11-{i:02d}", "close": 100 + i * 2}
        for i in range(1, 25)]                  # 稳定上涨
st = TI.rsi_state(bars)
ck(st["rsi"] == 100.0 and st["label"] == "超买", f"稳定上涨 → 超买（实际 {st}）")
ck(st["delta"] is not None, "带较前一日 delta")
st = TI.rsi_state(bars[:10])                    # 不足 period+1
ck(st["rsi"] is None and st["label"] is None, "数据不足 → None（不编造）")
ck(TI.rsi_state([])["rsi"] is None and TI.rsi_state(None)["rsi"] is None, "空/None → None")

# 坏值容错：None/字符串混进 closes
mixed = [{"close": 100 + i} for i in range(20)] + [
    {"close": None}, {"close": "oops"}, {"close": 120}]
st = TI.rsi_state(mixed)
ck(st["rsi"] is not None, "坏值被跳过仍出结果")

# ---------------- Wilder 记忆效应（先涨 10 再跌 10，period=10） ----------------
# 手算：avg_gain=0.9^10≈0.3487，avg_loss≈0.6513 → RS≈0.536 → RSI≈34.90。
# 注意这不是「对称涨跌回中性」——Wilder 平滑有记忆，回落速度由平滑常数决定；
# 第一版测试断言「应回 45~55」是**预期错不是代码错**（修过预期后钉死精确值）。
seq = [100.0 + i for i in range(11)] + [110.0 - i for i in range(1, 11)]
v = TI.rsi_series(seq, period=10)[-1]
ck(abs(v - 34.9) <= 0.1, f"先涨10再跌10 → 34.9（实际 {v}）")

# ---------------- 静态接线 ----------------
tt = (BASE / "t_trading.py").read_text(encoding="utf-8")
ck("tech_ind.rsi_state" in tt, "t_trading advisory 调 RSI")
ck('it["rsi"]' in tt, "advisory item 带 rsi")
ck("RSI" in tt and "注记" in tt, "why 尾部注记存在")
pt = (BASE / "paper_trading.py").read_text(encoding="utf-8")
ck('rsi_inject' in pt and "tech_ind.rsi_state" in pt, "决策上下文接 RSI（带开关）")
ck("仅参考状态非交易信号" in pt, "决策上下文写明非交易信号")

print(f"tech_ind_test: {N}/{N} 全过")
