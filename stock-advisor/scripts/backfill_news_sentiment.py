# -*- coding: utf-8 -*-
"""按标题文本回填历史新闻的情绪标记（打标与搜索解耦）。

2026-09-29：sentiment 9852 条全空，根因是打标只认「用哪个关键词搜到的」，
而 sa_watchlist 35 只的 keywords_pos/neg 一个都没配。现在改成按标题判定，
于是历史数据可以一次性回填，不用重新抓。

dry-run 默认；--apply 执行；--rollback <file> 回滚。
"""
import sys, json, argparse
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # 项目根（.env 在这里）
sys.path.insert(0, str(BASE))

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--rollback", metavar="FILE")
args = ap.parse_args()

E = BASE.parent / ".env"
c = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
     "password": "", "dbname": "postgres"}
for line in E.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" not in line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    k, v = k.strip(), v.strip()
    m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
         "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    if k in m:
        c[m[k]] = int(v) if k == "DB_PORT" else v

import psycopg2
import psycopg2.extras
import news_fetcher as NF

conn = psycopg2.connect(connect_timeout=45, **c)
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def show(t):
    print()
    print("=" * 88)
    print(t)
    print("=" * 88)


if args.rollback:
    backup = json.loads(Path(args.rollback).read_text(encoding="utf-8"))
    n = 0
    for row in backup["rows"]:
        cur.execute("UPDATE sa_news SET sentiment=%s WHERE id=%s",
                    (row["sentiment"], row["id"]))
        n += cur.rowcount
    print("已回滚 %d 行" % n)
    conn.close()
    raise SystemExit(0)

show("① 取个股专属词表")
cur.execute("SELECT code, keywords_pos, keywords_neg FROM sa_watchlist")
words = {}
for r in cur.fetchall():
    words[r["code"]] = {"pos": NF._split_keywords(r["keywords_pos"] or ""),
                        "neg": NF._split_keywords(r["keywords_neg"] or "")}
n_personal = sum(1 for v in words.values() if v["pos"] or v["neg"])
print("  %d 只自选股，其中 %d 只配了专属情绪词（其余靠通用表）"
      % (len(words), n_personal))

show("② 全量判定")
cur.execute("SELECT id, code, title, sentiment FROM sa_news ORDER BY id")
rows = cur.fetchall()
plan = []
for r in rows:
    sw = words.get(r["code"]) or {}
    new = NF.classify_sentiment(r["title"] or "", sw.get("pos"), sw.get("neg"))
    old = r["sentiment"] or ""
    if new != old:
        # 利空优先：已经是 neg 的不回退（save_to_db 也是这个语义）
        if old == "neg" and new != "neg":
            continue
        plan.append((r["id"], old, new, r["title"] or ""))

n_pos = sum(1 for _, _, n, _ in plan if n == "pos")
n_neg = sum(1 for _, _, n, _ in plan if n == "neg")
print("  总 %d 条，将变更 %d 条：利好 %d、利空 %d"
      % (len(rows), len(plan), n_pos, n_neg))
unchanged = len(rows) - len(plan)
print("  维持原状 %d 条（判不出情绪或已是利空）" % unchanged)

show("③ 变更样例（各 10 条）")
for want, label in (("neg", "判为利空"), ("pos", "判为利好")):
    print()
    print("  --- %s ---" % label)
    got = [p for p in plan if p[2] == want][:10]
    for i, (rid, old, new, title) in enumerate(got, 1):
        print("  %2d. %s" % (i, title[:74]))

show("④ 影响：35 只自选股近 7 日的情绪覆盖")
cur.execute("""SELECT w.code, w.name, count(*) tot,
       count(*) FILTER (WHERE n.sentiment='pos') pos,
       count(*) FILTER (WHERE n.sentiment='neg') neg
   FROM sa_news n JOIN sa_watchlist w ON w.code=n.code
   WHERE n.fetched_at > now() - interval '7 days'
   GROUP BY w.code, w.name ORDER BY 3 DESC, w.code LIMIT 40""")
cov = cur.fetchall()
print("  %-9s %-12s %5s %5s %5s %7s" % ("code", "name", "总数", "利好", "利空", "有效率"))
for r in cov:
    eff = (r["pos"] + r["neg"]) / r["tot"] * 100 if r["tot"] else 0
    print("  %-9s %-12s %5d %5d %5d %6.1f%%"
          % (r["code"], r["name"], r["tot"], r["pos"], r["neg"], eff))

if not args.apply:
    print()
    print("  dry-run。加 --apply 执行。")
    conn.close()
    raise SystemExit(0)

show("⑤ 执行")
# 备份必须在 commit **之前**落盘。2026-09-29 踩过：原来把写备份放在 commit
# 之后，备份那一步一崩（Decimal 无法 JSON 序列化 / RealDict 取不到键），
# 数据已经提交、脚本却以非零码退出 —— 造成「以为没生效、其实改了一半」，
# 而且没有回滚文件。
cur.execute("SELECT id, COALESCE(sentiment, '') AS sentiment FROM sa_news")
backup_rows = [{"id": r["id"], "sentiment": r["sentiment"]}
               for r in cur.fetchall()]
ts = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
out = BASE / "reports" / f"news_sentiment_{ts}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"at": ts, "rows": backup_rows}, ensure_ascii=False),
               encoding="utf-8")
print("  备份已写（commit 之前）: %s  %d 行" % (out.name, len(backup_rows)))

# 关掉 autocommit，整批放进一个事务：要么全成要么全不成
conn.autocommit = False
try:
    with conn.cursor() as c2:
        c2.executemany("UPDATE sa_news SET sentiment=%s WHERE id=%s",
                       [(new, rid) for rid, _old, new, _t in plan])
    conn.commit()
except Exception as exc:
    conn.rollback()
    print("  失败已回滚：%s" % str(exc)[:200])
    print("  可用 --rollback %s 恢复" % out.name)
    conn.close()
    raise SystemExit(1)
print("  更新 %d 行" % len(plan))

cur.execute("""SELECT COALESCE(sentiment,'(空)') s, count(*) n FROM sa_news
               GROUP BY 1 ORDER BY n DESC""")
print()
print("  最终分布:")
for r in cur.fetchall():
    print("    %-8s %5d" % (r["s"], r["n"]))
conn.close()
