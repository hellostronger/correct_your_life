# -*- coding: utf-8 -*-
"""个股锚定模式（paper_anchor.py）离线断言。Fake 连接 + stub，不联网不烧 LLM。
跑法：python stock-advisor/paper_anchor_test.py

覆盖：load_conf 深合并 / build_injection（非锚定票返回 ""、空仓预案、超期文本、
截断留痕、tuple 游标兼容）/ seed_mirror（幂等、沿用、降级 fresh、镜像成交与
现金扣减、现金不足、失败隔离）/ refill_watch（首见建行、回补复位、超期告警
当日去重）/ status 只读聚合 / 静态接线（paper_trading 参数与豁免、app.py 调用点
与端点、config_schema 字段、index.html 注册）。
"""
import ast
import os
import sys
from datetime import datetime
from decimal import Decimal

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

import paper_anchor as PA                            # noqa: E402
import paper_trading as PT                           # noqa: E402

N = 0
FAILS = []


def ok(cond, label):
    global N
    N += 1
    if not cond:
        FAILS.append(label)
        print(f"  FAIL #{N}: {label}")


# ---------------- Fake 基建 ----------------

class FakeCur:
    """按 SQL 子句路由返回行的假游标。

    routes 元素：(SQL 子串, rows, description 可选)。
    rows 给 dict 行时 description 不需要；给 tuple 行时**必须**给 description
    （生产 psycopg2 tuple 游标自带 description，夹具要如实模拟 ——
    朴素解析 SELECT 列名会被 COALESCE 括号里的逗号搞错位）。
    """

    def __init__(self, routes=None, default_rows=None):
        self.routes = routes or []
        self.default_rows = default_rows if default_rows is not None else []
        self.executed = []               # (sql, params)
        self.description = []
        self._ret = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for item in self.routes:
            key, rows = item[0], item[1]
            if key in sql:
                r = rows(sql, params) if callable(rows) else rows
                self._ret = list(r)
                if self._ret and not isinstance(self._ret[0], dict):
                    desc = item[2] if len(item) > 2 else None
                    self.description = desc or []
                return
        self._ret = list(self.default_rows)

    def fetchone(self):
        return self._ret[0] if self._ret else None

    def fetchall(self):
        return self._ret

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **kw):
        return self.cur

    def commit(self):
        self.commits += 1


class Recorder:
    def __init__(self):
        self.notifies = []

    def notify(self, title, content="", **kw):
        self.notifies.append((title, content))


def make_deps(routes=None, holdings=None, days=None, notify=None):
    rec = Recorder()
    deps = {
        "get_conn": lambda: FakeConn(FakeCur(routes)),
        "holdings_fn": lambda: holdings if holdings is not None else [],
        "trading_days_fn": (lambda s, e: days) if days is not None else None,
        "notify_fn": notify or rec.notify,
        "conf": {},
    }
    return deps, rec


class _Stub:
    """paper_anchor 经 PA.PT 调 paper_trading 函数；统一替换/恢复（全程替换，
    不靠「运气不产生副作用」—— test_settle_lock 的教训）。"""

    def __init__(self):
        self.saved = {}

    def __call__(self, **attrs):
        for k, v in attrs.items():
            self.saved[k] = getattr(PT, k)
            setattr(PT, k, v)
        return self

    def restore(self):
        for k, v in self.saved.items():
            setattr(PT, k, v)
        self.saved.clear()


# ---------------- 1. load_conf ----------------

def test_load_conf():
    print("== 1. load_conf 深合并 ==")
    c = PA.load_conf(None)
    ok(c["enabled"] is False and c["code"] == "" and c["seed_mode"] == "mirror",
       "缺省 = ANCHOR_DEFAULTS")
    ok(c["inject"]["max_chars"] == 1600 and c["inject"]["enabled"] is True,
       "inject 嵌套默认完整")
    c2 = PA.load_conf({"enabled": True, "inject": {"enabled": False}})
    ok(c2["enabled"] is True and c2["inject"]["enabled"] is False
       and c2["inject"]["max_chars"] == 1600,
       "浅键覆盖 + inject 深合并（只写 enabled 不顶掉 max_chars）")
    raw = {"inject": {"enabled": False}}
    PA.load_conf(raw)
    ok("max_chars" not in raw["inject"], "load_conf 不改调用方的 dict")


# ---------------- 2. build_injection ----------------

POS = {"02513": {"shares": 5000, "cost": 28.5, "name": "智谱"}}
POS_EMPTY = {}                     # 空仓态：positions 里没有锚定票
STATE_SEED = {"code": "02513", "status": "anchored", "empty_since": None,
              "empty_days": 0, "seed_done": True, "seed_price": 28.0,
              "seed_shares": 5000, "seed_note": "[锚定镜像] x",
              "last_alert_date": None}
STATE_EMPTY = {"code": "02513", "status": "empty", "empty_since": "2026-11-03",
               "empty_days": 7, "seed_done": True, "seed_price": 28.0,
               "seed_shares": 5000, "seed_note": "", "last_alert_date": None}

CONF_ON = {"enabled": True, "code": "02513", "seed_mode": "mirror",
           "max_refill_wait_days": 5,
           "inject": {"enabled": True, "max_chars": 1600}}


def _routes(state, tcount=3, pnl="-1200.5", settled=4, first_cost="28.0"):
    return [
        ("sa_paper_anchor_state", [state] if state else []),
        ("reasoning LIKE '[做T]%%'", [{"count": tcount}]),
        ("settle_price IS NOT NULL",
         [{"pnl": pnl, "count": settled, "first_cost": first_cost}]),
    ]


def run_injection(state, positions, conf=CONF_ON):
    cur = FakeCur(_routes(state))
    stub = _Stub()(_derive_paper_positions=lambda c: dict(positions))
    try:
        return PA.build_injection(cur, "02513", conf)
    finally:
        stub.restore()


def test_injection():
    print("== 2. build_injection ==")
    cur = FakeCur(_routes(STATE_SEED))
    stub = _Stub()(_derive_paper_positions=lambda c: dict(POS))
    try:
        ok(PA.build_injection(cur, "00858", CONF_ON) == "", "非锚定票 → 精确空串")
        ok(PA.build_injection(cur, "02513", {**CONF_ON, "enabled": False}) == "",
           "未启用 → 精确空串")
        ok(PA.build_injection(
            cur, "02513", {**CONF_ON, "inject": {"enabled": False}}) == "",
           "inject 关 → 精确空串")
        ok(PA.build_injection(cur, "02513", {**CONF_ON, "code": ""}) == "",
           "未配置锚定码 → 精确空串")
    finally:
        stub.restore()

    blk = run_injection(STATE_SEED, POS)
    ok(blk.startswith("【锚定模式（mission）"), "块头")
    ok("在本票上累计赚钱" in blk and "择机买回是必须完成的动作" in blk,
       "使命与回补纪律")
    ok("当前持仓 5000 股" in blk and "成本 28.5" in blk, "持仓进度")
    ok("初始成本 28" in blk, "初始成本线（%g 格式）")
    ok("-1,200" in blk or "-1200" in blk, "已实现盈亏带符号")
    ok("做T 3 次" in blk, "做T次数")
    ok("当前**空仓**" not in blk, "持仓态不出空仓预案（使命句里的「空仓躲暴跌」除外）")
    ok("已结算样本 4 笔" in blk and "估算" in blk, "口径标注（不冒充精确值）")

    blk2 = run_injection(STATE_EMPTY, POS_EMPTY)
    ok("当前**空仓**" in blk2 and "第 7 个交易日" in blk2, "空仓状态")
    ok("回补预案" in blk2 and "止跌企稳" in blk2, "回补预案")
    ok("已超期 2 个交易日" in blk2, "超期文本（7 > 5）")
    ok("距超期告警还剩" not in blk2, "超期后不再显示倒计时")

    blk3 = run_injection({**STATE_EMPTY, "empty_days": 2}, POS_EMPTY)
    ok("距超期告警还剩 3 个交易日（上限 5）" in blk3, "倒计时（5-2）")
    ok("已超期" not in blk3, "未超期不出超期文本")

    # 截断助手（空仓块本身 ~350 字符 < 400 下限，走不到截断分支，
    # 所以对 _truncate 直接单测；build_injection 的 cap 下限 400 是防线）
    long_blk = "锚" * 500
    t = PA._truncate(long_blk, 400)
    ok("锚定块超长已截断，全文 500 字符" in t and len(t) <= 400 + 40,
       "超长截断留痕（全文长度入标记）")
    short = "短块"
    ok(PA._truncate(short, 400) is short, "未超长原样返回（同一对象）")

    # enabled=false 时即便命中锚定票也必须逐字节还原为空（决策口径开关）
    ok(run_injection(STATE_SEED, POS, {**CONF_ON, "enabled": False}) == "",
       "enabled=false → 精确空串")


def test_injection_tuple_cursor():
    print("== 2b. build_injection tuple 游标 ==")
    desc_state = [(c,) for c in ("code", "status", "empty_since", "empty_days",
                                 "seed_done", "seed_price", "seed_shares",
                                 "seed_note", "last_alert_date")]
    cur = FakeCur([
        ("sa_paper_anchor_state",
         [("02513", "anchored", None, 0, True, 28.0, 5000, "n", None)],
         desc_state),
        ("reasoning LIKE", [(3,)], [("count",)]),
        ("settle_price",
         [(Decimal("-1200.5"), 4, "28.0")],
         [("pnl",), ("count",), ("first_cost",)]),
    ])
    stub = _Stub()(_derive_paper_positions=lambda c: dict(POS))
    try:
        blk = PA.build_injection(cur, "02513", CONF_ON)
        ok("当前持仓 5000 股" in blk and "做T 3 次" in blk and "初始成本 28" in blk,
           "tuple 游标同样产出完整块（KeyError: 0 教训的回归钉）")
    finally:
        stub.restore()


# ---------------- 3. seed_mirror ----------------

HOLD = [{"code": "02513", "name": "智谱", "net_shares": 10000, "avg_cost": 28.0},
        {"code": "600519", "name": "贵州茅台", "net_shares": 200, "avg_cost": 1500.0}]
# 02513 占比 = 280000 / (280000+300000) ≈ 48.28%；模拟盘 10 万 → 预算 48275
# → 28 元 × 1700 股（100 股整手）
ACCT = {"initial_cash": 100000.0, "cash": 100000.0, "total_value": 100000.0}


def _fee(*a, **k):
    value = a[3] if len(a) > 3 else 0
    return {"total": round(value * 0.003, 2), "commission": 0, "transfer": 0}


def _mkcur(state_rows):
    return FakeCur([("sa_paper_anchor_state", state_rows)])


def test_seed_mirror():
    print("== 3. seed_mirror ==")
    # 已 seed → 幂等跳过，绝不二次加仓
    cur = _mkcur([dict(STATE_SEED)])
    deps, _ = make_deps(holdings=HOLD)
    deps["get_conn"] = lambda: FakeConn(cur)
    stub = _Stub()(_derive_paper_positions=lambda c: {},
                   _account_row=lambda c: ACCT)
    try:
        r = PA.seed_mirror(deps, CONF_ON)
        ok(r["ok"] is True and "幂等跳过" in r["reason"], "已 seed → 幂等跳过")
        ok(not any("INSERT INTO sa_paper_trades" in s for s, _ in cur.executed),
           "幂等路径绝不二次加仓")
    finally:
        stub.restore()

    # 未 seed：镜像成交
    cur2 = _mkcur([])
    deps2, _ = make_deps(holdings=HOLD)
    deps2["get_conn"] = lambda: FakeConn(cur2)
    stub = _Stub()(_derive_paper_positions=lambda c: {},
                   _account_row=lambda c: ACCT,
                   buy_shares_for=lambda code, name, b, p: int(b // p // 100) * 100,
                   trade_fees=_fee,
                   is_star_market=lambda code: False,
                   _insert_trade_sql=PT._insert_trade_sql)
    try:
        r2 = PA.seed_mirror(deps2, CONF_ON)
        ok(r2["ok"] is True and r2.get("shares") == 1700,
           f"镜像股数 1700（实际 {r2.get('shares')}）")
        ok(abs(r2.get("ratio", 0) - 280000 / 580000) < 1e-6,
           "实盘成本占比 48.28%")
        ok(r2.get("price") == 28.0, "成本价取实盘 avg_cost")
        ok(r2.get("fee") == round(1700 * 28.0 * 0.003, 2), "费用按成交额计")
        ins_params = [p for s, p in cur2.executed
                      if "INSERT INTO sa_paper_trades" in s]
        ok(len(ins_params) == 1, "恰好一条买入流水")
        if ins_params:
            ok(ins_params[0][3] == "seed", "种子单 slot=seed（不与决策轮撞 UNIQUE）")
            ok(ins_params[0][4] == "buy" and ins_params[0][10].startswith("[锚定镜像]"),
               "reasoning 带锚定镜像前缀")
        upd = [(s, p) for s, p in cur2.executed if "sa_paper_account" in s]
        ok(len(upd) == 1 and upd[0][1][0] == 1700 * 28.0 + r2["fee"],
           "现金扣减 = 成交额 + 费用")
        ok(any("INSERT INTO sa_paper_anchor_state" in s and "seed_done" in s
               for s, _ in cur2.executed), "seed_done 落状态（upsert INSERT）")
    finally:
        stub.restore()

    # 实盘无该票 → 降级 fresh，不编造、不插流水
    cur3 = _mkcur([])
    deps3, _ = make_deps(holdings=HOLD)
    deps3["get_conn"] = lambda: FakeConn(cur3)
    stub = _Stub()(_derive_paper_positions=lambda c: {},
                   _account_row=lambda c: ACCT)
    try:
        r3 = PA.seed_mirror(deps3, {**CONF_ON, "code": "00858"})
        ok(r3.get("fallback_fresh") is True and "降级 fresh" in r3["reason"],
           "实盘无记录 → 降级 fresh")
        ok(not any("INSERT INTO sa_paper_trades" in s for s, _ in cur3.executed),
           "降级路径绝不插流水")
    finally:
        stub.restore()

    # 模拟盘已有持仓 → 沿用，不建仓
    cur4 = _mkcur([])
    deps4, _ = make_deps(holdings=HOLD)
    deps4["get_conn"] = lambda: FakeConn(cur4)
    stub = _Stub()(_derive_paper_positions=lambda c: dict(POS),
                   _account_row=lambda c: ACCT)
    try:
        r4 = PA.seed_mirror(deps4, CONF_ON)
        ok(r4["ok"] and "沿用" in r4["reason"], "模拟盘已有持仓 → 沿用")
        ok(not any("INSERT INTO sa_paper_trades" in s for s, _ in cur4.executed),
           "沿用路径不建仓")
    finally:
        stub.restore()

    # 现金不足 → 不建仓、不插流水
    cur5 = _mkcur([])
    deps5, _ = make_deps(holdings=HOLD)
    deps5["get_conn"] = lambda: FakeConn(cur5)
    poor = {"initial_cash": 100000.0, "cash": 100.0, "total_value": 100000.0}
    stub = _Stub()(_derive_paper_positions=lambda c: {},
                   _account_row=lambda c: poor,
                   buy_shares_for=lambda code, n, b, p: int(b // p // 100) * 100,
                   trade_fees=_fee,
                   is_star_market=lambda c: False)
    try:
        r5 = PA.seed_mirror(deps5, CONF_ON)
        ok(r5["ok"] is False and "现金不足" in r5["reason"], "现金不足拒绝建仓")
        ok(not any("INSERT INTO sa_paper_trades" in s for s, _ in cur5.executed),
           "现金不足绝不插流水")
    finally:
        stub.restore()

    # holdings_fn 抛异常 → 不抛出，reason 说明（种子失败不拖住决策轮）
    cur6 = _mkcur([])
    deps6, _ = make_deps(holdings=HOLD)
    deps6["get_conn"] = lambda: FakeConn(cur6)
    deps6["holdings_fn"] = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    stub = _Stub()(_derive_paper_positions=lambda c: {},
                   _account_row=lambda c: ACCT)
    try:
        r6 = PA.seed_mirror(deps6, CONF_ON)
        ok(r6["ok"] is False and "失败" in r6["reason"], "异常隔离（不抛出）")
    finally:
        stub.restore()


def test_seed_skip_modes():
    print("== 3b. seed_mirror 其他模式 ==")
    cur = FakeCur()
    deps, _ = make_deps(holdings=HOLD)
    deps["get_conn"] = lambda: FakeConn(cur)
    r = PA.seed_mirror(deps, {**CONF_ON, "seed_mode": "keep"})
    ok(r["ok"] and "跳过镜像建仓" in r["reason"], "keep 模式跳过")
    r2 = PA.seed_mirror(deps, {**CONF_ON, "seed_mode": "fresh"})
    ok(r2["ok"] and "跳过" in r2["reason"], "fresh 模式跳过")
    r3 = PA.seed_mirror(deps, {**CONF_ON, "code": ""})
    ok(r3["ok"] is False and "未配置" in r3["reason"], "空代码拒绝")
    ok(not any("INSERT INTO sa_paper_trades" in s for s, _ in cur.executed),
       "以上路径都不插流水")


# ---------------- 4. refill_watch ----------------

def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def test_refill_watch():
    print("== 4. refill_watch ==")
    deps, rec = make_deps()
    r = PA.refill_watch(deps, {"enabled": False, "code": "02513"})
    ok(r["ok"] is False and "未启用" in r["reason"], "未启用直接返回")

    cur = FakeCur()
    deps1, rec1 = make_deps(days=0)
    deps1["get_conn"] = lambda: FakeConn(cur)
    stub = _Stub()(_derive_paper_positions=lambda c: {})
    try:
        r1 = PA.refill_watch(deps1, CONF_ON)
        ok(r1["ok"] and r1["status"] == "empty" and r1["empty_days"] == 0,
           "空仓首见建行")
        ok(any("INSERT INTO sa_paper_anchor_state" in s
               for s, _ in cur.executed), "首见 INSERT 状态行")
        ok(rec1.notifies == [], "未超期不告警")
    finally:
        stub.restore()

    # 空仓累计：days=7 > 5 → 告警一次
    cur2 = FakeCur([("sa_paper_anchor_state", [dict(STATE_EMPTY)])])
    deps2, rec2 = make_deps(days=7)
    deps2["get_conn"] = lambda: FakeConn(cur2)
    stub = _Stub()(_derive_paper_positions=lambda c: {})
    try:
        r2 = PA.refill_watch(deps2, CONF_ON)
        ok(r2["alerted"] is True and r2["empty_days"] == 7, "超期告警触发")
        ok(len(rec2.notifies) == 1 and "回补超期" in rec2.notifies[0][0],
           "推送一条超期告警")
        ok(any("INSERT INTO sa_paper_anchor_state" in s
               and "last_alert_date" in s
               for s, _ in cur2.executed), "告警日期落库去重（upsert INSERT）")
    finally:
        stub.restore()

    # 同日去重：last_alert_date=今天 → 不再推，天数照常更新
    today_state = {**STATE_EMPTY, "empty_days": 7,
                   "last_alert_date": _today_str()}
    cur3 = FakeCur([("sa_paper_anchor_state", [today_state])])
    deps3, rec3 = make_deps(days=7)
    deps3["get_conn"] = lambda: FakeConn(cur3)
    stub = _Stub()(_derive_paper_positions=lambda c: {})
    try:
        r3 = PA.refill_watch(deps3, CONF_ON)
        ok(r3["alerted"] is False and rec3.notifies == [],
           "同日去重（不重复推送）")
        ok(r3["empty_days"] == 7, "天数照常更新")
    finally:
        stub.restore()

    # 回补复位：持仓恢复 → anchored + refilled 标记
    cur4 = FakeCur([("sa_paper_anchor_state", [dict(STATE_EMPTY)])])
    deps4, rec4 = make_deps(days=7)
    deps4["get_conn"] = lambda: FakeConn(cur4)
    stub = _Stub()(_derive_paper_positions=lambda c: {})
    try:
        stub2 = _Stub()(_derive_paper_positions=lambda c: dict(POS))
        try:
            r4 = PA.refill_watch(deps4, CONF_ON)
            ok(r4["status"] == "anchored" and r4.get("refilled") is True,
               "回补后状态复位 anchored")
            ok(rec4.notifies == [], "复位不再告警")
        finally:
            stub2.restore()
        # trading_days_fn 抛异常 → days=0 不误报
        cur5 = FakeCur([("sa_paper_anchor_state", [dict(STATE_EMPTY)])])
        deps5, rec5 = make_deps()
        deps5["get_conn"] = lambda: FakeConn(cur5)
        deps5["trading_days_fn"] = (
            lambda s, e: (_ for _ in ()).throw(ValueError("x")))
        r5 = PA.refill_watch(deps5, CONF_ON)
        ok(r5["ok"] and r5["empty_days"] == 0 and not r5["alerted"],
           "交易日函数失败 → 天数 0，不误报")
    finally:
        stub.restore()


# ---------------- 5. status ----------------

def test_status():
    print("== 5. status 只读聚合 ==")
    routes = [
        ("sa_paper_anchor_state", [dict(STATE_SEED)]),
        ("reasoning LIKE '[做T]%%'", [{"count": 3}]),
        ("settle_price IS NOT NULL",
         [{"pnl": "-1200.5", "count": 4, "first_cost": "28.0"}]),
        ("ORDER BY id DESC LIMIT 50", [
            {"trade_date": "2026-11-07", "slot": "seed", "side": "buy",
             "shares": 5000, "price": 28.0, "value": 140000.0,
             "status": "open", "reasoning": "[锚定镜像] x",
             "settle_date": None, "settle_price": None, "fee_total": 42.0},
        ]),
    ]
    deps, _ = make_deps(routes=routes)
    stub = _Stub()(_derive_paper_positions=lambda c: dict(POS))
    try:
        s = PA.status(deps, CONF_ON)
        ok(s["available"] is True and s["code"] == "02513", "状态可用")
        ok(s["position"]["shares"] == 5000 and s["position"]["cost"] == 28.5,
           "当前持仓")
        ok(s["perf"]["t_count"] == 3 and s["perf"]["settled_count"] == 4,
           "绩效聚合")
        ok(s["perf"]["realized_pnl"] == -1200.5, "已实现盈亏（Decimal→float）")
        ok(len(s["recent"]) == 1 and s["recent"][0]["slot"] == "seed",
           "最近流水")
        s2 = PA.status(deps, {**CONF_ON, "code": ""})
        ok(s2["available"] is False and "未配置" in s2["reason"], "空代码可解释")
        s3 = PA.status({}, CONF_ON)
        ok(s3["available"] is False and "deps" in s3["reason"], "缺 deps 可解释")
    finally:
        stub.restore()


# ---------------- 5b. 锚定成交优先级（2026-11-08 独立轨道裁决） ----------------

def test_anchor_execution_priority():
    print("== 5b. _execute_plans 锚定优先级 ==")
    calls = []

    def _plan(code, action, roi):
        return {
            "stock": {"code": code, "name": code},
            "decision": {"action": action, "confidence": 5, "reasoning": "",
                         "target_value_pct": 5, "stop_loss_pct": 5},
            "quote": {"price": 10.0}, "bars": [], "roi": roi,
        }

    plans = [_plan("600001", "buy", 9.0),      # ROI 最高
             _plan("02513", "buy", 3.0),       # 锚定票，ROI 低
             _plan("600002", "sell", 0.0)]
    conf = {}
    stub = _Stub()(
        _same_day_fills=lambda deps, today: {},
        _execute_decision=lambda deps, conf, stock, today, decision, quote,
        slot, bars=None, shares_override=None, anchor_code=None:
            (calls.append(stock["code"]), (True, ""))[1],
    )
    try:
        # 无锚定：卖出先、买入按 ROI —— 顺序与从前逐字节一致
        PT._execute_plans({}, conf, plans, "2026-11-08", "10:00")
        ok(calls == ["600002", "600001", "02513"],
           f"无锚定时顺序不变（实际 {calls}）")
        calls.clear()
        # 锚定 02513：卖出仍最先；买入中锚定排最前（哪怕 ROI 低）
        PT._execute_plans({}, conf, plans, "2026-11-08", "10:00",
                          anchor_code="02513")
        ok(calls == ["600002", "02513", "600001"],
           f"锚定票买入越过 ROI 队列排最前（实际 {calls}）")
        # 锚定卖出也排最前（先释放现金）
        calls.clear()
        plans2 = [_plan("600003", "sell", 0.0), _plan("02513", "sell", 0.0),
                  _plan("600004", "buy", 8.0)]
        PT._execute_plans({}, conf, plans2, "2026-11-08", "10:00",
                          anchor_code="02513")
        ok(calls == ["02513", "600003", "600004"],
           f"锚定卖出排最前（实际 {calls}）")
    finally:
        stub.restore()


# ---------------- 5c. 真 upsert 与 psycopg2 % 转义（2026-11-08 真库逮到） ----------------

def test_upsert_and_escape():
    print("== 5c. _upsert_state 真 upsert / LIKE 百分号转义 ==")
    # 纯 UPDATE 在行不存在时静默零行 —— seed_done 永远落不了库的教训
    cur = FakeCur()
    PA._upsert_state(cur, "02513", seed_done=True, status="anchored")
    sql, params = cur.executed[0]
    ok("INSERT INTO sa_paper_anchor_state" in sql
       and "ON CONFLICT (code) DO UPDATE" in sql,
       "行不存在也生效（INSERT ... ON CONFLICT DO UPDATE）")
    ok(params[0] == "02513" and params[1] is True, "参数按列序对齐")
    # 带 params 的 SQL 里字面量 % 必须 %% —— psycopg2 把 %' 当占位符
    # 解析报 IndexError: tuple index out of range（FakeCur 不模拟该解析，
    # 只能靠源码断言钉住）
    src = _src("paper_anchor.py")
    ok("reasoning LIKE '[做T]%%'" in src, "LIKE 百分号已转义（%%）")
    ok("LIKE '[做T]%'" not in src, "不存在未转义的 LIKE '[做T]%'")


# ---------------- 6. 静态接线 ----------------

def _src(path):
    with open(os.path.join(_here, path), encoding="utf-8-sig") as f:
        return f.read()


def test_static_wiring():
    print("== 6. 静态接线 ==")
    tree = ast.parse(_src("paper_trading.py"))
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in ("run_decisions", "_execute_plans", "_execute_decision"):
        args = [a.arg for a in fns[fn].args.args]
        ok("anchor_code" in args, f"{fn} 有 anchor_code 参数")
    # 豁免条件用 AST 找「code != anchor_code」比较节点（数子串会撞 docstring）
    cmp_found = any(isinstance(n, ast.Compare)
                    and isinstance(n.ops[0], ast.NotEq)
                    and isinstance(n.left, ast.Name) and n.left.id == "code"
                    and isinstance(n.comparators[0], ast.Name)
                    and n.comparators[0].id == "anchor_code"
                    for n in ast.walk(tree))
    ok(cmp_found, "_execute_decision 持仓上限豁免（code != anchor_code）")
    # run_decisions 候选剔除豁免：blocked_by_limit 推导里含 != anchor_code
    exempt = any(isinstance(n, ast.Compare)
                 and isinstance(n.ops[0], ast.NotEq)
                 and isinstance(n.comparators[0], ast.Name)
                 and n.comparators[0].id == "anchor_code"
                 for n in ast.walk(tree))
    ok(exempt, "run_decisions 候选剔除豁免（s['code'] != anchor_code）")
    ok("anchor_code=anchor_code" in _src("paper_trading.py"),
       "run_decisions → _execute_plans 透传")
    src_pt = _src("paper_trading.py")
    ok("import paper_anchor as _pa" in src_pt and "_pa.build_injection" in src_pt,
       "_decide_one 调 paper_anchor.build_injection")
    ok('ctx += "\\n" + anchor_block' in src_pt, "锚定块追加进 ctx")
    src_app = _src("app.py")
    ok("import paper_anchor" in src_app, "app.py import paper_anchor")
    ok("paper_anchor.seed_mirror(deps, _anchor)" in src_app, "决策轮种子建仓")
    ok("paper_anchor.refill_watch(deps, _anchor)" in src_app, "决策轮回补监控")
    ok("anchor_code=_anchor_code or None" in src_app, "run_decisions 传锚定码")
    ok('@app.get("/api/paper/anchor")' in src_app, "GET 端点")
    ok('@app.post("/api/paper/anchor/seed")' in src_app, "POST seed 端点")
    ok('"holdings_fn": _derive_holdings' in src_app, "deps 注入 holdings_fn")
    src_cs = _src("config_schema.py")
    for key in ("anchor.enabled", "anchor.code", "anchor.seed_mode",
                "anchor.max_refill_wait_days", "anchor.inject.max_chars"):
        ok(f'"key": "{key}"' in src_cs, f"config_schema 有 {key}")
    src_html = _src("static/index.html")
    ok('data-tab="anchor"' in src_html, "tab 按钮")
    ok('id="tab-anchor"' in src_html, "section 容器")
    ok("anchor:    loadAnchor" in src_html, "TAB_LOADERS 注册 loadAnchor")
    ok("'tdesk', 'anchor'," in src_html, "TABS 数组注册")
    ok("async function loadAnchor()" in src_html, "loadAnchor 定义")
    ok("/api/paper/anchor" in src_html, "前端调锚定端点")
    ok("锚定模式未开启" in src_html, "未启用引导文案")


def test_defaults_consistency():
    print("== 7. 默认值一致性 ==")
    import re
    src_cs = _src("config_schema.py")
    ok(PA.ANCHOR_DEFAULTS["enabled"] is False, "模块默认关（改决策口径，页面开启）")
    m = re.search(r'"key": "anchor\.inject\.max_chars", "t": "int", "d": (\d+)',
                  src_cs)
    ok(m and int(m.group(1)) == PA.ANCHOR_DEFAULTS["inject"]["max_chars"],
       "schema 默认值 == 模块默认值（防副本漂移）")
    m2 = re.search(r'"key": "anchor\.max_refill_wait_days", "t": "int", '
                   r'"d": (\d+)', src_cs)
    ok(m2 and int(m2.group(1)) == PA.ANCHOR_DEFAULTS["max_refill_wait_days"],
       "回补上限默认值一致（5）")
    m3 = re.search(r'"key": "anchor\.seed_mode", "t": "str", "d": "(\w+)"', src_cs)
    ok(m3 and m3.group(1) == PA.ANCHOR_DEFAULTS["seed_mode"], "seed_mode 默认 mirror")


if __name__ == "__main__":
    test_load_conf()
    test_injection()
    test_injection_tuple_cursor()
    test_seed_mirror()
    test_seed_skip_modes()
    test_refill_watch()
    test_status()
    test_anchor_execution_priority()
    test_upsert_and_escape()
    test_static_wiring()
    test_defaults_consistency()
    print(f"\n共 {N} 项，失败 {len(FAILS)}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
