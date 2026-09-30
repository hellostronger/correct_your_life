# -*- coding: utf-8 -*-
"""实测：容器的 512MB 内存到底能撑多大的截面。

为什么要实测而不是估
------------------
`build_slice_csv` 原来把上限写死 400 只，理由含糊。真正的天花板是容器的
`--memory=512m`，而超限的表现是**被 OOM killer 直接杀掉** —— 进程消失、
没有 traceback、stdout 什么都没有，从外面看和「超时」一模一样，根本看不出
是内存爆了。所以只能靠 runner 自己报的 peak_rss_mb 往外推。

跑法：从给定规模一路加码，每档真跑一次容器，记下成败和峰值内存。
一旦失败就停 —— 后面只会更糟，而且 101 只有 4 核 3G，不能并发。

用法: python scripts/calibrate_universe.py [post_id] [400,800,1600]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 关键：先放开上限，否则 build_slice_csv 自己就拦了（默认 400）
os.environ["JQ_MAX_CODES"] = "6000"

import jq_sandbox as JS                        # noqa: E402
from psycopg2 import connect                    # noqa: E402

DB = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
      "password": "", "dbname": "postgres"}
for _line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if "=" in _line and not _line.startswith("#"):
        _k, _, _v = _line.partition("=")
        _m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
              "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
        if _k.strip() in _m:
            _val = _v.strip()
            DB[_m[_k.strip()]] = int(_val) if _k.strip() == "DB_PORT" else _val

PID = sys.argv[1] if len(sys.argv) > 1 else "505366328b8be8ce53ef9575f22a65e0"
SIZES = ([int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2
         else [400, 800, 1600, 2400])
START, END = "2024-01-01", "2026-09-30"


def gc():
    c = connect(connect_timeout=60, **DB)
    c.autocommit = False
    return c


def universe(n: int) -> list:
    """取 n 只 K 线覆盖最全的票。

    和 app.py 的 _sandbox_slice 同一套口径：按覆盖天数降序。降序不是为了
    「优先给数据全的票」这么好心，而是为了**不把刚上市/长期停牌的票混进
    截面** —— 那些票在「按市值排序取最小 N 只」时会被当成最便宜优先买入。
    """
    with gc() as c, c.cursor() as cur:
        cur.execute("""SELECT code, count(*) FROM sa_market_kline
                       WHERE trade_date BETWEEN %s AND %s
                       GROUP BY code ORDER BY count(*) DESC LIMIT %s""",
                    (START, END, n))
        return [r[0] for r in cur.fetchall()]


def get_code(pid: str) -> str:
    with gc() as c, c.cursor() as cur:
        cur.execute("SELECT code FROM sa_strategy_source WHERE post_id=%s", (pid,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise SystemExit("no source for this article; run fetch+extract first")
    return row[0]


def one_run(pid: str, codes: list) -> dict:
    """切数据 + 跑一次容器。返回结果 dict，另附 slice 体积信息。"""
    import shutil
    import tempfile
    tdir = Path(tempfile.mkdtemp(prefix="scale_"))
    try:
        k, v = tdir / "data.csv", tdir / "val.csv"
        ki = JS.build_slice_csv(gc, codes, START, END, k,
                                benchmark=codes[0], max_codes=6000)
        JS.build_valuation_csv(gc, codes, START, END, v)
        r = JS.run_in_docker(get_code(pid), k, START, END, 1_000_000,
                             benchmark=codes[0], timeout=900, val_csv=v)
        r["slice"] = {"bytes": ki.get("bytes", 0),
                      "codes": len(ki.get("codes") or []),
                      "days": ki.get("days")}
        return r
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


# 标定实测记录。每次真跑完把新数据点加进来，并用它核对 jq_sandbox 里的
# RSS_FIXED_MB / RSS_PER_CODE_MB 有没有和现实脱节。
# 只留实测值，不留估计值 —— 估计值混进来就分不清哪个是测的哪个是猜的。
MEASURED = [
    # (codes, slice_mb, peak_rss_mb, total_return_pct)
    (400, 9.9, 153.3, 16.95),
    (800, 19.4, 263.7, 87.72),
    (1600, 38.8, 424.0, 128.57),
]


def verify_constants() -> bool:
    """用实测点反推常数，和 jq_sandbox 里写的比一遍。

    没有这一步的话，常数和现实脱节了没人知道 —— 估算会一直给一个
    「看起来合理」但偏小的数，于是真跑到一半被 OOM 杀掉。
    """
    if len(MEASURED) < 2:
        return True
    (n1, _, m1, _), (n2, _, m2, _) = MEASURED[0], MEASURED[-1]
    slope = (m2 - m1) / (n2 - n1)
    fixed = m1 - n1 * slope
    print("=== 用实测点反推常数 ===")
    print("  实测: %s" % ", ".join("%d只/%.0fMB" % (a, c) for a, _, c, _ in MEASURED))
    print("  反推: 固定 %.1f MB + 每只 %.4f MB" % (fixed, slope))
    print("  代码: 固定 %.1f MB + 每只 %.4f MB" % (JS.RSS_FIXED_MB, JS.RSS_PER_CODE_MB))
    bad = []
    for n, sl, rss, _ in MEASURED:
        pred = JS.RSS_FIXED_MB + JS.RSS_PER_CODE_MB * n
        dev = (pred - rss) / rss * 100 if rss else 0
        flag = "OK " if abs(dev) <= 8 else "!! "
        if abs(dev) > 8:
            bad.append(n)
        print("  %s %4d 只  实测 %6.1f  估算 %6.1f  偏差 %+5.1f%%  切片实测 %.1fMB"
              % (flag, n, rss, pred, dev, sl))
    if bad:
        print("  !! %d 个点偏差超 8%%，常数需要按新实测更新" % len(bad))
        return False
    print("  -> 常数与实测一致（都在 8%% 以内）")
    return True


def main():
    mem = JS.SANDBOX_DEFAULTS.get("memory")
    print("=== universe/memory calibration (container cap %s, serial) ===" % mem)
    print("  post_id %s" % PID)
    if len(sys.argv) <= 2:
        # 没给规模就只核对常数，别顺手把 4 次容器跑起来（那是几十分钟）
        verify_constants()
        return
    rows = []
    for n in SIZES:
        codes = universe(n)
        if len(codes) < n:
            print("  %5d -> only %d codes have kline, skip" % (n, len(codes)))
            continue
        t0 = time.time()
        try:
            r = one_run(PID, codes)
        except Exception as exc:                 # noqa: BLE001
            print("  %5d -> exception %s" % (n, str(exc)[:110]))
            rows.append((n, "EXC", 0.0, 0.0))
            break
        ok = r.get("ok")
        rss = r.get("peak_rss_mb") or 0.0
        mb = (r.get("slice") or {}).get("bytes", 0) / 1048576.0
        met = r.get("metrics") or {}
        tre = ("%+.2f%%" % (met["total_return"] * 100)) \
            if met.get("total_return") is not None else "-"
        print("  %5d codes  slice %6.1fMB  %-4s  peak RSS %6.1fMB  total %-9s %3.0fs  %s"
              % (n, mb, "ok" if ok else "FAIL", rss, tre, time.time() - t0,
                 (r.get("error") or "")[:60]))
        rows.append((n, "ok" if ok else "fail", rss, mb))
        if not ok:
            print("     stop here, bigger sizes only get worse")
            break
    print()
    print("=== summary ===")
    print("  %-9s %-7s %-13s %-10s" % ("universe", "result", "peak RSS MB", "slice MB"))
    for n, st, rss, mb in rows:
        print("  %-9d %-7s %-13.1f %-10.1f" % (n, st, rss, mb))
    good = [r for r in rows if r[1] == "ok" and r[2]]
    if len(good) >= 2:
        (n1, _, m1, _, _), (n2, _, m2, _, _) = good[0], good[-1]
        slope = (m2 - m1) / max(n2 - n1, 1)
        cap_mb = int(str(mem).rstrip("m"))
        print()
        print("  each extra code costs about %.2f MB peak RSS" % slope)
        print("  linear extrapolation: %dMB cap holds about %d codes (20%% headroom)"
              % (cap_mb, int(n2 + (cap_mb * 0.8 - m2) / max(slope, 0.001))))


if __name__ == "__main__":
    main()
