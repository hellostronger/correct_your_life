# -*- coding: utf-8 -*-
"""DDL 自检：在 init_db 之前抓出拼写错误，不让服务起不来。

为什么需要
----------
init_db 是「一条条 execute，失败就整块抛出」—— 一个拼错的类型名
（我写过 TIMESTAMPTY，少个 Z）就会让后面所有表都建不出来，而且**服务直接
起不来**（init_db 在模块末尾调用，未捕获异常直接终止进程）。
所以在开发期就该有��独立校验：按同样的拆分逻辑扫一遍，找出非 SQL 片段。

检查项
------
1. 拆分后是否有语句以 + % " ' 等非 SQL 字符开头（拼接写错时的典型症状）
2. 列定义位置的类型名是否合法（抓 TIMESTAMPTY 这类手滑）
3. 单 # 注释（PG 行注释是 --，用 # 会报 syntax error at or near "#"；
   本仓库已经踩过三次）
4. $$ 块（DO ... $$，会被 split(';') 弄坏）
5. 括号是否配平
"""
import io
import re
import sys
from collections import Counter
from pathlib import Path

VALID_TYPES = {
    "BIGSERIAL", "SERIAL", "INTEGER", "INT", "BIGINT", "SMALLINT", "TEXT",
    "VARCHAR", "CHAR", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "BOOLEAN",
    "DATE", "TIMESTAMP", "TIMESTAMPTZ", "TIME", "INTERVAL", "JSONB",
    "JSON", "UUID", "BYTEA", "BLOB",
}
KEYWORDS = {
    "CREATE", "TABLE", "INDEX", "UNIQUE", "PRIMARY", "KEY", "FOREIGN",
    "REFERENCES", "NOT", "NULL", "DEFAULT", "CHECK", "UNIQUE", "CONSTRAINT",
    "ON", "AND", "OR", "AS", "WITH", "ADD", "COLUMN", "IF", "EXISTS",
    "GENERATED", "ALWAYS", "IDENTITY", "CASCADE", "IN", "IS", "SELECT",
}


def split_statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def strip_comments(stmt: str) -> str:
    return "\n".join(l for l in stmt.splitlines()
                     if not l.strip().startswith("--")).strip()


def check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    errs: list[str] = []

    # ① 非 SQL 起始字符（拼接写错的症状）
    for i, s in enumerate(split_statements(src), 1):
        t = strip_comments(s)
        if not t:
            continue
        if t[0] in "+%\"'":
            errs.append("第 %d 条语句以 %r 开头 —— 像是拼接写错被当成了 SQL"
                        % (i, t[0]))
            errs.append("    %s" % t[:90].replace("\n", " "))

    # ② 列定义位置的类型名
    for k, l in enumerate(src.split("\n"), 1):
        m = re.match(r"^\s+(\w+)\s+([A-Z][A-Z0-9_]*)\s*(,|$|\s)", l)
        if not m:
            continue
        t = m.group(2)
        if t in VALID_TYPES or t in KEYWORDS:
            continue
        errs.append("行 %d: 列 %s 的类型 %r 不合法（合法类型：%s）"
                    % (k, m.group(1), t, ", ".join(sorted(VALID_TYPES))[:150]))

    # ③ 单 # 注释
    for k, l in enumerate(src.split("\n"), 1):
        st = l.strip()
        if st.startswith("#"):
            errs.append("行 %d: 用单 # 注释，PostgreSQL 只认 --：%s" % (k, st[:70]))

    # ④ $$ 块
    n = src.count("$$")
    if n:
        errs.append("有 %d 个 $$ —— DO $$ 块会被 init_db 的 split(';') 弄坏" % n)

    # ⑤ 括号配平
    bal = src.count("(") - src.count(")")
    if bal:
        errs.append("括号不配平：多 %d 个 '('" % bal)

    # ⑥ 统计
    stmts = [s for s in (strip_comments(x) for x in split_statements(src)) if s]
    print("  %s: %d 条语句, %d 行, %d 字符"
          % (path.name, len(stmts), len(src.split("\n")), len(src)))
    return errs


def main() -> int:
    print("=== DDL 自检 ===")
    all_errs: list[str] = []
    targets = [Path(__file__).resolve().parent / "schema_strategy_lib.sql"]
    # app.py 里的大 ddl 字符串也扫（它是 init_db 的主体）
    app = Path(__file__).resolve().parent / "app.py"
    if app.exists():
        s = app.read_text(encoding="utf-8")
        try:
            i = s.index('ddl = """')
            j = s.index('"""', i + 10)
            lib = (Path(__file__).resolve().parent
                   / "schema_strategy_lib.sql").read_text(encoding="utf-8")
            full = s[i + len('ddl = """'):j] + lib
            tmp = Path(__file__).resolve().parent / "scripts" / "_tmp_ddl_check.sql"
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_text(full, encoding="utf-8")
            found = check_file(tmp)
            if found:
                all_errs.extend(found)
            tmp.unlink()
        except Exception as exc:       # noqa: BLE001
            all_errs.append("app.py 的 ddl 提取失败: %s" % str(exc)[:100])

    for t in targets:
        if t.exists():
            found = check_file(t)
            if found:
                all_errs.extend(found)

    print()
    if all_errs:
        print("发现 %d 个问题：" % len(all_errs))
        for e in all_errs:
            print("  !! " + e)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
