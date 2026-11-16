"""技术指标纯函数（不联网、不连库、不 import app）。

目前只做 RSI（2026-11-16 用户要求「做到RSI」）：全项目此前**没有任何 RSI 数值
计算**（analysis_learn 词表里有 RSI 关键词、技法库里只有 1 条 RSI 文字技法，
都是文字层面）—— 本模块让系统真的会算。

口径
----
- **Wilder 平滑**（业界标准，与 TradingView/聚宽一致）：
  首个 avg_gain/avg_loss = 前 period 个变化的简单平均，
  之后 avg = (prev×(period−1) + cur) / period。
- 输入 close 序列**升序**（fetch_kline_ohlcv 的产出顺序，t_trading 同款）。
- 区间划分（提示口径，不是交易指令）：
  <30 超卖 · 30~45 偏弱 · 45~55 中性 · 55~70 偏强 · ≥70 超买。
"""
from __future__ import annotations

import math


def _finite(v) -> bool:
    try:
        f = float(v)
        return math.isfinite(f)
    except (TypeError, ValueError):
        return False


def rsi_series(closes: list, period: int = 14) -> list[float | None]:
    """Wilder RSI 序列（与 closes 等长，前 period 位为 None——预热不足）。

    closes 升序；不足 period+1 个数据点返回全 None（绝不编造）。
    """
    p = max(2, int(period))
    vals = [float(c) for c in (closes or []) if _finite(c)]
    n = len(vals)
    out: list[float | None] = [None] * n
    if n < p + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        ch = vals[i] - vals[i - 1]
        gains.append(max(0.0, ch))
        losses.append(max(0.0, -ch))
    # 前 p 个变化做简单平均（Wilder 种子）
    avg_gain = sum(gains[:p]) / p
    avg_loss = sum(losses[:p]) / p

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0   # 全涨=100；全平=中性 50
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    out[p] = round(_rsi(avg_gain, avg_loss), 2)
    for i in range(p, n - 1):
        avg_gain = (avg_gain * (p - 1) + gains[i]) / p
        avg_loss = (avg_loss * (p - 1) + losses[i]) / p
        out[i + 1] = round(_rsi(avg_gain, avg_loss), 2)
    return out


def _label(v: float) -> str:
    if v < 30:
        return "超卖"
    if v < 45:
        return "偏弱"
    if v < 55:
        return "中性"
    if v < 70:
        return "偏强"
    return "超买"


def rsi_state(bars: list[dict], period: int = 14) -> dict:
    """从日K bars（fetch_kline_ohlcv 形态：{date, close, ...}，升序）算最新 RSI 状态。

    返回 {"rsi", "prev", "label", "delta", "period"}；数据不足/坏值返回
    {"rsi": None, ...}（不编造）。
    """
    closes = [b.get("close") for b in (bars or []) if _finite(b.get("close"))]
    series = rsi_series(closes, period)
    vals = [v for v in series if v is not None]
    if not vals:
        return {"rsi": None, "prev": None, "label": None, "delta": None,
                "period": period}
    rsi, prev = vals[-1], (vals[-2] if len(vals) >= 2 else None)
    delta = round(rsi - prev, 2) if prev is not None else None
    return {"rsi": rsi, "prev": prev, "label": _label(rsi), "delta": delta,
            "period": period}
