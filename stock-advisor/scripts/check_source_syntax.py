# -*- coding: utf-8 -*-
"""查 HTML 实体 bug 的影响面：多少篇源码含 &gt; 这类转义。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from psycopg2 import connect                      # noqa: E402
import joinquant_source as JS                     # noqa: E402

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

conn = connect(connect_timeout=30, **DB)
conn.autocommit = True
cur = conn.cursor()
cur.execute("""SELECT s.post_id, a.title, s.code
               FROM sa_strategy_source s
               JOIN sa_strategy_article a ON a.post_id = s.post_id""")
rows = cur.fetchall()
bad = 0
print("=== 源码里的 HTML 实体（修复前的状态）===")
for pid, title, code in rows:
    ents = JS._ENTITY_RE.findall(code or "")
    if ents:
        bad += 1
        print("  %-14s %-26s %3d 处 %s" % (pid[:12], (title or "")[:26],
                                          len(ents), sorted(set(ents))[:5]))
        ex = next((l.strip() for l in (code or "").split("\n") if "&" in l), "")
        print("      例: %s" % ex[:92])
print("  -> %d/%d 篇含实体" % (bad, len(rows)))

print()
print("=== 修复后：逐篇做语法编译检查 ===")
ok = 0
for pid, title, code in rows:
    if not code:
        continue
    try:
        compile(code, "s.py", "exec")
        ok += 1
        print("  [编译通过] %-14s %-26s %d 行"
              % (pid[:12], (title or "")[:26], len(code.split("\n"))))
    except SyntaxError as exc:
        print("  [语法错误] %-14s %-26s 行%s: %s"
              % (pid[:12], (title or "")[:26], exc.lineno, exc.msg))
        for l in (code.split("\n")[:exc.lineno])[-1:]:
            print("        %s" % l.strip()[:92])
print("  -> %d/%d 篇语法正确" % (ok, len([r for r in rows if r[2]])))
conn.close()
