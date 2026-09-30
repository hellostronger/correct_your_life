# -*- coding: utf-8 -*-
"""列出某篇策略用到的全部聚宽 API 调用，以及我实现了哪些。"""
import ast
import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import jq_api                                       # noqa: E402
from psycopg2 import connect                        # noqa: E402

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
cur.execute("""SELECT a.title, s.code FROM sa_strategy_source s
               JOIN sa_strategy_article a ON a.post_id=s.post_id
               WHERE s.syntax_ok ORDER BY s.lines DESC LIMIT 1""")
title, code = cur.fetchone()
conn.close()

implemented = set(jq_api.api_names())
# 类也算
for nm in dir(jq_api):
    if not nm.startswith("_"):
        implemented.add(nm)

print("=== 策略：%s ===" % title)

# 扫出所有 Name/Attribute 调用
calls = {}
tree = ast.parse(code)
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        f = node.func
        name = None
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            base = f.value
            name = (base.id + "." + f.attr) if isinstance(base, ast.Name) \
                else f.attr
        if name:
            calls.setdefault(name, []).append(
                (node.lineno, [ast.unparse(a) for a in node.args][:6],
                 [k.arg for k in node.keywords if k.arg]))

# 排除纯 Python 内置
BUILTIN = {"print", "len", "range", "int", "float", "str", "list", "dict",
           "set", "sorted", "max", "min", "abs", "sum", "round", "isinstance",
           "enumerate", "zip", "bool", "any", "all", "tuple", "map", "filter",
           "reversed", "type", "getattr", "setattr", "hasattr", "repr", "id"}
print()
print("=== 用到的调用 vs 我的实现 ===")
missing = []
for name in sorted(calls):
    top = name.split(".")[-1]
    if name in BUILTIN or top in BUILTIN:
        continue
    ok = name in implemented or top in implemented
    lines = calls[name]
    args = lines[0][1]
    kws = lines[0][2]
    sig = ""
    obj = getattr(jq_api, top, None)
    if obj is not None and callable(obj):
        try:
            sig = str(inspect.signature(obj))
        except (TypeError, ValueError):
            sig = "?"
    flag = "OK " if ok else "缺!"
    print("  %s %-26s x%-3d 行%s %s"
          % (flag, name, len(lines), lines[0][0],
             ("实参=%s 关键字=%s" % (args[:3], kws)) if (args or kws) else ""))
    if not ok:
        missing.append(name)
    if ok and (args or kws):
        print("       我的签名: %s" % sig[:110])

print()
print("=== 缺失的 API：%d 个 ===" % len(missing))
for m in missing:
    print("  " + m)

print()
print("=== 属性访问（context.xxx / df.xxx 不管，看 g.xxx 和裸变量）===")
attrs = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name) and base.id in ("context", "g"):
            attrs.add(base.id + "." + node.attr)
print("  " + ", ".join(sorted(attrs)) or "  无")
