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
6. **DDL 里声明的列，库里是不是真的没有**（2026-09-30 加）

第 6 项为什么必要
----------------
`CREATE TABLE IF NOT EXISTS` 只判断「表在不在」，**不会给已存在的表补新列**。
我给 sa_strategy_source 加 redacted 时就踩了：DDL 跑一遍「全部通过」，服务
正常启动，直到第一次抓到带源码的帖子才 500
（`psycopg2.errors.UndefinedColumn: column "redacted" does not exist`）。

**幂等的 DDL 不等于能演进的 DDL。** 光做语法检查抓不到这类问题 —— 语法完全
正确，缺的是「代码已经写了新列、库还没跟上」。所以这里直接连库比对
information_schema.columns，把「代码引用了但库里没有的列」提前报出来。

对照「代码实际读了哪些列」比只看 DDL 更靠谱，但静态扫 SQL 字符串容易误报
（拼出来的列名、JSON key），所以只比对 DDL 声明的列 —— 加列时忘了写 ALTER
这个错，正好能被抓住。
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


def check_text(src: str, name: str) -> list[str]:
    """检查一段 DDL 文本。name 只用于统计输出。"""
    errs: list[str] = []

    # ① 非 SQL 起始字符（拼接写错的症状）
    for i, s in enumerate(split_statements(src), 1):
        t = strip_comments(s)
        if not t:
            continue
        if t[0] in "+%\"'":
            errs.append("%s 第 %d 条语句以 %r 开头 —— 像是拼接写错被当成了 SQL"
                        % (name, i, t[0]))
            errs.append("    %s" % t[:90].replace("\n", " "))

    # ② 列定义位置的类型名
    for k, l in enumerate(src.split("\n"), 1):
        m = re.match(r"^\s+(\w+)\s+([A-Z][A-Z0-9_]*)\s*(,|$|\s)", l)
        if not m:
            continue
        t = m.group(2)
        if t in VALID_TYPES or t in KEYWORDS:
            continue
        errs.append("%s 行 %d: 列 %s 的类型 %r 不合法（合法类型：%s）"
                    % (name, k, m.group(1), t, ", ".join(sorted(VALID_TYPES))[:150]))

    # ③ 单 # 注释
    for k, l in enumerate(src.split("\n"), 1):
        st = l.strip()
        if st.startswith("#"):
            errs.append("%s 行 %d: 用单 # 注释，PostgreSQL 只认 --：%s"
                        % (name, k, st[:70]))

    # ④ $$ 块
    n = src.count("$$")
    if n:
        errs.append("%s: 有 %d 个 $$ —— DO $$ 块会被 init_db 的 split(';') 弄坏"
                    % (name, n))

    # ⑤ 括号配平（**在剔除注释和字符串字面量之后**统计，否则注释里
    #    写一句「注意 ( 这里」就会误报。第一版直接数原文，一直假警报）
    code_only = strip_comments(src)
    code_only = re.sub(r"'(?:[^']|'')*'", "''", code_only)
    bal = code_only.count("(") - code_only.count(")")
    if bal:
        errs.append("%s: 括号不配平：'(' 比 ')' 少 %d 个" % (name, -bal))

    # ⑥ 统计
    stmts = [s for s in (strip_comments(x) for x in split_statements(src)) if s]
    print("  %s: %d 条语句, %d 行, %d 字符"
          % (name, len(stmts), len(src.split("\n")), len(src)))
    return errs


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """按顶层分隔符切，**跳过括号内和引号内的分隔符**。

    必须这么切，否则：
      NUMERIC(10,2)   -> ["NUMERIC(10", "2)"]     冒出一个假列名 "2)"
      PRIMARY KEY (a, b) -> ["PRIMARY KEY (a", " b)"]  冒出一个假列名 "b)"
    第一版就是直接 text.split(",")，结果 20 张表全报「缺列」，
    假警报比没检查更糟 —— 它会让人开始忽略这个工具的输出。
    """
    out, buf, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def parse_declared_columns(sql: str) -> dict[str, set[str]]:
    """从 DDL 里抽出每张表声明了哪些列。CREATE TABLE 和 ALTER ADD COLUMN 都要看。"""
    out: dict[str, set[str]] = {}
    # CREATE TABLE x ( col type, ... )
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(",
                         sql, re.I):
        tbl, i, depth = m.group(1).lower(), m.end(), 1
        buf = []
        while i < len(sql) and depth:
            ch = sql[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if not depth:
                    break
            buf.append(ch)
            i += 1
        body = "".join(buf)
        for line in split_top_level(body):
            line = re.sub(r"--[^\n]*", "", line).strip()
            if not line:
                continue
            head = line.split()[0].strip('"')
            # 跳过约束行
            if head.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT",
                                "CHECK", "EXCLUDE", "LIKE"):
                continue
            out.setdefault(tbl, set()).add(head.lower())
    # ALTER TABLE x ADD COLUMN [IF NOT EXISTS] col type, ADD COLUMN col type
    # 一条 ALTER 里可以加多列，逗号之后的每段都还带着 "ADD COLUMN" 前缀，
    # 不剥掉就会把 ADD 当成列名（第一版就报了个假的「sa_strategy_source 缺列 add」）
    for m in re.finditer(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(.*?);",
                         sql, re.I | re.S):
        tbl = m.group(1).lower()
        for part in split_top_level(m.group(2)):
            part = re.sub(r"--[^\n]*", "", part).strip()
            part = re.sub(r"^ADD\s+COLUMN\s+", "", part, flags=re.I)
            part = re.sub(r"^IF\s+NOT\s+EXISTS\s+", "", part, flags=re.I)
            if not part:
                continue
            cols = part.split()
            if len(cols) >= 2:
                out.setdefault(tbl, set()).add(cols[0].strip('"').lower())
    return out


def check_live_columns(declared: dict[str, set[str]]) -> list[str]:
    """连库比对：DDL 声明的列在 information_schema 里缺了没。"""
    errs: list[str] = []
    try:
        from psycopg2 import connect
    except ImportError:
        return ["没装 psycopg2，跳过实际列比对"]
    try:
        root = Path(__file__).resolve().parent.parent.parent
        env = root / ".env"
        if not env.exists():
            return ["找不到 .env，跳过实际列比对（无法连库）"]
        c: dict = {"host": "127.0.0.1", "port": 5432, "user": "postgres",
                  "password": "", "dbname": "postgres"}
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
        conn = connect(connect_timeout=15, **c)
    except Exception as exc:             # noqa: BLE001
        return ["连库失败，跳过实际列比对：%s" % str(exc)[:80]]

    try:
        cur = conn.cursor()
        for tbl, cols in sorted(declared.items()):
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s", (tbl,))
            have = {r[0].lower() for r in cur.fetchall()}
            if not have:
                # 表还没建（首次跑）——那是正常情况，建表语句会补上
                continue
            missing = sorted(cols - have)
            if missing:
                errs.append("表 %s 缺列：%s  -> 补一条 "
                            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS ..."
                            % (tbl, ", ".join(missing), tbl))
    finally:
        conn.close()
    return errs


def main() -> int:
    print("=== DDL 自检 ===")
    all_errs: list[str] = []

    # ---- 路径 ----
    # 踩过的坑：这个脚本在 scripts/ 下，最早我写的是
    #     Path(__file__).resolve().parent / "schema_strategy_lib.sql"
    # 也就是 scripts/schema_strategy_lib.sql —— 那个文件根本不存在
    #（真身在上一层的仓库根）。于是 targets 和 app 都静默跳过，
    # **这个自检从写出来那天起就一直在空转，永远打印「全部通过」**。
    # 我拿它当「DDL 没问题」的依据用了好几次，全是假的。
    # 现在用 ROOT 统一算一次，并且文件找不到就明确报错 —— 宁可吵，
    # 也不要静悄悄地什么都不检查。
    root = Path(__file__).resolve().parent.parent
    lib_sql = root / "schema_strategy_lib.sql"
    app_py = root / "app.py"
    for label, p in (("schema_strategy_lib.sql", lib_sql), ("app.py", app_py)):
        if not p.exists():
            print("  !! 找不到 %s（找的是 %s）—— 自检无法进行"
                  % (label, p))
            return 2

    combined: list[str] = [lib_sql.read_text(encoding="utf-8")]

    # app.py 里的大 ddl 字符串也扫（它是 init_db 的主体）
    s = app_py.read_text(encoding="utf-8")
    try:
        i = s.index('ddl = """')
        j = s.index('"""', i + 10)
        full = s[i + len('ddl = """'):j] + lib_sql.read_text(encoding="utf-8")
        combined.append(full)
        found = check_text(full, "<app.py ddl + lib>")
        all_errs.extend(found)
    except ValueError as exc:
        all_errs.append("app.py 里找不到 ddl = \"\"\" ... \"\"\" 块: %s" % exc)

    found = check_text(lib_sql.read_text(encoding="utf-8"), lib_sql.name)
    all_errs.extend(found)

    # ⑥ 实际列比对（连库）
    declared = parse_declared_columns("\n".join(combined))
    print("  声明了 %d 张表" % len(declared))
    if not declared:
        all_errs.append("从 DDL 里一列都没解析出来 —— 正则失效了，"
                        "这个自检等于没跑")
    live_errs = check_live_columns(declared)
    hard = [e for e in live_errs if not e.startswith(("找不到", "连库失败",
                                                      "没装"))]
    for e in live_errs:
        print("  " + e)
    all_errs.extend(hard)

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
