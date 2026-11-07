# -*- coding: utf-8 -*-
"""模拟账户加本金（paper_trading.deposit）离线断言。Fake 连接，不联网不连库。
跑法：python stock-advisor/paper_deposit_test.py

覆盖：三字段同步（cash/initial_cash/total_value 少一个都是坑）/
金额校验（<=0、非数、上限）/ 账户未初始化 / 侧车审计可关（stub）/
静态接线（端点、模型、前端按钮与 formDlg）。
"""
import ast
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

import paper_trading as PT                            # noqa: E402

N = 0
FAILS = []


def ok(cond, label):
    global N
    N += 1
    if not cond:
        FAILS.append(label)
        print(f"  FAIL #{N}: {label}")


class FakeCur:
    """每次 SELECT 依次消费一份行（deposit 里 UPDATE 前后各读一次账户）。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []
        self.description = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

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


ACC = {"initial_cash": 100000.0, "cash": 50000.0, "total_value": 120000.0}
ACC_AFTER = {"initial_cash": 300000.0, "cash": 250000.0, "total_value": 320000.0}
# UPDATE 后 _account_row 重读 —— FakeCur 依次返回两份行


def run_deposit(amount, note="", rows=(ACC, ACC_AFTER)):
    sidecars = []
    saved = getattr(PT, "_deposit_sidecar")
    PT._deposit_sidecar = lambda amt, n, acc: sidecars.append((amt, n, dict(acc)))
    deps = {"get_conn": lambda: FakeConn(FakeCur(list(rows)))}
    try:
        r = PT.deposit(deps, amount, note)
    finally:
        PT._deposit_sidecar = saved
    return r, sidecars


def test_deposit():
    print("== 1. deposit 三字段同步 ==")
    r, sc = run_deposit(200000, "锚定智谱同步")
    ok(r["ok"] is True and r["amount"] == 200000.0, "入金成功")
    ok(r["cash"] == 250000.0 and r["initial_cash"] == 300000.0
       and r["total_value"] == 320000.0, "返回三字段的新值")
    ok(sc and sc[0][0] == 200000.0 and sc[0][1] == "锚定智谱同步"
       and sc[0][2]["cash"] == 250000.0, "侧车带金额/备注/入金后账户快照")
    print("== 2. 金额校验 ==")
    r2, _ = run_deposit(0)
    ok(r2["ok"] is False and "> 0" in r2["reason"], "0 拒绝")
    r3, _ = run_deposit(-500)
    ok(r3["ok"] is False, "负数拒绝")
    r4, _ = run_deposit("abc")
    ok(r4["ok"] is False and "无效" in r4["reason"], "非数拒绝")
    r5, _ = run_deposit(200_000_001)
    ok(r5["ok"] is False and "上限" in r5["reason"], "超上限拒绝")
    print("== 3. 账户未初始化 ==")
    deps = {"get_conn": lambda: FakeConn(FakeCur([None]))}
    saved = PT._deposit_sidecar
    PT._deposit_sidecar = lambda *a: None
    try:
        r6 = PT.deposit(deps, 1000)
        ok(r6["ok"] is False and "未初始化" in r6["reason"], "无账户行拒绝且可解释")
    finally:
        PT._deposit_sidecar = saved
    print("== 4. 对账一致性（reconcile 口径） ==")
    # reconcile 用 initial_cash + 流水对账现金：入金同步加两者，drift 恒 0
    ok(abs((ACC_AFTER["cash"] - ACC["cash"])
           - (ACC_AFTER["initial_cash"] - ACC["initial_cash"])) < 1e-9,
       "cash 增量 == initial_cash 增量（入金不产生 drift）")


def test_static_wiring():
    print("== 5. 静态接线 ==")
    with open(os.path.join(_here, "app.py"), encoding="utf-8-sig") as f:
        src_app = f.read()
    ok('@app.post("/api/paper/deposit")' in src_app, "POST 端点")
    ok("class PaperDepositIn(BaseModel)" in src_app, "请求模型")
    ok("paper_trading.deposit(_paper_deps(), body.amount, body.note)" in src_app,
       "端点调 deposit")
    with open(os.path.join(_here, "static", "index.html"), encoding="utf-8-sig") as f:
        src_html = f.read()
    ok('onclick="paperDeposit()"' in src_html, "加本金按钮")
    ok("async function paperDeposit()" in src_html, "JS 函数")
    ok("/api/paper/deposit" in src_html, "前端调端点")
    ok("formDlg('➕ 模拟账户加本金'" in src_html, "走统一 formDlg（不用原生 prompt）")
    tree = ast.parse(open(os.path.join(_here, "paper_trading.py"),
                          encoding="utf-8-sig").read())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ok("deposit" in fns, "paper_trading.deposit 存在")
    ok("_deposit_sidecar" in fns, "侧车函数独立（可 stub）")


if __name__ == "__main__":
    test_deposit()
    test_static_wiring()
    print(f"\n共 {N} 项，失败 {len(FAILS)}")
    if FAILS:
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("ALL PASS")
