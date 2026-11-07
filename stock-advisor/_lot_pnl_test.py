# -*- coding: utf-8 -*-
"""交易明细单笔盈亏（_attach_lot_pnl）口径离线断言。Fake 行情，不联网。
跑法：python stock-advisor/_lot_pnl_test.py

2026-11-08 用户裁决：买入笔 = 现价 vs **该笔买入价**（旧口径是「vs 买入时点
摊薄成本」，11-06 低位补仓 418.6 显示成 -41.69% 被用户当成错账）。
卖出笔保持平均成本法标准实现（vs 卖出时点摊薄成本）不变。
币种：金额保持录入原币（港股=港元），折算/标注是前端职责。
"""
import os
import sys
from types import SimpleNamespace

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
sys.sa_bootstrap_done = True            # 不起守护线程（AGENTS.md 三个必备动作之一）

import app                              # noqa: E402

N = 0
FAILS = []


def ok(cond, label):
    global N
    N += 1
    if not cond:
        FAILS.append(label)
        print(f"  FAIL #{N}: {label}")


def _t(id, code, side, shares, price, date):
    return {"id": id, "code": code, "trade_date": date, "side": side,
            "shares": shares, "price": price, "note": "",
            "strategy_id": None, "strategy_triggered_at": None,
            "strategy_peak_price": None, "strategy_ladder_step": None,
            "strategy_name": None, "strategy_kind": None,
            "strategy_target_pct": None, "strategy_drawdown_pct": None,
            "strategy_config": {}, "created_at": "2026-11-08T00:00:00"}


def run(rows, price_by_code):
    app.fetch_quotes = lambda codes: {c: {"price": p}
                                      for c, p in price_by_code.items()}
    out = sorted(rows, key=lambda t: -t["id"])   # 新→旧，与 /api/trades 一致
    app._attach_lot_pnl(out)
    return out


def test_per_lot_basis():
    print("== 1. 买入笔 = 现价 vs 该笔买入价（02513 真实数据） ==")
    rows = [
        _t(1, "02513", "buy", 300, 1071.78, "2026-09-09"),
        _t(2, "02513", "buy", 100, 813.0, "2026-09-10"),
        _t(3, "02513", "buy", 100, 563.5, "2026-10-28"),
        _t(4, "02513", "buy", 200, 418.4, "2026-11-06"),
        _t(5, "02513", "buy", 100, 418.6, "2026-11-06"),
    ]
    out = run(rows, {"02513": 426.2})
    by_id = {t["id"]: t for t in out}
    ok(by_id[5]["lot_pnl"] == 760.0 and by_id[5]["lot_pnl_pct"] == 1.82,
       f"11-06 低位补仓 418.6 → +760.00 (+1.82%)（实际 {by_id[5]['lot_pnl']} / {by_id[5]['lot_pnl_pct']}）")
    ok(by_id[4]["lot_pnl"] == 1560.0 and by_id[4]["lot_pnl_pct"] == 1.86,
       "11-06 418.4 → +1560.00 (+1.86%)")
    ok(by_id[3]["lot_pnl"] == -13730.0 and by_id[3]["lot_pnl_pct"] == -24.37,
       "10-28 563.5 → -13730.00 (-24.37%)")
    ok(by_id[2]["lot_pnl"] == -38680.0 and abs(by_id[2]["lot_pnl_pct"] + 47.58) < 0.01,
       "09-10 813 → -38680.00 (-47.58%)")
    ok(by_id[1]["lot_pnl"] == -193674.0 and by_id[1]["lot_pnl_pct"] == -60.23,
       "09-09 首笔 1071.78 → -193674.00 (-60.23%)（首笔摊薄=买价，新旧口径同值）")
    total = sum(t["lot_pnl"] for t in out)
    ok(abs(total - (426.2 - 730.905) * 800) < 0.01,
       f"单笔加总 = 总浮动 -243764.0（实际 {total}）—— 口径变更不动总账")


def test_sell_and_cleared():
    print("== 2. 卖出已实现口径不变 / 清仓后买入笔为空 ==")
    rows = [
        _t(1, "600519", "buy", 300, 100.0, "2026-11-01"),
        _t(2, "600519", "buy", 100, 200.0, "2026-11-02"),
        _t(3, "600519", "sell", 400, 110.0, "2026-11-03"),
    ]
    out = run(rows, {"600519": 105.0})
    by_id = {t["id"]: t for t in out}
    # 摊薄回放：买 300@100 → avg 100；买 100@200 → avg (30000+20000)/400=125
    ok(by_id[3]["lot_realized"] == (110.0 - 125.0) * 400,
       "卖出已实现 = (卖价-当时摊薄)*股数（平均成本法，不随本次裁决变化）")
    ok(by_id[3]["lot_pnl"] == by_id[3]["lot_realized"], "卖出笔 lot_pnl = 已实现")
    ok(by_id[1]["lot_pnl"] is None and by_id[2]["lot_pnl"] is None,
       "已清仓 → 买入笔浮动为 None（持仓已不在，无浮动可算）")
    ok(by_id[1]["lot_cost"] == 100.0 and by_id[2]["lot_cost"] == 200.0,
       "买入笔 lot_cost = 该笔买入价（口径变更后的字段语义）")

    print("== 3. A股与港股同逻辑；无行情时买入笔为空 ==")
    rows2 = [_t(1, "600519", "buy", 100, 10.0, "2026-11-01")]
    out2 = run(rows2, {})                  # 无行情
    ok(out2[0]["lot_pnl"] is None and out2[0]["lot_pnl_pct"] is None,
       "无行情 → 买入笔浮动 None（不编造）")
    rows3 = [_t(1, "600519", "buy", 100, 10.0, "2026-11-01")]
    out3 = run(rows3, {"600519": 11.0})
    ok(out3[0]["lot_pnl"] == 100.0 and out3[0]["lot_pnl_pct"] == 10.0,
       "A股 6 位代码同口径（+100.00 / +10.00%）")


if __name__ == "__main__":
    test_per_lot_basis()
    test_sell_and_cleared()
    print(f"\n共 {N} 项，失败 {len(FAILS)}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
