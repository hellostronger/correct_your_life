# -*- coding: utf-8 -*-
"""advice_followup 单测：信号聚合 / 采纳匹配 / 差额计算 / 汇总。"""
import advice_followup as AF


def _row(code, action, price, movable=0, base=0, t="2026-11-12T10:00:00",
         date="2026-11-12", shares=None, why=""):
    d = {"shares": shares or movable, "shares_suggest": 0}
    return {"code": code, "name": code, "action": action, "price": price,
            "movable": movable, "base_shares": base, "detail": d,
            "created_at": t, "trade_date": date, "why": why or "test"}


def test_group_continuous_rows_into_one_signal():
    rows = [
        _row("00100", "t_sell", 181.0, movable=600, t="2026-11-12T10:09:00"),
        _row("00100", "t_sell", 181.5, movable=600, t="2026-11-12T10:10:00"),
        _row("00100", "t_sell", 182.0, movable=800, t="2026-11-12T10:11:00"),
    ]
    sigs = AF.group_signals(rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["rows"] == 3
    assert s["price"] == 181.0            # 首行价 = 信号锚点
    assert s["suggest_shares"] == 800     # 建议股数取整段最大
    assert s["time"] == "2026-11-12T10:09"


def test_group_splits_on_gap_over_24h():
    rows = [
        _row("00100", "hard_stop", 150.0, base=2520, t="2026-11-13T10:00:00",
             date="2026-11-13"),
        _row("00100", "hard_stop", 151.0, base=2520, t="2026-11-14T10:00:00",
             date="2026-11-14"),     # 24h 整 —— 边界内，同一信号
        _row("00100", "hard_stop", 152.0, base=2520, t="2026-11-17T10:00:00",
             date="2026-11-17"),     # 跨周末 >24h —— 新信号
    ]
    sigs = AF.group_signals(rows)
    assert len(sigs) == 2
    assert sigs[0]["rows"] == 2
    assert sigs[1]["rows"] == 1


def test_group_overnight_same_signal():
    rows = [
        _row("00992", "trend_reduce", 17.8, t="2026-11-12T15:00:00",
             date="2026-11-12", shares=500),
        _row("00992", "trend_reduce", 17.7, t="2026-11-13T09:40:00",
             date="2026-11-13", shares=500),
    ]
    assert len(AF.group_signals(rows)) == 1


def test_suggest_shares_by_action():
    assert AF._suggest_shares(_row("x", "hard_stop", 1, base=2520)) == 2520
    assert AF._suggest_shares(_row("x", "t_sell", 1, movable=600)) == 600
    assert AF._suggest_shares(_row("x", "trend_reduce", 1, shares=100)) == 100


def test_match_followed_same_direction_within_window():
    sigs = AF.group_signals([_row("00100", "t_sell", 181.0, movable=600,
                                  t="2026-11-12T10:09:00", date="2026-11-12")])
    trades = [
        {"code": "00100", "side": "sell", "shares": 600, "price": 181.4,
         "trade_date": "2026-11-12"},          # 当天照做 → 采纳
        {"code": "00100", "side": "buy", "shares": 600, "price": 175.0,
         "trade_date": "2026-11-13"},          # 方向不符，不参与
    ]
    AF.match_followed(sigs, trades)
    assert sigs[0]["followed"] is True
    assert sigs[0]["trade_shares"] == 600
    assert sigs[0]["trade_price"] == 181.4


def test_match_followed_next_day_counts():
    sigs = AF.group_signals([_row("01888", "trend_reduce", 6.0, shares=1000,
                                  t="2026-11-10T14:00:00", date="2026-11-10")])
    trades = [{"code": "01888", "side": "sell", "shares": 1000, "price": 5.9,
               "trade_date": "2026-11-11"}]
    AF.match_followed(sigs, trades)
    assert sigs[0]["followed"] is True


def test_match_followed_out_of_window_not_counted():
    sigs = AF.group_signals([_row("01888", "trend_reduce", 6.0, shares=1000,
                                  t="2026-11-10T14:00:00", date="2026-11-10")])
    trades = [{"code": "01888", "side": "sell", "shares": 1000, "price": 5.9,
               "trade_date": "2026-11-20"}]     # 10 天后才卖 → 不算采纳
    AF.match_followed(sigs, trades)
    assert sigs[0]["followed"] is False


def test_one_trade_consumed_once():
    """一笔成交只能喂饱一条信号（防止差额双算）。"""
    rows = [
        _row("00992", "trend_reduce", 17.8, shares=500,
             t="2026-11-11T10:00:00", date="2026-11-11"),
        _row("00992", "hard_stop", 17.6, base=500,
             t="2026-11-12T10:00:00", date="2026-11-12"),
    ]
    sigs = AF.group_signals(rows)
    trades = [{"code": "00992", "side": "sell", "shares": 500, "price": 17.5,
               "trade_date": "2026-11-11"}]
    AF.match_followed(sigs, trades)
    flw = [s for s in sigs if s["followed"]]
    assert len(flw) == 1                       # 只喂了先出的 trend_reduce


def test_evaluate_sell_followed_uses_actual_price():
    sigs = AF.group_signals([_row("00100", "t_sell", 181.0, movable=600,
                                  t="2026-11-12T10:09:00", date="2026-11-12")])
    AF.match_followed(sigs, [{"code": "00100", "side": "sell", "shares": 600,
                              "price": 181.4, "trade_date": "2026-11-12"}])
    sigs[0]["is_hk"] = True                    # 端点在 evaluate 前标记币种
    AF.evaluate(sigs, {"00100": 150.0}, rate=0.85)
    s = sigs[0]
    assert s["eval_src"] == "actual"
    # 卖出 181.4 vs 持有到现在 150：优势 = 600×(181.4-150) = +18840
    assert abs(s["gain"] - 18840.0) < 0.01
    assert abs(s["gain_cny"] - 18840.0 * 0.85) < 0.01


def test_evaluate_missed_counterfactual_can_be_negative():
    """没照做且价格后来涨了 → 反事实差额为负（不理是对的）。"""
    sigs = AF.group_signals([_row("00100", "hard_stop", 150.0, base=2520,
                                  t="2026-11-13T10:00:00", date="2026-11-13")])
    AF.match_followed(sigs, [])
    AF.evaluate(sigs, {"00100": 165.0}, rate=1.0)
    # 止损卖 150 vs 持有到现在 165：照做会少赚 2520×15 = -37800
    assert abs(sigs[0]["gain"] - (-37800.0)) < 0.01


def test_evaluate_buy_direction():
    sigs = AF.group_signals([_row("02513", "t_buy", 400.0, movable=200,
                                  t="2026-11-12T10:00:00", date="2026-11-12")])
    AF.match_followed(sigs, [])
    AF.evaluate(sigs, {"02513": 430.0}, rate=1.0)
    # 买回 400 vs 现在 430：照做优势 = 200×30 = +6000
    assert abs(sigs[0]["gain"] - 6000.0) < 0.01


def test_evaluate_missing_price_gain_none():
    sigs = AF.group_signals([_row("02513", "t_buy", 400.0, movable=200,
                                  t="2026-11-12T10:00:00", date="2026-11-12")])
    AF.match_followed(sigs, [])
    AF.evaluate(sigs, {}, rate=1.0)
    assert sigs[0]["gain"] is None             # 缺行情：不计差额，条数照计


def test_summarize_counts_and_gains():
    rows = [
        _row("00100", "t_sell", 181.0, movable=600,
             t="2026-11-12T10:09:00", date="2026-11-12"),
        _row("01888", "hard_stop", 6.0, base=1000,
             t="2026-11-10T14:00:00", date="2026-11-10"),
    ]
    sigs = AF.group_signals(rows)
    AF.match_followed(sigs, [{"code": "00100", "side": "sell", "shares": 600,
                              "price": 181.4, "trade_date": "2026-11-12"}])
    AF.evaluate(sigs, {"00100": 150.0, "01888": 6.5}, rate=1.0)
    sm = AF.summarize(sigs)
    assert sm["total"] == 2 and sm["followed_n"] == 1 and sm["missed_n"] == 1
    assert abs(sm["followed_gain_cny"] - 18840.0) < 0.01   # 00100 实际卖
    assert abs(sm["missed_gain_cny"] - (-500.0)) < 0.01    # 01888 止损卖6 vs 现6.5
    assert abs(sm["net_gap_cny"] - 18340.0) < 0.01
    assert sm["by_action"][0]["total"] >= 1


def test_hold_and_wait_rows_ignored():
    rows = [
        _row("x", "hold", 1.0, t="2026-11-12T10:00:00"),
        _row("x", "trend_wait", 1.0, t="2026-11-12T10:01:00"),
    ]
    assert AF.group_signals(rows) == []


if __name__ == "__main__":
    import sys
    _failed = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as _e:
                _failed += 1
                print("FAIL", _name, _e)
    print("done, failed =", _failed)
    sys.exit(1 if _failed else 0)
