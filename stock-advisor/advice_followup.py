# -*- coding: utf-8 -*-
"""建议 vs 实际操作 统计（2026-11-15 用户需求）。

三个问题：
  1. 引擎给了多少条「动作类」建议（高抛/买回/减仓/止损）？
  2. 其中多少条被实盘操作采纳了，多少条没理？
  3. 采纳的赚了多少差额？没采纳的如果照做会差多少钱（可为负 = 不理是对的）？

设计要点
--------
- **信号聚合**：做T引擎是连续刷的（连续模式下一分钟一轮），同一条件会落库
  几十上百行。必须先把 (code, action) 的连续行聚成一条「信号」——首行时间/
  价格是信号锚点，建议股数取整段最大值。
- **采纳匹配**：信号日起 +3 个自然日内有同方向实盘成交（t_sell/trend_reduce/
  hard_stop/spike_reduce 配 sell，t_buy 配 buy）算「已采纳」。同一笔成交只
  消费一次（防止一笔卖出同时喂饱两条信号）。
- **差额口径**（评估价 = 最新现价 now）：
    卖向：卖出相对继续持有的优势 = n×(卖出价 − now)   价格后跌为正
    买向：买入相对空仓的优势     = n×(now − 买入价)   价格后涨为正
  已采纳用**实际成交价/股数**（用户真实的执行），未采纳用**信号价×建议股数**
  （反事实）。两者都是「照做 vs 不做」的差额，可以同表相加。
  币种：港股差额为 HKD 原币，汇总乘汇率折 CNY（调用方传 rate）。
"""

from __future__ import annotations

ACTIONABLE = ("t_sell", "t_buy", "trend_reduce", "hard_stop", "spike_reduce")
SELL_DIR = {"t_sell", "trend_reduce", "hard_stop", "spike_reduce"}   # 其余为买向

# 信号聚合的相邻行最大间隔：盘中轮次间隔 ~1 分钟，隔夜 ~18 小时 ——
# ≤24h 算同一信号的延续（隔夜条件没解除不算新建议），跨周末（>60h）算新信号。
EPISODE_GAP_HOURS = 24.0

# 采纳匹配窗口（自然日）：信号日当天的盘后/次日早盘手动跟单都算。
FOLLOW_WINDOW_DAYS = 3

ACTION_LABEL = {
    "t_sell": "高抛做T", "t_buy": "买回接回", "trend_reduce": "趋势减仓",
    "hard_stop": "止损警示", "spike_reduce": "异动减持",
}


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if x == x else default          # NaN 防御
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _suggest_shares(row: dict) -> int:
    """单行建议的可执行股数（trend_reduce 存在 detail.shares，spike 在
    shares_suggest，做T在 movable；hard_stop 是清仓信号 → base_shares）。"""
    action = row.get("action")
    if action == "hard_stop":
        return _i(row.get("base_shares"))
    if action in ("t_sell", "t_buy"):
        return _i(row.get("movable"))
    d = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    return max(_i(d.get("shares")), _i(d.get("shares_suggest")), _i(row.get("movable")))


def group_signals(rows: list[dict]) -> list[dict]:
    """把连续刷的 advisory 行聚合成信号。

    输入行字段：code/name/action/price/movable/base_shares/detail/created_at/
    trade_date/why。created_at 排序后同 (code, action) 且间隔 ≤24h 归并。
    """
    usable = [r for r in rows if r.get("action") in ACTIONABLE]
    usable.sort(key=lambda r: (str(r.get("code")), str(r.get("action")),
                               str(r.get("created_at"))))
    signals: list[dict] = []
    for r in usable:
        sig = {
            "code": r.get("code"), "name": r.get("name") or "",
            "action": r.get("action"),
            "dir": "sell" if r.get("action") in SELL_DIR else "buy",
            "date": str(r.get("trade_date") or "")[:10],
            "time": str(r.get("created_at") or "")[:16],
            "price": _f(r.get("price"), 0.0),
            "why": (r.get("why") or "")[:160],
            "suggest_shares": _suggest_shares(r),
            "rows": 1, "_last": r.get("created_at"),
        }
        prev = signals[-1] if signals else None
        if (prev and prev["code"] == sig["code"] and prev["action"] == sig["action"]
                and prev["_last"] is not None and r.get("created_at") is not None):
            gap_h = (_ts(r.get("created_at")) - _ts(prev["_last"])) / 3600.0
            if gap_h <= EPISODE_GAP_HOURS:
                prev["rows"] += 1
                prev["_last"] = r.get("created_at")
                # 建议股数取整段最大（盘中补回的 movable 会涨）
                prev["suggest_shares"] = max(prev["suggest_shares"], sig["suggest_shares"])
                continue
        signals.append(sig)
    for s in signals:
        s.pop("_last", None)
    return signals


def _ts(v):
    """created_at → epoch 秒（datetime 或 ISO 字符串都吃）。"""
    import datetime as _dt
    if isinstance(v, _dt.datetime):
        return v.timestamp()
    try:
        return _dt.datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return 0.0


def _d(s: str):
    import datetime as _dt
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def match_followed(signals: list[dict], trades: list[dict]) -> None:
    """就地给每条信号标 followed / trade 信息。

    trades 字段：code/side/shares/price/trade_date。同一笔成交只消费一次
    ——按信号时间升序分配（先出的信号先用，后来的信号用剩余成交）。
    """
    import datetime as _dt
    # 可用成交池：code -> [ {…, consumed} ]
    pool: dict[str, list[dict]] = {}
    for t in trades:
        pool.setdefault(t.get("code"), []).append(dict(t, consumed=False))
    for s in sorted(signals, key=lambda x: x.get("time") or ""):
        base = _d(s.get("date"))
        s["followed"] = False
        s["trade_shares"] = 0
        s["trade_price"] = None
        if base is None:
            continue
        want_side = "sell" if s["dir"] == "sell" else "buy"
        end = base + _dt.timedelta(days=FOLLOW_WINDOW_DAYS)
        for t in pool.get(s["code"], []):
            if t["consumed"] or t.get("side") != want_side:
                continue
            td = _d(t.get("trade_date"))
            if td is None or not (base <= td <= end):
                continue
            t["consumed"] = True
            s["followed"] = True
            sh, px = _i(t.get("shares")), _f(t.get("price"), 0.0)
            old_sh = s["trade_shares"]
            s["trade_shares"] = old_sh + sh
            # 多笔合并时加权均价
            s["trade_price"] = (round((old_sh * (s["trade_price"] or 0) + sh * px)
                                      / s["trade_shares"], 4)
                                if s["trade_shares"] else None)
    # 清理池字段不外泄（池是本地变量，无需处理）


def evaluate(signals: list[dict], now_prices: dict, rate: float = 1.0) -> None:
    """就地给每条信号算差额（原币 gain + 折CNY gain_cny）。

    now_prices: code -> 现价（原币）。缺价的信号 gain=None（不计入汇总，
    但仍计入采纳/未采纳条数——条数不依赖行情）。
    """
    for s in signals:
        now = now_prices.get(s["code"])
        s["now_price"] = now
        s["gain"] = None
        s["gain_cny"] = None
        if now is None or not isinstance(now, (int, float)) or now <= 0:
            continue
        if s["followed"] and s["trade_shares"] and s["trade_price"]:
            n, p = s["trade_shares"], s["trade_price"]
            src = "actual"
        elif s["suggest_shares"] and s["price"]:
            n, p = s["suggest_shares"], s["price"]
            src = "counterfactual"
        else:
            continue                                    # 无股数可评：只计条数
        s["eval_shares"], s["eval_price"], s["eval_src"] = n, p, src
        gain = (p - now) * n if s["dir"] == "sell" else (now - p) * n
        s["gain"] = round(gain, 2)
        s["gain_cny"] = round(gain * rate, 2) if s.get("is_hk") else round(gain, 2)


def summarize(signals: list[dict]) -> dict:
    """汇总：条数 + 差额（已采纳用实际执行差额；未采纳用反事实差额）。"""
    def _pick(fs):
        return [s for s in fs if isinstance(s.get("gain"), (int, float))]

    followed = [s for s in signals if s.get("followed")]
    missed = [s for s in signals if not s.get("followed")]
    f_gain = round(sum(s["gain_cny"] for s in _pick(followed)), 2)
    m_gain = round(sum(s["gain_cny"] for s in _pick(missed)), 2)
    by_action: dict[str, dict] = {}
    for s in signals:
        a = by_action.setdefault(s["action"], {
            "label": ACTION_LABEL.get(s["action"], s["action"]),
            "total": 0, "followed": 0, "missed": 0, "gain_cny": 0.0})
        a["total"] += 1
        a["followed" if s.get("followed") else "missed"] += 1
        if isinstance(s.get("gain_cny"), (int, float)):
            a["gain_cny"] = round(a["gain_cny"] + s["gain_cny"], 2)
    return {
        "total": len(signals), "followed_n": len(followed), "missed_n": len(missed),
        "followed_gain_cny": f_gain, "missed_gain_cny": m_gain,
        "net_gap_cny": round(f_gain + m_gain, 2),      # 全照做相对现状的总差额
        "evaluated_n": len(_pick(signals)),
        "by_action": [by_action[k] for k in ACTIONABLE if k in by_action],
    }
