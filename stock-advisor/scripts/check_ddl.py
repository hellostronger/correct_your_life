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
import ast
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

    # ① 每条语句（剥掉注释后）必须以 SQL 关键字开头
    #
    # 为什么改成「关键字白名单」而不是「首字符不是 +%\"'」
    # ------------------------------------------------
    # init_db 是 `ddl.split(";")` **逐条 execute**。所以只要注释里出现一个
    # 分号，一条 CREATE TABLE 就会被切成两半，后半截以运算符开头 ->
    # 全新部署时建表失败、服务起不来。
    # 我就在 DDL 注释里写了一句含分号的中文说明，踩了这个坑：
    # 报错是 "syntax error at or near "=""，跟「注释里有分号」毫无关联，
    # 光看报错根本联想不到。改成白名单之后能直接报出「第 N 条以 = 开头」。
    SQL_START = re.compile(
        r"^(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|SELECT|WITH|SET|TRUNCATE|"
        r"COMMENT|GRANT|REVOKE|BEGIN|COMMIT|DO|COPY|VACUUM|ANALYZE)\b",
        re.I)
    for i, s in enumerate(split_statements(src), 1):
        t = strip_comments(s)
        if not t:
            continue
        if not SQL_START.match(t):
            first = t.split()[0] if t.split() else t[:12]
            errs.append("%s 第 %d 条语句以 %r 开头，不是 SQL 关键字 —— "
                        "多半是**注释里混进了分号**把一条语句切成了两半"
                        % (name, i, first[:20]))
            errs.append("    %s" % t[:90].replace("\n", " "))

    # ② 列定义位置的类型名
    #
    # 排除以约束关键字开头的行 —— 实测踩到过：
    #     REFERENCES sa_strategy_article(post_id) ON DELETE CASCADE,
    # 这行会被当成「列名 ON、类型 DELETE」而误报类型不合法。
    # 修法是跳过这些关键字开头的行（它们是约束子句，不是列定义）。
    COL_CONSTRAINT_KW = {"ON", "PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT",
                         "CHECK", "REFERENCES", "EXCLUDE", "LIKE", "DEFERRABLE"}
    for k, l in enumerate(src.split("\n"), 1):
        m = re.match(r"^\s+(\w+)\s+([A-Z][A-Z0-9_]*)\s*(,|$|\s)", l)
        if not m:
            continue
        if m.group(1).upper() in COL_CONSTRAINT_KW:
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


def check_prompts() -> list[str]:
    """自检 LLM 提示词模板：能不能当 format 模板用、字段名齐不齐。

    为什么值得单独做一项检查
    ----------------------
    USER_TMPL 里为了输出 JSON 示例，**所有字面花括号都写成双写**（{{ }}）。
    一旦有人（或脚本）改坏一行，`.format()` 会在运行时抛
    `ValueError: Single '}' encountered in format string` ——
    而这个错只在**真调 LLM 的时候**才炸，等于每次都要花钱才发现。
    更糟的是它长得很像 LLM 配置问题，容易往错的方向排查。

    这里做两件事：① 拿一个假数据真跑一遍 format（不花钱）；
    ② 检查 normalize() 认识的键和模板里要求的键对得上。
    """
    errs: list[str] = []
    root = Path(__file__).resolve().parent.parent
    mod = root / "strategy_extract.py"
    if not mod.exists():
        return []
    import importlib
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import strategy_extract as SX
        importlib.reload(SX)
    except Exception as exc:                # noqa: BLE001
        return ["strategy_extract 导入失败：%s" % str(exc)[:160]]

    art = {"title": "T", "author": "A", "published_at": None,
           "reply_count": 1, "like_count": 2, "clone_count": 3,
           "content_text": "正文"}
    for label, src in (("有源码", {"code": "x = 1", "n_blocks": 1,
                                  "raw_lines": 1, "redacted": False,
                                  "stub_sites": 0, "stub_reasons": []}),
                       ("脱敏", {"code": "def f():\n    ...", "n_blocks": 1,
                               "raw_lines": 2, "redacted": True,
                               "stub_sites": 1, "stub_reasons": ["省略"]}),
                       ("无源码", None)):
        try:
            SX.build_prompt(art, src, [])
        except Exception as exc:            # noqa: BLE001
            errs.append("build_prompt(%s) 失败：%s -> 提示词模板的花括号"
                        "或占位符坏了（字面花括号必须写成 {{ }}）"
                        % (label, str(exc)[:140]))
    # 提示词里要求的段名，必须都在解析器的字典里
    # （少一个就是「模型填了但我们读不到」，而且不报错，静默丢字段）
    try:
        import strategy_sections as SS
        want = set(re.findall(r"^###([A-Z_]{2,20})$", SX.SYSTEM, re.M))
        miss = sorted(want - set(SS.SECTION_KEYS))
        if miss:
            errs.append("提示词要求了这些段但解析器不认识，字段会被静默丢掉：%s"
                        % ", ".join("###" + m for m in miss))
        # 拿一段假输出走一遍，确认解析器本身没坏
        probe_txt = ("###TITLE\n测试策略\n###SUMMARY\n一二三。\n"
                     "###TYPE\n打板\n"
                     "###STEP\n1 | 选股 | 低位三连阳 | 正文原句\n"
                     "2 | 卖出 | 尾盘 14.1% | get_close_sell\n"
                     "###PARAM\n持股数: 10\n调仓周期: 每周五\n"
                     "###PERF\n年化: 14.1%\n最大回撤: -20%\n"
                     "###APPLICABLE\n- 牛市主升\n###RISK\n- 单票集中度\n"
                     "###UNCERTAINTY\n无\n###RESEARCH\nfalse\n"
                     "###SCORE\n4 | 源码完整，只需改写 API\n")
        d = SS.parse_sections(probe_txt)
        n = SX.normalize(d)
        if len(n["steps"]) != 2 or not n["params"] \
                or n["portable_score"] != 4 or not n["applicable"]:
            errs.append("分段解析器自检没过：steps=%d params=%d score=%s "
                        "applicable=%d"
                        % (len(n["steps"]), len(n["params"]),
                           n["portable_score"], len(n["applicable"])))
    except Exception as exc:                # noqa: BLE001
        errs.append("分段格式解析自检失败：%s" % str(exc)[:140])
    print("  提示词模板: %s" % ("通过" if not errs else "有问题"))
    return errs
    print("  提示词模板: %s" % ("通过" if not errs else "有问题"))
    return errs


def check_sql_params() -> list[str]:
    """扫 cur.execute(sql, (参数...))，用 ast 精确比对 %s 个数与参数个数。

    为什么值得做
    ------------
    psycopg2 在占位符比参数多时报的是
        IndexError: tuple index out of range
    ——**跟 SQL、跟数据库、跟数据内容全都无关**，纯看代码根本猜不出是哪句。
    我在 save_digest 上踩过一次：INSERT 20 列 20 参数，但
    `ON CONFLICT ... DO UPDATE SET raw_response=%s` 又多了一个占位符。
    而这个错只在**真的抽完一篇要落库**时才炸 —— 每修一个 bug 都要烧一次
    LLM 才能发现（一次 140 秒 + 一万 token）。

    **用 ast 而不是正则数逗号**：第一版用正则 + 缩进猜，报 11 条里 10 条是
    误报。一个天天误报的自检等于没有自检 —— 它只会训练人忽略它的输出。
    ast 数的是真实 tuple 的元素个数，多行、嵌套、表达式都能算对。
    """
    errs: list[str] = []
    root = Path(__file__).resolve().parent.parent
    targets = ["strategy_extract.py", "strategy_crawler.py",
               "joinquant_source.py", "backtest.py", "market_data.py",
               "stock_discovery.py", "stock_roster.py", "paper_strategy.py"]
    checked = 0
    for name in targets:
        f = root / name
        if not f.exists():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            errs.append("%s 语法错误：%s" % (name, exc))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "execute"):
                continue
            sql_node, arg_node = node.args[0], node.args[1]
            if isinstance(sql_node, ast.JoinedStr):
                continue                      # f-string 拼的，静态数不了
            if not (isinstance(sql_node, ast.Constant)
                    and isinstance(sql_node.value, str)):
                continue
            sql = sql_node.value
            # **不要剥 SQL 注释再数** —— psycopg2 的插值器不认识 `--` 注释，
            # 注释里的占位符字面量会被当成真的占位符，然后报
            # IndexError: tuple index out of range。所以这里必须连注释一起数。
            # （我曾反过来「修」过一次：把注释剥掉再数，结果自检说没问题，
            #   运行时照样炸。方向反了。）
            n_ph = sql.count("%s")
            # 单独提醒：注释里写占位符字面量
            for ln in sql.split("\n"):
                cm = re.match(r"\s*--(.*)$", ln)
                if cm and ("%s" in cm.group(1) or "%(" in cm.group(1)):
                    errs.append("%s:%d SQL 注释里出现了占位符字面量 —— "
                                "psycopg2 不认识注释，会当成真占位符，"
                                "报 tuple index out of range"
                                % (name, node.lineno))
                    break
            if "%(" in sql:
                continue                      # 具名占位符，另算
            if not isinstance(arg_node, (ast.Tuple, ast.List)):
                continue
            n_args = len(arg_node.elts)
            checked += 1
            if n_ph != n_args:
                errs.append("%s:%d 占位符 %d 个但参数 %d 个 -> %s"
                            % (name, node.lineno, n_ph, n_args,
                               "少了参数（psycopg2 只会报 tuple index out of "
                               "range，跟 SQL 内容毫无关联，极难查）"
                               if n_ph > n_args else "多了参数"))
    print("  SQL 占位符比对: 检查 %d 处 execute，%s"
          % (checked, "通过" if not errs else "发现 %d 处" % len(errs)))
    return errs



def check_insert_columns() -> list[str]:
    """比对 `INSERT INTO t (a,b,c) ... ON CONFLICT DO UPDATE SET x=...`
    里出现的列，跟 DDL 声明的列是否一致（缺列 = 数据静默丢进默认值）。

    为什么需要
    ----------
    我给 sa_strategy_digest 加了 uncertainty / needs_research /
    research_hint 三列（因为抽取器要标「这份抽取可不可信」），**DDL 加了、
    代码里 normalize 也产出了，但忘了写进 INSERT**。结果：
      - 抽取接口返回 needs_research=true（用的是内存里的值，看着一切正常）
      - 列表接口返回 needs_research=false（读的是库里的默认值 FALSE）
    两边对不上，而且**不报任何错**。这种「加了字段但忘了落库」的静默丢失，
    光看代码和看接口返回值都发现不了。
    """
    errs: list[str] = []
    root = Path(__file__).resolve().parent.parent
    lib = (root / "schema_strategy_lib.sql").read_text(encoding="utf-8")
    app = (root / "app.py").read_text(encoding="utf-8")
    declared = parse_declared_columns(app[app.index('ddl = """'):]
                                       + lib)
    ddl_cols = declared.get("sa_strategy_digest", set())
    f = root / "strategy_extract.py"
    if not f.exists() or not ddl_cols:
        return []
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args):
            continue
        s = node.args[0]
        if not (isinstance(s, ast.Constant) and isinstance(s.value, str)):
            continue
        m = re.search(r"INSERT\s+INTO\s+sa_strategy_digest\s*\((.*?)\)",
                      s.value, re.S | re.I)
        if not m:
            continue
        used = {c.strip().lower() for c in m.group(1).replace("\n", " ").split(",")
                if c.strip()}
        # 只有「在 DO UPDATE 里被 now() 自动填」的列才算没漏 ——
        # 判定不能放宽成「SET 子句里出现过就算」。放宽过一次就漏报了：
        # 把 uncertainty 从 INSERT 拿掉（但 UPDATE SET 里还留着它），
        # 检查器说通过，实际上 INSERT 走的是 DDL 的默认值 ''，
        # 而 DO UPDATE 在插入时根本不执行 —— 数据静默丢成空串。
        AUTO = ("extract_at", "created_at", "updated_at")
        tail = s.value[m.end():]
        um = re.search(r"DO\s+UPDATE\s+SET(.*?)(?:WHERE|RETURNING|$)",
                       tail, re.S | re.I)
        if um:
            for c in AUTO:
                if re.search(r"\b%s\s*=\s*now\(\)" % c, um.group(1), re.I):
                    used.add(c)
        missing = sorted(ddl_cols - used)
        if missing:
            errs.append("strategy_extract.py:%d INSERT 少写了这些列，"
                        "它们会静默落成默认值：%s"
                        % (node.lineno, ", ".join(missing)))
    print("  INSERT 列比对: %s"
          % ("通过" if not errs else "发现 %d 处" % len(errs)))
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

    # ⑦ 提示词模板自检（不花钱，只验 format 能不能跑通）
    all_errs.extend(check_prompts())
    # ⑧ SQL 占位符 vs 参数个数
    all_errs.extend(check_sql_params())
    # ⑨ INSERT 列 vs DDL 列（漏写 = 静默丢数据）
    all_errs.extend(check_insert_columns())

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
