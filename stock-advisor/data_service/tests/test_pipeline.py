"""端到端验证 save_snapshot + build_overview 走新数据源。

不写库：用 mock 掉 _get_conn，把 SQL 结果喂回去，验证
  1. save_snapshot 能吃 data_service 的字段（type/精度）
  2. build_overview 能算分、排序、暴露 stale_days
  3. 陈旧度告警逻辑正确
"""
import os
import sys
from pathlib import Path
from datetime import date, timedelta
from unittest import mock

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sector

from data_service.tests.asserts import FAIL, PASS, check, report


# ---------------- 假 DB ----------------
# psycopg2.extras.execute_values 的三个硬性要求（都踩过）：
#   1. cur.connection.encoding 必须在 psycopg2._ext.encodings 里存在
#      —— 该字典只有 'UTF8'/'UNICODE' 等大写形式，'utf8' 和 'utf-8' 都 KeyError
#   2. cur.mogrify 逐行被调（template, row），这是唯一能拿到「实际插入的行」的地方
#   3. execute_values 传给 cur.execute 的是 bytes（b''.join(parts)），不是 str

class FakeCur:
    """按「即将执行的 SQL」显式派发结果。

    不能靠「上一次 SQL」反查：ensure_tables 的 DDL 会在中间插入，
    导致 _load_history / _load_daily_meta 拿到错位的行。
    """

    def __init__(self, store, conn):
        self.store = store
        self.connection = conn
        self.mogrify = self._mogrify
        self._pending = None

    def _mogrify(self, sql, params):
        self.store["mogrify_calls"] = self.store.get("mogrify_calls", 0) + 1
        if isinstance(params, (list, tuple)):
            self.store.setdefault("snap_rows_arg", []).append(list(params))
        return sql

    def execute(self, sql, args=None):
        head = sql.decode("utf-8", "replace") if isinstance(sql, bytes) else str(sql)
        low = head.lower()
        self.store.setdefault("sqls", []).append(head.strip().split("\n")[0][:70])
        if "create table" in low or "create index" in low:
            self._pending = []
        elif "insert into sa_sector_snapshots" in low:
            self.store["snap_sql_seen"] = True
            self._pending = []
        elif "insert into sa_sector_daily" in low:
            self.store["daily_rows"] = args
            self._pending = []
        elif "select 1 from sa_sector_daily" in low:
            self._pending = self.store.get("daily_exists", (None,))
        elif "from sa_sector_daily" in low and "order by snap_date desc" in low:
            self._pending = self.store.get("daily_meta_result", [])
        elif "from sa_sector_snapshots" in low and "distinct snap_date" in low:
            self._pending = self.store.get("history_result", [])
        elif "max(snap_date)" in low:
            self._pending = [(self.store.get("latest"),)]
        else:
            self._pending = []

    def fetchone(self):
        rows = self._pending or []
        return rows[0] if rows else None

    def fetchall(self):
        return self._pending or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    encoding = "UTF8"          # 必须是 psycopg2 认识的大写形式

    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCur(self.store, self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def with_store(store):
    """装一个假连接。ensure_tables 的 DDL 会被 FakeCur 忽略。"""
    sector._get_conn = lambda: FakeConn(store)


class _Snap:
    """A._merge 只需要 notes 列表，够用即可。"""

    def __init__(self):
        self.notes = []
        self.degraded = False
        self.cross_check = None


print("=" * 78)
print("1. save_snapshot 吃 data_service 的字段")
print("=" * 78)
boards = [
    {"code": "881142", "name": "生物制品", "kind": "industry", "pct": 4.63,
     "turnover": 236.62, "turnover_rate": 1.32, "main_inflow": 14.36,
     "up_count": 53, "down_count": 2, "lead_stock": "康希诺",
     "lead_stock_code": "", "lead_stock_pct": 20.0, "quote_ts": None,
     "score": 67.2},
    {"code": "308941", "name": "猴痘概念", "kind": "concept", "pct": 2.96,
     "turnover": None, "turnover_rate": None, "main_inflow": 1.1,
     "up_count": None, "down_count": None, "lead_stock": "康希诺",
     "lead_stock_code": "", "lead_stock_pct": 20.0, "quote_ts": None,
     "score": 55.0},
    {"code": "", "name": "地域无代码", "kind": "region", "pct": None,
     "turnover": None, "turnover_rate": None, "main_inflow": None,
     "up_count": None, "down_count": None, "lead_stock": "",
     "lead_stock_code": "", "lead_stock_pct": None, "quote_ts": None,
     "score": None},
]
zt = {"qdate": "20260930", "total": 52, "max_lb": 5, "sum_zbc": 3,
      "by_board": {"生物制品": 3}, "tier": "ths_rankings"}
store = {}
with_store(store)
res = sector.save_snapshot(boards, zt, {}, [], None)
print(f"      save_snapshot -> {res}")
check("不抛错", isinstance(res, dict))
check("save_snapshot 发了快照 INSERT", bool(store.get("snap_sql_seen")))
check("mogrify 逐行被调用(批量插入)", bool(store.get("mogrify_calls")),
      note=f"calls={store.get('mogrify_calls')}")
check("写了日表", "daily_rows" in store)
sr = store.get("snap_rows_arg")
if sr:
    check("无 code 的行被丢弃 -> 入库 2 行", len(sr) == 2, note=f"= {len(sr)}")
    check("code 全部非空", all(r[1] for r in sr))
    check("上报了丢弃数", res.get("dropped_no_code") == 1,
          note=f"= {res.get('dropped_no_code')}")
    check("name/kind 非空", all(r[2] and r[3] for r in sr))
    check("pct 列可空(可为 None)", any(r[4] is None for r in sr) or
          all(isinstance(r[4], float) for r in sr))

print()
print("=" * 78)
print("1b. 全部无 code 时必须报错而不是写空")
print("=" * 78)
store_b = {}
with_store(store_b)
try:
    sector.save_snapshot(
        [{"code": "", "name": "A", "kind": "industry", "pct": 1.0,
          "turnover": None, "turnover_rate": None, "main_inflow": None,
          "up_count": None, "down_count": None, "lead_stock": "",
          "lead_stock_pct": None, "quote_ts": None}],
        {}, {}, [], None)
    check("全无 code 时抛错", False, note="没有抛错，会写入脏数据")
except RuntimeError as e:
    check("全无 code 时抛错", "没有 code" in str(e), note=f"实得 {str(e)[:60]}")

print()
print("=" * 78)
print("2. build_overview：算分/排序/陈旧度")
print("=" * 78)
yesterday = date.today() - timedelta(days=1)
store2 = {
    "history_result": [
        (yesterday, "881142", "生物制品", "industry", 4.00, 10.0, 200.0),
        (date.today(), "881142", "生物制品", "industry", 4.63, 14.36, 236.62),
        (yesterday, "308941", "猴痘概念", "concept", 3.20, 2.0, None),
        (date.today(), "308941", "猴痘概念", "concept", 2.96, 1.1, None),
    ],
    "daily_meta_result": [
        (date.today(), 52, 5, 3, {"生物制品": 3}, {}, {}),
    ],
}
with_store(store2)
with mock.patch.object(
        sector, "fetch_zt_pool",
        return_value={"qdate": "20260930", "total": 52, "max_lb": 5,
                      "sum_zbc": 3, "by_board": {"生物制品": 3}}):
    with mock.patch.object(
            sector, "fetch_index_overview", return_value=[]):
        ov = sector.build_overview()

print(f"      snap_date={ov.get('snap_date')} stale_days={ov.get('stale_days')} "
      f"stale={ov.get('stale')}")
print(f"      total_boards={ov.get('total_boards')}")
check("非空", not ov.get("empty"))
check("snap_date 是今天", ov.get("snap_date") == date.today().isoformat())
check("stale_days=0", ov.get("stale_days") == 0, note=f"= {ov.get('stale_days')!r}")
check("stale=False", ov.get("stale") is False, note=f"= {ov.get('stale')!r}")
check("stale_note 为空", not ov.get("stale_note"))
check("有板块", ov.get("total_boards", 0) > 0)
bs = ov["boards"]
check("按分降序", all(bs[i]["score"] >= bs[i + 1]["score"]
                      for i in range(len(bs) - 1)))
check("有 score_parts", "score_parts" in bs[0])
for b in bs:
    print(f"        {b['name'][:14]:<16} {b['score']:>6}  {b['score_parts']}")
# 动量：生物制品 2 天 4.00->4.63 应为正
top = [b for b in bs if b["name"] == "生物制品"]
if top:
    check("动量分为正", top[0]["score_parts"]["mom"] > 0,
          note=f"mom={top[0]['score_parts']['mom']}")

print()
print("=" * 78)
print("3. 陈旧告警：数据落后 9 天")
print("=" * 78)
stale9 = date.today() - timedelta(days=9)
store3 = {
    "history_result": [
        (stale9, "881142", "生物制品", "industry", 4.00, 10.0, 200.0),
    ],
    "daily_meta_result": [
        (stale9, 64, 4, 2, {}, {}, {}),
    ],
}
with_store(store3)
with mock.patch.object(
        sector, "fetch_zt_pool",
        return_value={"qdate": "20260921", "total": 64, "max_lb": 4,
                      "sum_zbc": 2, "by_board": {}}):
    with mock.patch.object(
            sector, "fetch_index_overview", return_value=[]):
        ov9 = sector.build_overview()
print(f"      stale_days={ov9.get('stale_days')} stale={ov9.get('stale')}")
print(f"      note: {ov9.get('stale_note')}")
check("stale_days=9", ov9.get("stale_days"), 9)
check("stale=True", ov9.get("stale"), True)
check("有告警文案", "停留" in (ov9.get("stale_note") or ""))

print()
print("=" * 78)
print("4. collect_health：库异常时不得谎报")
print("=" * 78)


class BoomConn(FakeConn):
    def cursor(self):
        cur = FakeCur(self.store, self)
        cur.execute = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("connection refused"))
        return cur


sector._get_conn = lambda: BoomConn({})
h = sector.collect_health()
print(f"      {h}")
check("stale 为 None(不谎报)", h.get("stale") is None, note=f"= {h.get('stale')!r}")
check("带 db_error", "db_error" in h)
check("仍有 healthy 字段", "healthy" in h)

sys.exit(report())
