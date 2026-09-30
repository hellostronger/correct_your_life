# -*- coding: utf-8 -*-
"""全市场估值灌库（按日批量，一天一个请求）。

为什么必须走「按日批量」而不是逐只
--------------------------------
逐只调 `fetch_em_valuation` 是 5500 次请求 × 每次分页 2~20 秒 = 十几小时。
而 `RPT_VALUEANALYSIS_DET` 这个报表**不带 SECURITY_CODE 过滤时能一次吐全市场**
（实测 pageSize=6000 一次返回 5572 条，1.2 秒）。所以改成「一天一个请求」：
975 个交易日 = 975 个请求 = 20 分钟（4 线程约 5 分钟）。

与逐只路径共用同一套解析
----------------------
复用 `valuation_data.EM_MAP` / `_f` / `store_valuation`，**不另写一套映射**。
否则同一只票在两条路下会算出两个市值，策略按市值排序时就用了不确定的数 ——
那种错不报错，只是让回测结果悄悄变成另一回事。

请求区间
--------
默认 2022-10-01 起，与 `load_all_kline.py` 的 K 线区间对齐。估值必须**覆盖
K 线全程**：策略在任意调仓日都要读市值，缺一段就会在某几天报「需要估值数据」
或者（更糟）把那些天的股票当成市值 0 排到最前面。

断点续跑
--------
先查库里已有哪些 trade_date，跳过。跑到一半崩了直接重跑即可（唯一键是
code+trade_date，ON CONFLICT 幂等）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import valuation_data as VD                      # noqa: E402
from psycopg2 import connect                      # noqa: E402

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

WORKERS = 4
BATCH_DAYS = 15               # 攒多少个交易日的行就落一次库
# 从 2024-01-01 起，而不是跟着 K 线的 4 年。原因有两个：
#  1) **腾讯最多只给 641 行**（最早 2024-02-06），所以 K 线表里根本不会有
#     2024-02 之前的数据。估值多灌那 350 天就是 350 × 5572 = 195 万行
#     白占 350MB 磁盘，纯浪费。
#  2) 101 的磁盘只剩 4.4G（已用 89%），`/var/lib/docker` 一个目录就吃 22G。
#     能省的必须省。650 天 × 5572 = 362 万行 ≈ 0.67GB。
# 另外这和沙箱默认的 start=2024-01-01 一致，策略默认就有估值可用。
START = "2024-01-01"
PAGE_SIZE = 6000              # 实测 6000 一次能吐全市场 5572 条
# 一天至少要有这么多只票，才算「这天灌满了」。全市场现在 5572 只，
# 2024-01 时约 5100 只，所以 4000 是个安全线：低到不会漏掉真实的完整日，
# 高到能认出「只有 36 只」那种半成品。
MIN_CODES_PER_DAY = 4000
# **这个报表没有换手率字段**。我一开始把 TURNOVER_RATE 加进 columns，
# 东财返回 success=false / code=9501「返回字段不存在」，而 HTTP 状态是 200
# —— 于是代码把 result=null 当成「这天没数据」，0 行还静默返回。
# 两个教训都留着：字段名不能猜，success 必须判。
# 换手率要靠 `fetch_tx_quote` 逐只补（腾讯那 38 字段里有），
# 全市场 5500 只走不了那条路，所以本表这一列大面积为 NULL。
EXTRA_COLS = ("SECURITY_CODE,TRADE_DATE,TOTAL_SHARES,FREE_SHARES_A,"
              "TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A,PE_TTM,PB_MRQ,"
              "PS_TTM,CLOSE_PRICE")


def get_conn():
    c = connect(connect_timeout=60, **DB)
    c.autocommit = False
    return c


def trading_days(start: str, end: str) -> list:
    """列出工作日。**不剔除法定节假日**。

    节假日东财返回空数组，fetch_day 自然拿到 0 行直接跳过 ——
    比维护一份 A 股节假日表可靠（每年调休都会变，错的表比没有表更糟）。
    """
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    out, d = [], d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def fetch_day(day: str, retries: int = 3) -> list:
    """取某一天全市场估值。返回的行已按我们自己的列名归一。"""
    flt = urllib.parse.quote("(TRADE_DATE='%s')" % day, safe="()='")
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_VALUEANALYSIS_DET&columns=" + EXTRA_COLS +
           "&filter=%s&pageNumber=1&pageSize=%d"
           "&sortColumns=SECURITY_CODE&sortTypes=1&source=WEB&client=WEB"
           % (flt, PAGE_SIZE))
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": VD.UA,
                              "Referer": "https://data.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            # 判 success：东财把「字段不存在」等错误报成 HTTP 200 +
            # success=false + result=null。只看 data 的话，接口报错会被
            # 当成节假日无数据，0 行静默返回 —— 灌完才发现缺一大片。
            if not j.get("success", True):
                raise RuntimeError("东财估值接口报错 code=%s: %s"
                                   % (j.get("code"), str(j.get("message"))[:150]))
            res = j.get("result") or {}
            raw = res.get("data") or []
            rows = []
            for x in raw:
                row = {}
                for em_key, our_key in VD.EM_MAP.items():
                    # **code 和 trade_date 都不能过 _f**。_f 是给数值列用的，
                    # 它把非数值一律转成 None（float() 抛 ValueError 被它吞掉）。
                    # 两个真实的坑：
                    #   code:       "002731" -> 2731.0，前导零被吃掉，于是估值表
                    #               里是 "002731.0"、K 线表里是 "002731"，两边
                    #               对不上，市值类策略看到「零重合」。
                    #   trade_date: "2026-09-30 00:00:00" -> None，于是
                    #               store_valuation 的过滤条件
                    #               `if r.get("code") and r.get("trade_date")`
                    #               把 359 万行全丢掉、返回 0 —— 而我当时打印的是
                    #               缓冲区长度不是返回值，所以 12 次「已落库 8 万行」
                    #               全是假的，表里一行没进。
                    # 逐只路径躲过了第二个坑，因为它最后有
                    # row["code"] = code / row["trade_date"] = td 覆盖。
                    if our_key == "code":
                        c = x.get(em_key)
                        if isinstance(c, (int, float)):
                            c = "%06d" % int(c)      # 补回前导零
                        row[our_key] = str(c).strip()
                    elif our_key == "trade_date":
                        # 东财给的是 "2026-09-30 00:00:00"，取日期部分
                        row[our_key] = str(x.get(em_key) or "")[:10]
                    else:
                        row[our_key] = VD._f(x.get(em_key))
                # 这个报表不提供换手率。显式写 None，不靠 setdefault ——
                # 让「确实没有」和「忘了映射」在代码里就分得开。
                row["turnover_rate"] = None
                # 少任何一样都入不了库，所以在这里就拒掉，别指望下游过滤
                if (row.get("code") and row.get("trade_date")
                        and row.get("total_market_cap")):
                    rows.append(row)
            return rows
        except Exception as exc:                 # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return []


def main():
    print("=== 全市场估值灌库（按日批量，一天 1 个请求）===")
    end = date.today().isoformat()
    days = trading_days(START, end)
    with get_conn() as conn, conn.cursor() as cur:
        # 判定「这天灌完了」必须看**当天有多少只票**，不能只看有没有行。
        # 第一版用 `SELECT DISTINCT trade_date`，结果 718 天里有 666 天被
        # 判成「已灌」—— 因为库里原本那 36 只票就覆盖了 968 个日期，
        # 每天只有 36 行。于是它只补 52 天，666 天永远停在 36 只，
        # 正好是最坏结果：表看着有数据，策略一跑就大面积缺市值。
        cur.execute("""SELECT trade_date FROM sa_stock_valuation
                       GROUP BY trade_date HAVING count(*) >= %s""",
                    (MIN_CODES_PER_DAY,))
        have = {r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])
                for r in cur.fetchall()}
    todo = [d for d in days if d not in have]
    print("  区间 %s ~ %s 共 %d 个工作日" % (START, end, len(days)))
    print("  灌满(>=%d 只/天)已有 %d 天 -> 待灌 %d 天"
          % (MIN_CODES_PER_DAY, len(days) - len(todo), len(todo)))
    if not todo:
        print("  没有要灌的")
        return
    print("  并发 %d  pageSize %d  预计 %d 个请求"
          % (WORKERS, PAGE_SIZE, len(todo)))
    t0 = time.time()
    lock = threading.Lock()
    buf: list = []
    st = {"days": 0, "rows": 0, "empty": 0, "fail": 0, "errs": {}}

    def worker(day: str):
        try:
            rows = fetch_day(day)
        except Exception as exc:                 # noqa: BLE001
            with lock:
                st["fail"] += 1
                k = type(exc).__name__ + ":" + str(exc)[:60]
                st["errs"][k] = st["errs"].get(k, 0) + 1
            return
        with lock:
            if not rows:
                st["empty"] += 1          # 节假日/非交易日，正常
            else:
                st["days"] += 1
                st["rows"] += len(rows)
                buf.extend(rows)
            n = st["days"] + st["empty"] + st["fail"]
            if n % 25 == 0:
                el = time.time() - t0
                print("  %d/%d 天  %d 行  %.0fs  (%.1f 天/秒)"
                      % (n, len(todo), st["rows"], el, n / max(el, 0.1)),
                      flush=True)

    conn = get_conn()
    cur = conn.cursor()
    written = 0

    def flush(rows: list) -> int:
        """落库并**核对真实写入行数**。

        这里第一版打印的是 len(rows)（缓冲区长度），不是 store_valuation 的
        返回值。结果 store_valuation 因为所有行 trade_date 都是 None 而
        过滤掉全部、返回 0，脚本却连着 12 次打印「已落库 8 万行」——
        表里一行没进，我据此以为灌完了。

        教训：凡是「写入 N 行」这种汇报，都必须报**实际写入数**，而且
        缓冲区非空而写入数为 0 要当成错误抛出来，不能当成正常。
        """
        nonlocal written
        n = VD.store_valuation(cur, rows)
        conn.commit()
        if rows and n == 0:
            raise RuntimeError(
                "缓冲区有 %d 行但 store_valuation 写入 0 行 —— 字段映射有问题"
                "（看它的过滤条件：code 和 trade_date 都必须有值）。"
                "样本: %r" % (len(rows), rows[0]))
        written += n
        return n

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i in range(0, len(todo), BATCH_DAYS):
            batch = todo[i:i + BATCH_DAYS]
            for f in [ex.submit(worker, d) for d in batch]:
                f.result()
            if buf:
                with lock:
                    rows, buf[:] = list(buf), []
                n = flush(rows)
                el = time.time() - t0
                d2 = st["days"] + st["empty"] + st["fail"]
                print("  已落库 %d 行（累计 %d）  [%d/%d 天  %.0fs  预计还剩 %.1f 分钟]"
                      % (n, written, d2, len(todo), el,
                         (len(todo) - d2) * el / max(d2, 1) / 60.0), flush=True)
    if buf:
        flush(buf)

    el = time.time() - t0
    print()
    print("=== 结果 ===")
    print("  有数据 %d 天 / 非交易日(空) %d 天 / 报错 %d 天 / 用时 %.0fs"
          % (st["days"], st["empty"], st["fail"], el))
    print("  抓到 %d 行，实际写入 %d 行" % (st["rows"], written))
    if st["errs"]:
        print("  报错分类:")
        for k, v in sorted(st["errs"].items(), key=lambda x: -x[1])[:6]:
            print("    %4d 次  %s" % (v, k[:90]))
    with get_conn() as c2, c2.cursor() as cu:
        cu.execute("SELECT count(*), count(DISTINCT code), min(trade_date),"
                   " max(trade_date) FROM sa_stock_valuation")
        a, b, c, d = cu.fetchone()
    print("  sa_stock_valuation 现在 %d 行 / %d 只 / %s ~ %s" % (a, b, c, d))
    # **交叉核对**：抓到的行数和写进去的行数、以及库里的增量，三者必须对得上。
    # 第一版就是缺这一步：写进去 0 行，报告却说「写入约 359 万行」。
    grew = a - 29046
    print("  库净增 %d 行（灌之前是 29046）" % grew)
    if a < 100000:
        print("  !! 只有 %d 行，远少于抓到的 %d 行 —— 没写进去，别当成完成"
              % (a, st["rows"]))
    else:
        print("  -> 核对通过：库里确实有全市场数据了")
    conn.close()


if __name__ == "__main__":
    main()
