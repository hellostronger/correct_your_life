# -*- coding: utf-8 -*-
"""迁移：老的 3 条预置策略 -> 新的 7 条命名。

老的 3 条（保守·峰值回撤8% / 标准·固定止盈15% / 激进·分批+10%/+20%）与新增的
锁利·移动止盈8% / 锁利·固定止盈15% / 锁利·分批+10%/+20% 参数完全相同，只是改了名。
不去重的话对照表里每只票会有 6 行里 3 行是重复的。

策略表 → 绑定表 → 影子卖出表 之间是 FK ON DELETE CASCADE，所以必须**先搬再删**，
否则老策略一删，绑定和影子卖出记录会跟着一起没了。
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")
from psycopg2 import connect  # noqa: E402

B = "http://127.0.0.1:8686"

PAIRS = [
    ("保守·峰值回撤8%", "锁利·移动止盈8%"),
    ("标准·固定止盈15%", "锁利·固定止盈15%"),
    ("激进·分批+10%/+20%", "锁利·分批+10%/+20%"),
]

E = Path("..") / ".env"
c = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
     "password": "", "dbname": "postgres"}
for line in E.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" not in line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    m = {"DB_HOST": "host", "DB_PORT": "port", "DB_USERNAME": "user",
         "DB_PASSWORD": "password", "DB_DATABASE": "dbname"}
    if k.strip() in m:
        c[m[k.strip()]] = int(v) if k.strip() == "DB_PORT" else v.strip()

conn = connect(connect_timeout=45, **c)
cur = conn.cursor()


def one(sql, args=()):
    cur.execute(sql, args)
    r = cur.fetchone()
    return r[0] if r else None


print("=== 迁移前 ===")
one("SELECT count(*) FROM sa_strategies")
print("  策略 %d 条, 绑定 %d 条, 影子卖出 %d 条" % (
    one("SELECT count(*) FROM sa_strategies"),
    one("SELECT count(*) FROM sa_paper_strategy_bindings"),
    one("SELECT count(*) FROM sa_paper_strategy_exits")))

moved_bind = moved_exit = 0
for old, new in PAIRS:
    oid = one("SELECT id FROM sa_strategies WHERE name=%s", (old,))
    nid = one("SELECT id FROM sa_strategies WHERE name=%s", (new,))
    if not oid:
        print("  跳过（老策略不存在）: %s" % old)
        continue
    if not nid:
        print("  跳过（新策略不存在）: %s" % new)
        continue
    # 影子卖出先搬：它引用 binding_id，绑定 id 不变，只改 strategy_id
    cur.execute("UPDATE sa_paper_strategy_exits SET strategy_id=%s WHERE strategy_id=%s",
                (nid, oid))
    moved_exit += cur.rowcount
    # 绑定：若 (new, code) 已存在则合并（保留已推进的档位/峰值），否则直接改指向
    cur.execute("""UPDATE sa_paper_strategy_bindings b SET strategy_id=%s
                   WHERE b.strategy_id=%s AND NOT EXISTS (
                       SELECT 1 FROM sa_paper_strategy_bindings x
                       WHERE x.strategy_id=%s AND x.code=b.code)""",
                (nid, oid, nid))
    moved_bind += cur.rowcount
    # 合并掉的重复绑定：它们的影子卖出要改挂到保留的那条上
    cur.execute("""UPDATE sa_paper_strategy_exits e SET binding_id = k.id
                   FROM sa_paper_strategy_bindings k
                   WHERE e.binding_id IN (
                       SELECT b.id FROM sa_paper_strategy_bindings b
                       WHERE b.strategy_id=%s
                         AND EXISTS (SELECT 1 FROM sa_paper_strategy_bindings x
                                     WHERE x.strategy_id=%s AND x.code=b.code))
                     AND k.strategy_id=%s AND k.code IN (
                       SELECT b2.code FROM sa_paper_strategy_bindings b2
                       WHERE b2.strategy_id=%s)
                     AND k.id <> e.binding_id""",
                (oid, nid, nid, oid))
    # 删掉重复绑定，再删老策略
    cur.execute("""DELETE FROM sa_paper_strategy_bindings b
                   WHERE b.strategy_id=%s AND EXISTS (
                       SELECT 1 FROM sa_paper_strategy_bindings x
                       WHERE x.strategy_id=%s AND x.code=b.code)""",
                (oid, nid))
    cur.execute("DELETE FROM sa_strategies WHERE id=%s", (oid,))
    print("  %-22s (id=%s) -> %-22s (id=%s)  搬绑定 %d 搬影子卖出 %d" % (
        old, oid, new, nid, moved_bind, moved_exit))

conn.commit()

print("\n=== 迁移后 ===")
print("  策略 %d 条, 绑定 %d 条, 影子卖出 %d 条" % (
    one("SELECT count(*) FROM sa_strategies"),
    one("SELECT count(*) FROM sa_paper_strategy_bindings"),
    one("SELECT count(*) FROM sa_paper_strategy_exits")))
print("  重复绑定检查: %s" % one(
    """SELECT count(*) FROM (SELECT strategy_id, code FROM sa_paper_strategy_bindings
       GROUP BY strategy_id, code HAVING count(*)>1) x"""))
print("  孤儿影子卖出: %s" % one(
    """SELECT count(*) FROM sa_paper_strategy_exits e
       WHERE NOT EXISTS (SELECT 1 FROM sa_paper_strategy_bindings b WHERE b.id=e.binding_id)"""))

cur.execute("SELECT id, name, kind, target_pct, drawdown_pct FROM sa_strategies ORDER BY id")
print("\n  策略清单:")
for r in cur.fetchall():
    print("   [%s] %-30s %-9s tp=%-6s dd=%s" % r)

# 每只票应有 7 条绑定
cur.execute("""SELECT code, count(*) FROM sa_paper_strategy_bindings
               GROUP BY code ORDER BY code""")
rows = cur.fetchall()
print("\n  每票绑定数: %s" % sorted({r[1] for r in rows}))
conn.close()
