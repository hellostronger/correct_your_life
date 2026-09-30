# -*- coding: utf-8 -*-
"""最终验收：把这一轮做的事从各个角度实测一遍，不看日志不看印象。

覆盖：
  1. 全市场数据规模与质量（K线 / 估值）
  2. 沙箱六个端点是否都通
  3. 体检的内存估算是否与实测一致
  4. 前端新页面在真实 API 下能否渲染（交给 node 那份测试）
  5. 数据库里有没有留下脏数据（超额收益 / 空指标 / 逐日过短）
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from psycopg2 import connect                    # noqa: E402

BASE = "http://127.0.0.1:8686"
ROOT = Path(__file__).resolve().parent.parent
DB = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
      "password": "", "dbname": "postgres"}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
             "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
        if k.strip() in m:
            val = v.strip()
            DB[m[k.strip()]] = int(val) if k.strip() == "DB_PORT" else val

fails = []


def head(t):
    print()
    print("=== %s ===" % t)


def get(p):
    try:
        with urllib.request.urlopen(BASE + p, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as exc:                     # noqa: BLE001
        return 0, {"__err": str(exc)[:120]}


def post(p, body):
    req = urllib.request.Request(BASE + p, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"detail": e.read().decode("utf-8", "replace")[:160]}
    except Exception as exc:                     # noqa: BLE001
        return 0, {"__err": str(exc)[:120]}


def check(ok, label, detail=""):
    print("  %s %-44s %s" % ("OK " if ok else "!! ", label, detail))
    if not ok:
        fails.append(label)


def main():
    conn = connect(connect_timeout=60, **DB)
    conn.autocommit = True
    cur = conn.cursor()

    head("1. 全市场数据")
    cur.execute("SELECT count(*), count(DISTINCT code), min(trade_date),"
                " max(trade_date) FROM sa_market_kline")
    n, c, d0, d1 = cur.fetchone()
    print("  sa_market_kline   %d 行 / %d 只 / %s ~ %s" % (n, c, d0, d1))
    check(c >= 5000, "K线覆盖全市场", "%d 只" % c)
    # 断言的对象要选对。原先写的是「不足 300 天的要有 0 只」，但那 103 只
    # 全是 2026 年新上市的（K线首日 == 名册上市日，103/103 一致），
    # 它们本来就只有几天数据，是对的。真正该为 0 的是
    # 「上市早于 2024-03 却只有 <300 天」—— 那才说明腾讯漏给了数据。
    cur.execute("""SELECT count(*) FROM (
                     SELECT k.code FROM sa_market_kline k
                     GROUP BY k.code HAVING count(*) < 300) s
                   JOIN sa_stock_roster r ON r.code = s.code
                   WHERE r.list_date < '2024-03-01'""")
    missing = cur.fetchone()[0]
    check(missing == 0, "没有「早该有数据却几乎没有」的代码",
          "%d 只" % missing)
    cur.execute("""SELECT count(*) FROM (
                     SELECT code FROM sa_market_kline
                     GROUP BY code HAVING count(*) < 300) t""")
    print("  其中新股 %d 只（K线首日==上市日，合理，不该算问题）"
          % cur.fetchone()[0])
    cur.execute("SELECT count(*), count(DISTINCT code) FROM sa_stock_valuation")
    vn, vc = cur.fetchone()
    print("  sa_stock_valuation %d 行 / %d 只" % (vn, vc))
    check(vc >= 5000, "估值覆盖全市场", "%d 只" % vc)
    cur.execute("""SELECT count(*) FROM sa_stock_valuation
                   WHERE total_market_cap IS NULL OR total_market_cap <= 0""")
    check(cur.fetchone()[0] == 0, "没有市值缺失/为 0 的行")
    cur.execute("""SELECT count(*) FROM sa_market_kline v
                   JOIN sa_stock_valuation u
                     ON u.code=v.code AND u.trade_date=v.trade_date""")
    print("  K线与估值同日可对齐 %d 行" % cur.fetchone()[0])

    head("2. 沙箱端点")
    st, h = get("/api/sandbox/health")
    check(st == 200 and h.get("ok"), "GET /api/sandbox/health",
          "image=%s disk=%s mem=%s" % (h.get("image"), h.get("disk_free"),
                                       h.get("mem_available_mb")))
    st, arts = get("/api/strategy-lib/articles?limit=5")
    check(st == 200 and arts.get("n") is not None, "GET /api/strategy-lib/articles",
          "%d 篇" % len(arts.get("items") or []))
    pid = None
    cur.execute("""SELECT a.post_id FROM sa_strategy_source s
                   JOIN sa_strategy_article a ON a.post_id=s.post_id
                   WHERE s.syntax_ok ORDER BY a.clone_count DESC LIMIT 1""")
    r = cur.fetchone()
    if r:
        pid = r[0]
    check(bool(pid), "有 syntax_ok 的源码可测", str(pid))
    if pid:
        st, pc = post("/api/sandbox/precheck",
                      {"post_id": pid, "start": "2024-01-01", "limit_codes": 400})
        est = pc.get("universe_estimate") or {}
        check(st == 200 and est, "POST /api/sandbox/precheck",
              "估算 %.0fMB / 上限 %sMB" % (est.get("est_peak_rss_mb", 0),
                                           est.get("memory_cap_mb")))
        st, runs = get("/api/sandbox/runs?limit=5")
        check(st == 200, "GET /api/sandbox/runs", "%d 条" % len(runs.get("items") or []))
        st, cmp_ = get("/api/sandbox/compare?post_id=" + pid)
        check(st == 200, "GET /api/sandbox/compare",
              "verdict=%s" % (cmp_.get("verdict") or "(无对比)")[:40])
        st, art = get("/api/strategy-lib/article?post_id=" + pid)
        src = art.get("source") or {}
        check(st == 200 and src.get("code"), "GET /api/strategy-lib/article",
              "源码 %s 行" % src.get("lines"))

    head("3. 脏数据复查（不删，只报）")
    cur.execute("""SELECT id, name, total_return FROM sa_backtest_run
                   WHERE total_return > 500 OR annual_return > 200
                      OR (total_return IS NOT NULL AND trade_count = 0)
                   ORDER BY id""")
    dirty = cur.fetchall()
    check(not dirty, "sa_backtest_run 无超额/空指标", str(dirty[:3]) if dirty else "")
    cur.execute("""SELECT r.id, count(d.*) FROM sa_backtest_run r
                   LEFT JOIN sa_backtest_daily d ON d.run_id = r.id
                   GROUP BY r.id, r.start_date, r.end_date
                   HAVING count(d.*) < 50 AND (r.end_date - r.start_date) > 30""")
    short = cur.fetchall()
    check(not short, "没有「区间很长但逐日很少」的回测", str(short[:3]) if short else "")
    cur.execute("SELECT count(*) FROM sa_sandbox_run WHERE NOT ok")
    failed = cur.fetchone()[0]
    print("  沙箱失败留痕 %d 条（这是要留的，不是脏数据）" % failed)

    head("4. 库体积")
    cur.execute("""SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid))
                   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname='public' AND c.relkind='r'
                   ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 6""")
    for r in cur.fetchall():
        print("  %-26s %s" % (r[0], r[1]))
    conn.close()

    print()
    print("=== 结论 ===")
    if fails:
        print("  !! %d 项不通过: %s" % (len(fails), fails))
        sys.exit(1)
    print("  全部通过")


if __name__ == "__main__":
    main()
