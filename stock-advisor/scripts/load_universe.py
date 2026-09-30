"""往 sa_market_kline 补一批票的数据。

为什么要补：本地只有 3 只（510300/159915/600519，1923 行），
**多因子/截面类策略的横截面必须有足够多的标的** —— 只有 3 只的时候
「按因子排序取前 N」毫无意义，回测结果没有参考价值。
社区抓来的策略里多因子/小市值/轮动占大头，都需要真实截面。

只用东财和腾讯两个公开源（market_data.fetch_tx_kline / fetch_em_kline），
不碰任何需要付费的数据接口。
"""
import sys
import time
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

# 一篮子覆盖不同板块的票 + 几只 ETF（ETF 适合当宽基/轮动的替代标的）
UNIVERSE = [
    # 宽基 ETF
    "510300", "510500", "510050", "159915", "588000", "512100",
    # 银行/保险/券商
    "601398", "601939", "601288", "600036", "601318", "600030",
    # 消费/医药
    "600519", "000858", "000568", "600276", "300760", "000001",
    # 资源/周期
    "601899", "600188", "000933", "600309", "601088", "600585",
    # 制造/新能源/科技
    "300750", "002594", "600031", "000651", "002415", "300124",
    # 军工/建筑/交运
    "600893", "601668", "601800", "600009", "601111", "600104",
]


def get_conn():
    c = connect(connect_timeout=30, **DB)
    c.autocommit = False
    return c


def main():
    conn = connect(connect_timeout=30, **DB)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT code, count(*), min(trade_date), max(trade_date) "
                "FROM sa_market_kline GROUP BY code")
    have = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    print("=== 已有数据 ===")
    for c, (n, a, b) in sorted(have.items()):
        print("  %s %5d 行  %s ~ %s" % (c, n, a, b))
    todo = [c for c in UNIVERSE if c not in have]
    print("\n=== 待补 %d 只：%s ===" % (len(todo), todo))

    ok = fail = 0
    deps = {"get_conn": get_conn}
    for i, code in enumerate(todo, 1):
        t0 = time.time()
        try:
            r = MD.sync_code(deps, code, years=4.0)
        except Exception as exc:                # noqa: BLE001
            print("  [%2d/%d] %s 抛异常: %s" % (i, len(todo), code,
                                             str(exc)[:70]))
            fail += 1
            continue
        n = r.get("fetched", 0)
        if n:
            print("  [%2d/%d] %s 写入 %4d 行 %s~%s (%.1fs)%s"
                  % (i, len(todo), code, n, r.get("oldest"), r.get("newest"),
                     time.time() - t0,
                     "  部分源失败" if r.get("partial") else ""))
            ok += 1
        else:
            print("  [%2d/%d] %s 无新增 %s"
                  % (i, len(todo), code, r.get("why") or r.get("error") or ""))
            fail += 1
        time.sleep(0.35)          # 别把源站打挂（我之前把东财打过两次）

    cur.execute("SELECT count(DISTINCT code), count(*) FROM sa_market_kline")
    c2, r2 = cur.fetchone()
    print("\n=== 结果：成功 %d，失败 %d ===" % (ok, fail))
    print("  sa_market_kline 现在 %d 只 / %d 行" % (c2, r2))
    cur.execute("""SELECT min(trade_date), max(trade_date)
                   FROM sa_market_kline""")
    a, b = cur.fetchone()
    print("  区间 %s ~ %s" % (a, b))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
