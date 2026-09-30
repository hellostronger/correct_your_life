# -*- coding: utf-8 -*-
"""从已存的 content_md 重新抽源码（回填），**不发任何 HTTP 请求**。

为什么需要
----------
我改过抽取逻辑好几次：
  1. 只留最长的一个代码块 —— 把《多因子LightGBM》9 块 11279 字的
     源码砍到只剩 12 行（选股逻辑整段丢失）。
  2. 加了脱敏检测（作者用 `...` 省略核心逻辑）。
  3. 改成拼接全部代码块。
已经抓进库的文章是用老逻辑抽的，留在那儿就是错的。

**正文已经存在 content_md 里，重抽根本不需要再请求聚宽。** 所以回填
既便宜又快，也不会再给站点添压力 —— 这一点很重要，因为我实测把东财
打过挂，不该为了修数据再刷一遍源站。

用法：
    python scripts/reextract_source.py            # 全部重抽
    python scripts/reextract_source.py --only-code  # 只重抽有源码的
    python scripts/reextract_source.py --post_id <id>
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import joinquant_source as JS   # noqa: E402
from psycopg2 import connect     # noqa: E402


def db_connect():
    c = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
         "password": "", "dbname": "postgres"}
    env = ROOT.parent / ".env"
    if env.exists():
        m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
             "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k.strip() in m:
                val = v.strip()
                c[m[k.strip()]] = int(val) if k.strip() == "DB_PORT" else val
    return connect(connect_timeout=30, **c)


def main() -> int:
    only_code = "--only-code" in sys.argv
    post_id = None
    if "--post_id" in sys.argv:
        post_id = sys.argv[sys.argv.index("--post_id") + 1]

    conn = db_connect()
    conn.autocommit = False
    cur = conn.cursor()

    sql = ("SELECT post_id, title, content_md, author_id FROM sa_strategy_article")
    args: list = []
    if post_id:
        sql += " WHERE post_id = %s"
        args.append(post_id)
    elif only_code:
        sql += (" WHERE post_id IN (SELECT post_id FROM sa_strategy_source)")
    sql += " ORDER BY fetched_at"
    cur.execute(sql, args)
    rows = cur.fetchall()
    print("=== 重抽 %d 篇（不发 HTTP）===" % len(rows))

    n_code = n_red = 0
    grew = 0
    for pid, title, md, _aid in rows:
        md = md or ""
        src = JS.extract_source(md, None)
        cur.execute("SELECT lines FROM sa_strategy_source WHERE post_id=%s",
                    (pid,))
        old = cur.fetchone()
        old_lines = old[0] if old else 0
        if not src["has_code"]:
            if old:
                # 老逻辑抽出来的代码块，在新逻辑下不算代码（多半是日志）
                cur.execute("DELETE FROM sa_strategy_source WHERE post_id=%s",
                            (pid,))
            continue
        s = src["source"]
        slim = [{k: v for k, v in b.items() if k != "code"} | {"chars": b["chars"]}
                for b in src["blocks"]]
        cur.execute(
            """INSERT INTO sa_strategy_source
               (post_id, lang, code, lines, origin, redacted, stub_sites,
                stub_reasons, n_blocks, blocks, other_blocks)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (post_id) DO UPDATE SET
                 lang=EXCLUDED.lang, code=EXCLUDED.code,
                 lines=EXCLUDED.lines, origin=EXCLUDED.origin,
                 redacted=EXCLUDED.redacted, stub_sites=EXCLUDED.stub_sites,
                 stub_reasons=EXCLUDED.stub_reasons,
                 n_blocks=EXCLUDED.n_blocks, blocks=EXCLUDED.blocks,
                 other_blocks=EXCLUDED.other_blocks,
                 extracted_at=now()""",
            (pid, s.get("lang", ""), s["code"], s["lines"], s.get("origin", ""),
             bool(s["redacted"]), int(s["stub_sites"]),
             __import__("json").dumps(s["stub_reasons"], ensure_ascii=False),
             int(s.get("n_blocks") or 1),
             __import__("json").dumps(slim, ensure_ascii=False),
             __import__("json").dumps(
                 [{k: v for k, v in b.items() if k != "code"}
                  for b in src["other_blocks"]], ensure_ascii=False)))
        n_code += 1
        if s["redacted"]:
            n_red += 1
        flag = ""
        if s["lines"] > old_lines:
            flag = "  <- 比原来多 %d 行" % (s["lines"] - old_lines)
            grew += 1
        print("  %-14s %3d->%3d行 %d块 %-30s%s%s" % (
            pid[:12], old_lines, s["lines"], s.get("n_blocks", 1),
            (title or "")[:30], "  [脱敏]" if s["redacted"] else "", flag))
    conn.commit()

    print("\n  有源码 %d 篇（脱敏 %d 篇），补回 %d 篇丢失的代码"
          % (n_code, n_red, grew))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
