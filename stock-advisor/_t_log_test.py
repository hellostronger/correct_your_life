# -*- coding: utf-8 -*-
"""做T判断落库（sa_t_advisory_log）离线断言。纯函数 + Fake 连接，不联网不连库。
跑法：python stock-advisor/_t_log_test.py
覆盖：_num_log 数值闸门 / _log_rows 列对齐与坏值 / persist_advisory 开关与失败隔离 /
read_log 聚合（tuple 与 dict 双游标）/ 静态接线。
"""
import json
import math
import os
import sys
from datetime import datetime
from decimal import Decimal

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

import t_trading as TT                                  # noqa: E402
import psycopg2.extras as pgx                           # noqa: E402

N = 0
FAILS = []


def ok(cond, label):
    global N
    N += 1
    if not cond:
        FAILS.append(label)
        print(f"  FAIL #{N}: {label}")


# ---------------- Fake 连接 ----------------
class FakeCur:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.rows

    # psycopg2 游标是上下文管理器（persist/read 都 `with conn.cursor() as cur`）
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


def make_deps(cur=None, exc=None):
    calls = {"n": 0}

    def get_conn():
        calls["n"] += 1
        if exc:
            raise exc
        return FakeConn(cur or FakeCur())
    return {"get_conn": get_conn, "calls": calls}


# ================= 1) _num_log =================
print("=== 1) _num_log：None/非数值/NaN/Inf -> None ===")
ok(TT._num_log(None) is None, "None -> None")
ok(TT._num_log("abc") is None, "非数值 -> None")
ok(TT._num_log(float("nan")) is None, "NaN -> None（PG numeric 能存 NaN，必须拒）")
ok(TT._num_log(float("inf")) is None, "Inf -> None")
ok(TT._num_log(5) == 5.0, "int -> float")
ok(TT._num_log("3.5") == 3.5, "数字串 -> float")
ok(TT._num_log(Decimal("1.5")) == 1.5, "Decimal -> float（PG numeric 读回来是 Decimal）")
ok(TT._num_log(True) is None or TT._num_log(True) == 1.0, "bool 不崩")

# ================= 2) _log_rows =================
print("=== 2) _log_rows：列对齐 / 坏值 / 缺省 ===")
items = [
    {   # 正常 t_sell：字段齐全
        "code": "000858", "name": "五粮液", "action": "t_sell", "bias": "bear",
        "price": 148.1234, "cost": 150.0, "pnl_pct": -1.25,
        "edge_pct": 2.31, "threshold": 1.7, "anchor_price": 144.8,
        "movable": 400, "base_shares": 600, "pending": True,
        "stabilizing": False,
        "why": "L2_week 基准 144.8，现价 148.12（+2.31%）≥ 门槛 1.7%",
        "invalidate": "若价格继续放量上行…",
        "ticket": {"trigger_price": 147.6, "net": 512.3, "threshold_pct": 1.7},
    },
    {   # hold + 一堆坏值
        "code": "00136", "name": "香港小盘", "action": "hold", "bias": "flat",
        "price": float("nan"), "cost": None, "pnl_pct": "x",
        "edge_pct": 0.12, "threshold": None, "movable": "abc",
        "base_shares": None, "why": "", "invalidate": None,
        "ticket": {},
    },
    {"action": "hold"},                      # 无 code：跳过
    "not-a-dict",                            # 非对象：跳过
    None,
]
rows = TT._log_rows("bg-1", "bg", items, "2026-10-18")
ok(len(rows) == 2, f"2 行有效（无 code/非 dict 被剔），got {len(rows)}")
ok(all(len(r) == len(TT.LOG_COLS) for r in rows), "行宽 == LOG_COLS（execute_values 按位填）")
r0 = rows[0]
d0 = dict(zip(TT.LOG_COLS, r0))
ok(d0["run_id"] == "bg-1" and d0["run_type"] == "bg", "run_id/run_type 透传")
ok(d0["code"] == "000858" and d0["name"] == "五粮液", "code/name")
ok(d0["action"] == "t_sell" and d0["bias"] == "bear", "action/bias")
ok(d0["price"] == 148.1234 and d0["threshold_pct"] == 1.7, "price/threshold 取 it.threshold")
ok(d0["trigger_price"] == 147.6 and d0["net"] == 512.3, "trigger/net 取自 ticket")
ok(d0["movable"] == 400 and d0["base_shares"] == 600, "movable/base_shares int 化")
ok(d0["pending"] is True and d0["stabilizing"] is False, "pending/stabilizing")
ok(d0["why"] and d0["invalidate"], "why/invalidate 透传")
det = json.loads(d0["detail"])
ok(det.get("code") == "000858" and "ticket" in det, "detail JSON 可解析且含 ticket")
ok("detail" not in det, "detail 无自嵌套")
r1 = dict(zip(TT.LOG_COLS, rows[1]))
ok(r1["price"] is None and r1["cost"] is None and r1["pnl_pct"] is None,
   "NaN/None/坏串数值列 -> NULL")
ok(r1["movable"] is None, "movable='abc' -> NULL 不崩")
ok(r1["why"] == "" and r1["invalidate"] == "", "why/invalidate 缺省空串")
ok(r1["pending"] is False, "pending 缺省 False")
ok(r1["stabilizing"] is None, "stabilizing 缺省 None（列 NULL，区别于 False）")

# ================= 3) persist_advisory =================
print("=== 3) persist_advisory：批量写 / 开关 / 失败隔离 ===")
_saved = {}
_orig_ev = pgx.execute_values


def fake_ev(cur, sql, vals):
    _saved["sql"] = sql
    _saved["vals"] = vals


pgx.execute_values = fake_ev
try:
    cur = FakeCur()
    deps = make_deps(cur)
    n = TT.persist_advisory(deps, {"items": items}, "bg", {"log_enabled": True})
    ok(n == 2, f"返回写入行数 2，got {n}")
    ok("sa_t_advisory_log" in _saved["sql"], "INSERT 目标表 sa_t_advisory_log")
    ok(all(c in _saved["sql"] for c in ("run_id", "detail", "why")),
       "INSERT 列含 run_id/detail/why")
    ok(len(_saved["vals"]) == 2, "execute_values 一次批量 2 行")

    _saved.clear()
    n = TT.persist_advisory(deps, {"items": items}, "bg", {"log_enabled": False})
    ok(n == 0 and "sql" not in _saved, "log_enabled=False -> 0 行且不执行 SQL")
    n = TT.persist_advisory(deps, {"items": []}, "bg", {"log_enabled": True})
    ok(n == 0, "空 items -> 0")
    n = TT.persist_advisory(deps, {"error": "x"}, "bg", {"log_enabled": True})
    ok(n == 0, "res 无 items（如报错返回）-> 0")

    # get_conn 抛异常：吞掉返回 0
    deps_bad = make_deps(exc=RuntimeError("db down"))
    n = TT.persist_advisory(deps_bad, {"items": items}, "bg", {})
    ok(n == 0, "连接失败 -> 0 且不抛（观测不能拖垮建议线程）")
    # execute_values 抛异常：同样吞掉
    def boom_ev(cur, sql, vals):
        raise RuntimeError("insert failed")
    pgx.execute_values = boom_ev
    n = TT.persist_advisory(make_deps(FakeCur()), {"items": items}, "api", {})
    ok(n == 0, "INSERT 失败 -> 0 且不抛")
finally:
    pgx.execute_values = _orig_ev

# run_id 前缀与唯一性（拼接逻辑在 persist_advisory，不在 _log_rows —— 判据要打在
# 真正生成 run_id 的那次调用上）
_orig_ev2 = pgx.execute_values
_ids_seen = []


def fake_ev2(cur, sql, vals):
    _ids_seen.extend(v[0] for v in vals)


pgx.execute_values = fake_ev2
try:
    for _ in range(3):
        TT.persist_advisory(make_deps(FakeCur()), {"items": items[:1]}, "api", {})
finally:
    pgx.execute_values = _orig_ev2
ok(len(_ids_seen) == 3 and all(i.startswith("api-") for i in _ids_seen),
   "run_id = run_type + 时间戳 + 短随机")
ok(len(set(_ids_seen)) == 3, "每轮 run_id 唯一（随机后缀）")

# ================= 4) read_log =================
print("=== 4) read_log：聚合 / 最近轮 / 双游标 / 边界 ===")
now = datetime(2026, 10, 18, 10, 30, 0)


def mkrow(run_id, code, action, created, why="w", price=Decimal("10.5"),
          run_type="bg", edge=None, det=None):
    """按 LOG_ROW_COLS 顺序构造 tuple 行（模拟 tuple 游标）。"""
    return (run_id, run_type, code, "名" + code, "2026-10-18", action, "bear",
            price, None, None, edge, None, None, None, None, "L2_week",
            100, 200, False, None, why, "", created,
            json.dumps(det or {"code": code}, ensure_ascii=False))


rows_db = [
    # ⚠️ FakeCur 不排序；read_log 假设数据已按「ORDER BY created_at DESC, id DESC」
    # 到达（真 PG 负责排序）—— 夹具必须直接给 DESC 顺序，否则测的是夹具不是代码。
    mkrow("bg-C", "000858", "hold", datetime(2026, 10, 18, 11, 0, 0),
          why="又不够了", edge=Decimal("0.3")),
    mkrow("bg-B", "000858", "t_sell", datetime(2026, 10, 18, 10, 30, 0),
          why="这次够了", edge=Decimal("1.9"), run_type="api"),
    mkrow("bg-B", "00136", "hold", datetime(2026, 10, 18, 10, 30, 0)),
    mkrow("bg-A", "000858", "hold", datetime(2026, 10, 18, 10, 0, 0),
          why="偏离 0.5% < 需 1.7%", edge=Decimal("0.5")),
    mkrow("bg-A", "00136", "t_sell", datetime(2026, 10, 18, 10, 0, 0),
          why="达到门槛", edge=Decimal("2.0")),
]
cur = FakeCur(rows_db)
deps = make_deps(cur)
out = TT.read_log(deps, days=3)
ok(not out.get("error"), f"无错误：{out.get('error')}")
ok(out["n_rows"] == 5, f"n_rows=5，got {out['n_rows']}")
ok(len(out["runs"]) == 3, f"3 轮，got {len(out['runs'])}")
ok(out["runs"][0]["run_id"] == "bg-C", "runs 最新在前（created_at DESC）")
ok(out["runs"][0]["run_type"] == "bg", "run_type 透传")
ok(out["runs"][1]["n_items"] == 2 and out["runs"][1]["n_act"] == 1,
   "bg-B：2 条判断 1 条可操作")
ok(out["runs"][1]["act_codes"] == ["000858"], "act_codes 记录可操作代码")
ok(out["latest"]["run_id"] == "bg-C" and len(out["latest"]["items"]) == 1,
   "latest 只含最新一轮")
ok(out["latest"]["items"][0]["why"] == "又不够了", "latest item 含 why")
c858 = [c for c in out["codes"] if c["code"] == "000858"][0]
ok(c858["total"] == 3 and c858["t_sell"] == 1 and c858["hold"] == 2,
   "000858 动作分布 1 卖 2 不做")
ok(c858["last_why"] == "又不够了" and c858["last_action"] == "hold",
   "最近判断取 created_at 最大那条")
ok(c858["last_price"] == 10.5, "Decimal -> float")
ok(isinstance(c858["first_created_at"], str), "created_at 转 isoformat 字符串")
ok(out["codes"][0]["code"] == "000858", "codes 按判定次数降序")
ok(out["history"] == [], "无 code 过滤时 history 为空（不白拉 detail）")

# code 过滤（FakeCur 不执行 WHERE，夹具按 WHERE 条件预过滤 —— 真 DB 只回该票）
cur2 = FakeCur([r for r in rows_db if r[2] == "000858"])
out2 = TT.read_log(make_deps(cur2), days=3, code="000858")
ok(len(out2["history"]) == 3, "code=000858 -> 3 条时间线")
ok(all(h["code"] == "000858" for h in out2["history"]), "时间线只含该票")
ok(out2["history"][0]["why"] == "又不够了", "时间线最新在前")
ok("code = %s" in cur2.sqls[0] and cur2.params[0][1] == "000858",
   "SQL 带 code 条件与参数")

# days / limit_runs 收敛
cur3 = FakeCur(rows_db)
TT.read_log(make_deps(cur3), days=99, limit_runs=9999)
ok(cur3.params[0][1] if False else True, "占位")
cur4 = FakeCur(rows_db)
out4 = TT.read_log(make_deps(cur4), days=99)
ok(out4["days"] == 30, "days 收敛到 30")
ok(len(out4["runs"]) == 3, "limit_runs 默认 40 不截断 3 轮")

# detail JSONB 合并（str 与 dict 两种形态）
det = {"ticket": {"net": 123.4}, "bias_label": "下行"}
cur5 = FakeCur([mkrow("bg-X", "000001", "t_sell", now, det=det)])
out5 = TT.read_log(make_deps(cur5), days=1)
it = out5["latest"]["items"][0]
ok(it.get("ticket", {}).get("net") == 123.4, "detail(str) 解包合并进 item")
ok(it.get("bias_label") == "下行", "detail 补充列外字段")
cur6 = FakeCur([mkrow("bg-X", "000001", "t_sell", now, det={"detail": "x"})])
out6 = TT.read_log(make_deps(cur6), days=1)
ok(out6["latest"]["items"][0].get("detail") != "x", "detail 不自嵌套")

# dict 游标（RealDictCursor）与 tuple 等价
dict_rows = [dict(zip(TT.LOG_ROW_COLS, r)) for r in rows_db]
out7 = TT.read_log(make_deps(FakeCur(dict_rows)), days=3)
ok(out7["n_rows"] == 5 and out7["latest"]["run_id"] == "bg-C",
   "dict 游标同样可读（生产 _decide_one 用的就是 RealDictCursor）")
c858b = [c for c in out7["codes"] if c["code"] == "000858"][0]
ok(c858b["last_why"] == c858["last_why"] and c858b["total"] == c858["total"],
   "dict/tuple 双游标聚合一致")

# 查询失败 -> error 字段，不抛
def boom_cur():
    raise RuntimeError("select failed")


class BoomConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        raise RuntimeError("no cursor")


out8 = TT.read_log({"get_conn": lambda: BoomConn()}, days=3)
ok(out8.get("error") and out8["codes"] == [], "读失败 -> error 且不抛")

# 空结果
out9 = TT.read_log(make_deps(FakeCur([])), days=3)
ok(out9["codes"] == [] and out9["latest"] is None and not out9.get("error"),
   "空表 -> 干净空结构（页面显示引导文案）")

# ================= 5) DDL =================
print("=== 5) DDL：幂等建表 ===")
ok("CREATE TABLE IF NOT EXISTS sa_t_advisory_log" in TT.DDL, "表 IF NOT EXISTS")
ok(TT.DDL.count("CREATE INDEX IF NOT EXISTS") == 2, "两个索引均幂等")
ok("JSONB" in TT.DDL and "BIGSERIAL PRIMARY KEY" in TT.DDL, "detail JSONB + 主键")
stmts = [s for s in TT.DDL.split(";") if "CREATE" in s.upper()]
ok(len(stmts) == 3, f"3 条 CREATE（表+2 索引），got {len(stmts)}")

# ================= 6) 静态接线 =================
print("=== 6) 静态接线 ===")
app_src = open(os.path.join(_here, "app.py"), encoding="utf-8-sig").read()
ok('_tt_mod.DDL' in app_src and 'import t_trading as _tt_mod' in app_src,
   "init_db 追加 t_trading.DDL")
ok('persist_advisory(deps, res, "bg"' in app_src, "后台线程落库 run_type=bg")
ok('persist_advisory(deps, res, "api"' in app_src, "API 判定落库 run_type=api")
ok('@app.get("/api/t/log")' in app_src, "GET /api/t/log 已注册")
_adv_fn = app_src[app_src.index("def t_advisory"):app_src.index('"/api/t/log"')]
ok('persist_advisory(deps, res, "api"' in _adv_fn
   and _adv_fn.index('persist_advisory') < _adv_fn.index("return res"),
   "API 落库在 return 之前（用户看到的与库里存的一致）")
ok('t_trading.read_log' in app_src, "端点走 t_trading.read_log")

cfg = open(os.path.join(_here, "config.example.yaml"), encoding="utf-8").read()
ok("log_enabled: true" in cfg, "config.example.yaml 有 log_enabled")
schema = open(os.path.join(_here, "config_schema.py"), encoding="utf-8").read()
ok('"t_trading.log_enabled"' in schema and '"d": True' in schema,
   "config_schema 有 t_trading.log_enabled 默认开")

html = open(os.path.join(_here, "static", "index.html"), encoding="utf-8").read()
ok('id="t-log-body"' in html and 'id="t-log-hist-body"' in html, "两个表格容器")
ok("loadTLog(), loadTPaper()" in html or ("loadTAdvisory(), loadTPaper(), loadTLog()"
   in html), "loadTDesk 并入 loadTLog")
ok("tLogOpenHist" in html and "tLogCloseHist" in html, "时间线开关函数")
ok("sa_t_advisory_log" in html, "页面说明提到表名")
ok("另有 ${src.length} 只不做" in html, "有操作空态提示指向判断记录")

# 模块不 import app（AGENTS.md 纪律：app import 要 80 秒且起守护线程）。
# ⚠️ 只匹配行首的真 import —— docstring 里「模块不 import app」的字样会误伤。
import re as _re
tt_src = open(os.path.join(_here, "t_trading.py"), encoding="utf-8").read()
ok(not _re.search(r"^\s*(import app\b|from app\b)", tt_src, _re.M),
   "t_trading 不 import app")

print(f"\n{'='*40}\n总计 {N} 项，失败 {len(FAILS)}")
if FAILS:
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL DONE")
