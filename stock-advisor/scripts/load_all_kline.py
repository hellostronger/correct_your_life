# -*- coding: utf-8 -*-
"""全市场日线灌库（并发）。

为什么必须并发
--------------
全市场 5000+ 只，串行一只 ~1 秒就是 1.5 小时。并发 8 线程压到 ~10 分钟。

**只用腾讯源**，这是有实测依据的取舍：
  - 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn 我今天打了几百次没被限速
  - 东财 push2 我已经被封过一次（6 个镜像主机全挂），全市场灌数据
    不能再依赖它；push2his 也 RemoteDisconnected
  - 腾讯单次约 640 行（2.6 年），对「验证社区策略」这个用途够用
代价：腾讯没有成交额和换手率。那两列留 None —— 社区策略用得不多，
而为了它去赌东财的限流不值得。

限速策略
--------
8 个线程 + 每个线程内部 0.12s 间隔。实测这个量级腾讯不拦。真被限流了
（本脚本会统计失败数）就把 WORKERS 调小重跑 —— ON CONFLICT 保证幂等，
重跑不会产生重复行。

失败不中断
----------
单只失败就记下来继续，最后汇总。5000 只里挂几只很正常，
不该因为第 200 只超时就把整个任务废掉。
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import market_data as MD                        # noqa: E402
from psycopg2 import connect                    # noqa: E402

DB = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
      "password": "", "dbname": "postgres"}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" not in line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
         "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    if k.strip() in m:
        val = v.strip()
        DB[m[k.strip()]] = int(val) if k.strip() == "DB_PORT" else val

# 线程数 = 4，不是越多越好。实测（同一批 24 只，只测 HTTP 不写库）：
#   1 线程 0.92 只/秒  2 线程 1.05  4 线程 1.14  8 线程 1.16  16 线程 1.15
# 吞吐到 4 线程就压平了（对端按出口 IP 限 ~1.15 请求/秒），但**单请求耗时
# 随线程数线性变长**（1.09s -> 3.5s -> 6.9s -> 13.9s）。所以 8 线程以上
# 只会成倍放大超时概率，吞吐一点不涨。
WORKERS = 4
THROTTLE = 0.12          # 每线程内部最小间隔（秒）
BATCH_CODES = 40         # 攒多少只就写一次库
YEARS = 4.0

# 沪深 A 股（含科创板 688 / 创业板 300）。**不含北交所**：
# 社区策略里几乎都用 c.endswith('.XSHG') or c.endswith('.XSHE') 过滤，
# 北交所会被排除，灌了也用不上（而且流动性差、数据质量差）。
EXCLUDE_PREFIX = ("4", "8", "92")


def get_conn():
    c = connect(connect_timeout=60, **DB)
    c.autocommit = False
    return c


def target_codes() -> list:
    """要灌的代码清单：名册里沪深 A 股 + 库里已有的（不重复劳动）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, name, market FROM sa_stock_roster")
        roster = {r[0]: (r[1], r[2] or "") for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT code FROM sa_market_kline")
        have = {r[0] for r in cur.fetchall()}
    out = []
    for c, (name, mkt) in roster.items():
        if not c.isdigit() or len(c) != 6:
            continue
        if c.startswith(EXCLUDE_PREFIX):
            continue
        if "北京" in mkt:
            continue
        out.append(c)
    return sorted(out), have


def main():
    print("=== 全市场日线灌库 ===")
    codes, have = target_codes()
    todo = [c for c in codes if c not in have]
    print("  名册沪深 A 股 %d 只，库里已有 %d 只，待灌 %d 只"
          % (len(codes), len(have), len(todo)))
    if not todo:
        print("  没有要灌的")
        return
    start = (date.today() - timedelta(days=int(YEARS * 365))).isoformat()
    end = date.today().isoformat()
    print("  区间 %s ~ %s  并发 %d  每只间隔 %.2fs" % (start, end, WORKERS,
                                                    THROTTLE))
    t0 = time.time()
    lock = threading.Lock()
    buf: list = []
    done = {"n": 0, "rows": 0, "fail": 0, "errs": {}, "delisted": 0,
            "delisted_codes": [], "fail_codes": []}

    def worker(code: str):
        try:
            rows = MD.fetch_tx_kline(code, start, end)
            if not rows:
                # 0 行 ≠ 源坏了。名册里混着已退市/长期停牌/本来就无效的代码，
                # 腾讯对它们返回空数组。这属于「本来就该没有」，单列一类，
                # 免得跟真故障混在一起白排查。
                with lock:
                    done["delisted"] += 1
                    done["delisted_codes"].append(code)
                return
            with lock:
                buf.extend(rows)
                done["n"] += 1
                done["rows"] += len(rows)
                n = done["n"]
                if n % 25 == 0:
                    el = time.time() - t0
                    print("  %d/%d 只  %d 行  %.0fs  (%.1f 只/秒)"
                          % (n, len(todo), done["rows"], el, n / max(el, 0.1)),
                          flush=True)
        except Exception as exc:                # noqa: BLE001
            with lock:
                done["fail"] += 1
                k = type(exc).__name__ + ":" + str(exc)[:50]
                done["errs"][k] = done["errs"].get(k, 0) + 1
                done["fail_codes"].append(code)
        finally:
            time.sleep(THROTTLE)

    conn = get_conn()
    cur = conn.cursor()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # **必须一码一任务**。第一版写成 submit(lambda: [worker(c) for c in batch])，
        # 一个任务包了 40 只 —— 线程池拿到 1 个任务，主线程又立刻 f.result()
        # 阻塞，于是整个脚本退化成纯串行（实测 1.17s/只，和单线程拉一模一样，
        # 8 线程池完全没用上）。这种错不报错，只是白等 100 分钟。
        for i in range(0, len(todo), BATCH_CODES):
            batch = todo[i:i + BATCH_CODES]
            futs = [ex.submit(worker, c) for c in batch]
            for f in futs:
                f.result()          # 等这批全部结束，再把缓冲落库
            if buf:
                with lock:
                    rows, buf[:] = list(buf), []
                n = MD.store_kline(cur, rows)
                conn.commit()
                el = time.time() - t0
                d = done["n"] + done["fail"]
                print("  已落库 %d 行  [%d/%d 只  %.0fs  预计还剩 %.0f 分钟]"
                      % (n, d, len(todo), el,
                         (len(todo) - d) * el / max(d, 1) / 60.0), flush=True)
    if buf:
        MD.store_kline(cur, buf)
        conn.commit()

    el = time.time() - t0
    total = done["n"] + done["fail"] + done["delisted"]
    print()
    print("=== 结果 ===")
    print("  拉取 %d 只：有数据 %d / 退市无效 %d / 报错 %d"
          % (total, done["n"], done["delisted"], done["fail"]))
    print("  写入约 %d 行 / 用时 %.0fs（有效速度 %.2f 只/秒）"
          % (done["rows"], el, done["n"] / max(el, 0.1)))
    if done["delisted"]:
        # 这批要单独看：如果是「名册里有、但实际从没上市/已退市」就没问题；
        # 如果里面混着本该有数据的票，说明名册脏了，要回头清 sa_stock_roster。
        print("  退市/无效 %d 只（前 15 个）: %s"
              % (done["delisted"], ", ".join(done["delisted_codes"][:15])))
    if done["errs"]:
        print("  真报错分类（最多 6 类）:")
        for k, v in sorted(done["errs"].items(), key=lambda x: -x[1])[:6]:
            print("    %4d 次  %s" % (v, k[:88]))
    with get_conn() as c2, c2.cursor() as cu:
        cu.execute("SELECT count(DISTINCT code), count(*) FROM sa_market_kline")
        a, b = cu.fetchone()
        cu.execute("SELECT min(trade_date), max(trade_date) FROM sa_market_kline")
        d0, d1 = cu.fetchone()
    print("  sa_market_kline 现在 %d 只 / %d 行，区间 %s ~ %s" % (a, b, d0, d1))
    conn.close()


if __name__ == "__main__":
    main()
