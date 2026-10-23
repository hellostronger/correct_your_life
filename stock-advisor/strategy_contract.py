# -*- coding: utf-8 -*-
"""策略契约 v1 —— 所有策略代码的唯一准入标准。

为什么需要「契约」
------------------
策略库里有三种来源（聚宽原文、LLM 重写、人工手写），过去每加一条策略都要在
运行器里为它做特例适配，于是「能不能跑」取决于我记得哪些坑，而不是代码本身。
这份契约把「什么样的代码一定能被沙箱跑起来」写成可机器判定的规则：
符合 → 直接可跑；不符合 → 确定的错误码，不再靠猜。

三层契约
--------
① 结构契约（C1~C7）：代码必须长成沙箱能驱动的形状（见 CONTRACT_RULES）
② 能力契约：只能调用沙箱真实实现了的 API —— 清单**从 jq_api.py 源码 AST 派生**，
   不是手抄。绝不调用 `jq_api.api_names()`：它内部 `setattr(builtins, ...)`
   会把 56 个 API 注入 app 进程的全局命名空间（有副作用）。
③ 结论契约：只取最后一个交易日、按实际成交算权重 —— 由 strategy_daily 实现。

前两层这里静态判定，第三层运行时保证。

本模块不 import app / jq_api / jq_sandbox（纯标准库 + 文件读取），
可以被任何一侧安全引用。
"""
import ast
import builtins
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

CONTRACT_VERSION = "v1"

# ---------------------------------------------------------------------------
# ① 沙箱形状常量（与 sandbox_runner.main 的执行顺序对齐）
# ---------------------------------------------------------------------------

# 沙箱固定回调：先于 run_daily 注册的函数执行；initialize 只在第一天调一次。
FIXED_CB = ("before_trading_start", "market_open", "after_trading_end",
            "market_close", "handle_data")

ORDER_FUNCS = ("order", "order_value", "order_target",
               "order_target_value", "order_target_percent")

# 沙箱只驱动 scheduled["daily"]。weekly/monthly 注册进去但从不会被执行，
# 所以「只用它们」的策略在这里判 infeasible，而不是让它空跑出一个直线净值。
REBALANCE_FUNCS = ("run_daily", "run_weekly", "run_monthly")

# ---------------------------------------------------------------------------
# ② import 策略
# ---------------------------------------------------------------------------

# 纯计算类标准库/三方库：沙箱镜像里都有（jq_api 自己就依赖 numpy/pandas）。
ALLOWED_IMPORTS = {
    "math", "numpy", "pandas", "re", "json", "collections", "itertools",
    "functools", "statistics", "decimal", "bisect", "heapq", "copy",
    "__future__",
}

# 沙箱里**不存在**这两个模块，但它们的名字几乎都是已注入的 API
# （get_price / get_fundamentals / query …），所以整行改写成 pass 即可。
STRIPPABLE_MODULES = {"jqdata", "jqfactor"}

# datetime 不是「随便导入」的：沙箱注入的是 _SimClock 实例，策略拿到的时间
# 必须是模拟时间，否则同一天跑两次会得到不同结论（不可复现）。
DATETIME_MODULE = "datetime"
# from datetime import X 的单别名改写成什么：
DATETIME_REWRITE = {
    "datetime": "pass",                                # datetime.now() → 模拟时间
    "timedelta": "timedelta = datetime.timedelta",
    "date": "date = datetime.date",
}

BANNED_IMPORTS = {
    "subprocess", "socket", "shutil", "ctypes", "multiprocessing",
    "urllib", "urllib2", "http", "ftplib", "smtplib", "telnetlib",
    "paramiko", "requests", "aiohttp", "pty", "signal", "resource",
    "sysconfig", "imp", "importlib", "os", "sys", "time", "random",
    "threading", "asyncio", "pathlib", "glob", "tempfile", "pickle",
    "marshal", "gc", "inspect", "logging", "builtins",
}

# 破坏隔离或不可复现的内建函数。open 在 --read-only + 无网络下本来也用不了，
# 提前给一条明确的错误，比让它在容器里抛 FileNotFoundError 好定位。
BANNED_BUILTINS = {
    "open", "exec", "eval", "compile", "input", "breakpoint",
    "__import__", "globals",
}

# 这些属性链拿到的是**真实系统时间**（_SimClock.__getattr__ 把类本身转发给
# 真实 datetime 模块），会让「同一天跑两次结论不同」。
BANNED_DATETIME_CHAINS = (
    ("datetime", "date", "today"),
    ("datetime", "datetime", "now"),
    ("datetime", "datetime", "today"),
    ("datetime", "datetime", "utcnow"),
)
BANNED_DATETIME_CHAIN_IF_DATE = (("date", "today"),)

# jq_api 里属于「内部实现」的公开名，不算 API（与 _add_builtins 的排除集对齐）。
_INTERNAL_NAMES = {
    "setup", "api_names", "JQ", "FIELDS", "PRICE_FIELDS",
    "Portfolio", "Context", "JQError", "code_blocks",
}
# _add_builtins 显式跳过的导入名（它们出现在 globals 里但不是 API）。
_IMPORT_BOUND_SKIP = {"pd", "np", "math", "sys", "traceback",
                      "defaultdict", "_dt"}

# ---------------------------------------------------------------------------
# ③ 契约条文（UI 展示 / 给 LLM 的生成提示共用同一份事实）
# ---------------------------------------------------------------------------

CONTRACT_RULES = (
    ("C1", "必须有模块级 `def initialize(context)`：聚宽靠它注册回调，"
           "缺了等于空跑，净值是一条直线且不报错。"),
    ("C2", "必须有沙箱能驱动的入口：`run_daily(顶层具名函数, time='HH:MM')` "
           "或 `before_trading_start` / `handle_data` 等固定回调。"
           "**不能传 lambda、字符串、嵌套函数** —— runner 按 `__name__` "
           "去模块 globals 里取函数，取不到就静默跳过。"),
    ("C3", "import 只允许纯计算库（math / numpy / pandas / re / json / "
           "collections / itertools / functools / statistics / decimal / "
           "bisect / heapq / copy）。`from jqdata import *` 会自动改写。"),
    ("C4", "禁止 open / exec / eval / compile / __import__ / globals。"),
    ("C5", "不能有占位函数：入口函数体不能只有 pass / ... / "
           "raise NotImplementedError。"),
    ("C6", "必须至少调用一次下单函数（order / order_value / order_target / "
           "order_target_value / order_target_percent）。只算指标不下单的"
           "代码不是策略。"),
    ("C7", "只能调用沙箱真实实现了的 API。清单从 jq_api.py 源码派生，"
           "调用未实现的直接判 infeasible。"),
)

CONTRACT_NOTES = (
    "handle_data 每天收盘后跑一次，没有盘中时点语义（打板/竞价类不可实现）。",
    "run_daily 的 time 只是标签，沙箱按日线执行，不模拟盘中成交时点。",
    "initialize 只在第一个交易日调用一次，之后不再调用。",
    "时间一律用 `datetime.now()` / `datetime.today()`（模拟时间）；"
    "`datetime.date.today()` 与 `datetime.datetime.now()` 拿到的是真实系统时间，"
    "被禁止。",
    "order_target 的 amount 参数是**目标持仓股数**，不是目标市值"
    "（与聚宽同名不同义，按 sandbox 实现为准）。",
    "沙箱初始资金 1,000,000 —— 下单股数不代表用户账户，结论层按**权重**换算。",
)

# ---------------------------------------------------------------------------
# ④ 能力清单：从 jq_api.py 源码 AST 派生
# ---------------------------------------------------------------------------

_API_CACHE = None


def _strip_docstring(body: list) -> list:
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _raise_name(node: ast.Raise) -> str:
    exc = node.exc
    if isinstance(exc, ast.Name):
        return exc.id
    if isinstance(exc, ast.Call):
        f = exc.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
    return ""


def _is_pure_stub(fn) -> bool:
    """body 去掉 docstring 后只剩一条 raise，且整函数没有任何 return。

    判据必须排除「有分支的写操作 + 末尾 raise」这种真实实现 ——
    典型例子是 `get_extras`：首条是赋值、字段不支持时才 raise，
    把它当 stub 会把一个可用 API 误判成不可用（曾经就判错过）。
    """
    body = _strip_docstring(fn.body)
    if len(body) != 1 or not isinstance(body[0], ast.Raise):
        return False
    if any(isinstance(n, ast.Return) for n in ast.walk(fn)):
        return False
    name = _raise_name(body[0])
    if name == "NotImplementedError":
        return True
    return "未实现" in ast.dump(body[0])


def api_capability() -> dict:
    """{'declared': set, 'available': set, 'unavailable': {name: {line, reason}},
        'partial': {name: reason}}

    每次进程只解析一次 jq_api.py（约 1400 行，毫秒级）。**不调用**
    `jq_api.api_names()` —— 那个函数会把 56 个 API 注入当前进程的内建命名空间。
    """
    global _API_CACHE
    if _API_CACHE is not None:
        return _API_CACHE

    src_path = BASE_DIR / "jq_api.py"
    if not src_path.exists():
        raise RuntimeError("找不到 %s，无法派生沙箱 API 清单" % src_path)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    funcs, classes, assigns = [], set(), set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(node)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigns.add(t.id)
        elif isinstance(node, ast.AnnAssign) and \
                isinstance(node.target, ast.Name):
            assigns.add(node.target.id)

    internal = _INTERNAL_NAMES | _IMPORT_BOUND_SKIP

    def pub(name):
        return not name.startswith("_") and name not in internal

    fn_names = {f.name for f in funcs if pub(f.name)}
    declared = fn_names | {c for c in classes if pub(c)} | \
        {a for a in assigns if pub(a)}

    unavailable = {}
    for f in funcs:
        if f.name in fn_names and _is_pure_stub(f):
            unavailable[f.name] = {"line": f.lineno,
                                   "reason": "函数体只有 raise（未实现）"}
    available = declared - set(unavailable)

    # get_extras 是「部分支持」：只认 is_st / paused / is_halted，
    # 其它字段抛错。它不是 stub，所以留在 available 里单独标注。
    partial = {}
    if "get_extras" in available:
        partial["get_extras"] = "只支持 is_st / paused / is_halted"

    _API_CACHE = {
        "declared": declared,
        "available": available,
        "unavailable": unavailable,
        "partial": partial,
        "source": str(src_path),
    }
    return _API_CACHE


def _wrap(names, width=72) -> str:
    out, line = [], ""
    for n in names:
        piece = (n + ", ")
        if len(line) + len(piece) > width and line:
            out.append(line.rstrip())
            line = ""
        line += piece
    if line:
        out.append(line.rstrip().rstrip(","))
    return "\n".join(out)


def api_text() -> str:
    """人类可读的能力清单（页面展示用）。"""
    cap = api_capability()
    lines = ["可用 API（%d 个）" % len(cap["available"]),
             _wrap(sorted(cap["available"])),
             "",
             "未实现（调用即判 infeasible，%d 个）：%s"
             % (len(cap["unavailable"]), "、".join(sorted(cap["unavailable"])))]
    if cap["partial"]:
        lines.append("部分支持：" + "；".join(
            "%s（%s）" % (k, v) for k, v in sorted(cap["partial"].items())))
    return "\n".join(lines)


def contract_text() -> str:
    """契约全文（UI 展示用）。"""
    rules = "\n".join("  %s %s" % (k, v) for k, v in CONTRACT_RULES)
    notes = "\n".join("  · " + x for x in CONTRACT_NOTES)
    return ("策略契约 %s\n%s\n\n口径须知：\n%s\n\n%s"
            % (CONTRACT_VERSION, rules, notes, api_text()))


def contract_prompt() -> str:
    """给 LLM 的生成提示（strategy_implement 用）。"""
    cap = api_capability()
    rules = "\n".join("- %s %s" % (k, v) for k, v in CONTRACT_RULES)
    notes = "\n".join("- " + x for x in CONTRACT_NOTES)
    return (
        "【必须遵守的策略契约 %s】违反任意一条这份代码都会被拒绝执行。\n"
        "%s\n\n"
        "【口径须知】\n%s\n\n"
        "【沙箱可用的 API（只有这些，其余一律不存在）】\n%s\n"
        "【沙箱未实现的 API，绝对不要使用】\n%s\n"
        % (CONTRACT_VERSION, rules, notes,
           _wrap(sorted(cap["available"]), 88),
           "、".join(sorted(cap["unavailable"])))
    )


# ---------------------------------------------------------------------------
# ⑤ 静态检查
# ---------------------------------------------------------------------------

def _defined_names(tree) -> set:
    """代码自己定义/绑定的名字（函数、参数、赋值、导入、异常名…）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def _module_scope_defs(tree) -> dict:
    """模块作用域里定义的函数/类（含 if / try / for 里定义但要绑到模块的）。

    嵌套在函数体内的 def **不算** —— runner 按名字从模块 globals 取函数，
    嵌套函数不在里面，run_daily 传它会被静默跳过。
    """
    out = {}

    def walk_body(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
                out[n.name] = n
            elif isinstance(n, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                walk_body(n.body)
                for h in getattr(n, "handlers", []) or []:
                    walk_body(h.body)
                walk_body(getattr(n, "orelse", []) or [])
                walk_body(getattr(n, "finalbody", []) or [])

    walk_body(tree.body)
    return out


def _attr_chain(node) -> tuple:
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return tuple(reversed(parts))
    return ()


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _iter_imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def _line_indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def strip_unsupported_imports(code: str, strip_lines: dict) -> str:
    """按 {行号: 替换文本} 原地改写（缩进沿用原行）。

    为什么是 `pass` 而不是删掉整行：import 可能是 `try:` / `if:` 里唯一
    的语句，删了会变成空块 → 语法错误，而且报错位置与真因无关。
    """
    if not strip_lines:
        return code
    lines = code.split("\n")
    for lineno, repl in strip_lines.items():
        i = int(lineno) - 1
        if 0 <= i < len(lines):
            lines[i] = _line_indent(lines[i]) + repl
    return "\n".join(lines)


def check_contract(code: str) -> dict:
    """按契约 v1 静态判定一段策略代码。

    返回::

        {"contract_version", "ok", "status", "syntax_ok", "n_lines",
         "errors":[{"code","detail"}], "warnings":[{"code","detail"}],
         "notes":[str], "has_initialize", "entry":[{kind,name,time}],
         "uses_order", "api_calls":[str], "unavailable_apis":[str],
         "api_errors":[str], "strip_lines":{lineno: repl}, "risks":[...]}

    status ∈ syntax_error | contract_failed | infeasible | ok
    """
    res = {
        "contract_version": CONTRACT_VERSION,
        "ok": False, "status": "syntax_error",
        "syntax_ok": False, "n_lines": 0,
        "errors": [], "warnings": [], "notes": [],
        "has_initialize": False, "entry": [], "uses_order": False,
        "api_calls": [], "unavailable_apis": [], "api_errors": [],
        "strip_lines": {}, "risks": [],
    }

    def err(code, detail):
        res["errors"].append({"code": code, "detail": detail})

    def warn(code, detail):
        res["warnings"].append({"code": code, "detail": detail})

    if not code or not code.strip():
        err("E_SYNTAX", "代码为空")
        return _finish(res)

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        err("E_SYNTAX", "line %s: %s" % (exc.lineno, exc.msg))
        return _finish(res)

    res["syntax_ok"] = True
    res["n_lines"] = len(code.split("\n"))
    lines = code.split("\n")
    mod_defs = _module_scope_defs(tree)

    # ---------------- C1 initialize ----------------
    init = mod_defs.get("initialize")
    res["has_initialize"] = isinstance(init, (ast.FunctionDef,
                                             ast.AsyncFunctionDef))
    if not res["has_initialize"]:
        err("E_INIT", "没有模块级 `def initialize(context)`"
                      "（聚宽靠它注册回调，缺了等于空跑）")

    # ---------------- C3 import ----------------
    star_import = False
    date_imported = False
    for node in _iter_imports(tree):
        multiline = getattr(node, "end_lineno", node.lineno) != node.lineno
        if isinstance(node, ast.Import):
            roots = [a.name.split(".")[0] for a in node.names]
            asnames = [a.asname for a in node.names]
            if any(r in STRIPPABLE_MODULES for r in roots) or \
                    DATETIME_MODULE in roots:
                if multiline:
                    err("E_IMPORT", "第 %d 行：需要改写的 import 必须写在一行内"
                                    % node.lineno)
                    continue
                if len(roots) != 1:
                    err("E_IMPORT", "第 %d 行：import 多个模块时无法安全改写，"
                                    "请拆成独立的一行" % node.lineno)
                    continue
                root, asname = roots[0], asnames[0]
                if root in STRIPPABLE_MODULES:
                    res["strip_lines"][node.lineno] = "pass"
                    res["notes"].append(
                        "第 %d 行 `import %s` 改写为 pass（该模块不存在，其 API "
                        "已直接注入）" % (node.lineno, root))
                elif asname is not None:
                    err("E_IMPORT", "第 %d 行：`import datetime as %s` 无法改写，"
                                    "请改用 `datetime.now()` / `datetime.today()`"
                                    % (node.lineno, asname))
                else:
                    res["strip_lines"][node.lineno] = "pass"
                    res["notes"].append(
                        "第 %d 行 `import datetime` 改写为 pass（沙箱注入的是"
                        "模拟时钟）" % node.lineno)
                continue
            for root in roots:
                if root in BANNED_IMPORTS:
                    err("E_IMPORT", "第 %d 行：禁止导入 %s" % (node.lineno, root))
                elif root not in ALLOWED_IMPORTS:
                    err("E_IMPORT", "第 %d 行：%s 不在 import 白名单里"
                                    % (node.lineno, root))
        else:
            mod = node.module or ""
            root = mod.split(".")[0]
            if node.level:
                err("E_IMPORT", "第 %d 行：不支持相对导入" % node.lineno)
                continue
            if root in STRIPPABLE_MODULES:
                if multiline:
                    err("E_IMPORT", "第 %d 行：需要改写的 import 必须写在一行内"
                                    % node.lineno)
                    continue
                res["strip_lines"][node.lineno] = "pass"
                res["notes"].append(
                    "第 %d 行 `from %s import ...` 改写为 pass（该模块不存在，"
                    "其 API 已直接注入）" % (node.lineno, root))
                continue
            if root == DATETIME_MODULE:
                names = [a.name for a in node.names]
                if len(names) != 1 or node.names[0].asname:
                    err("E_IMPORT",
                        "第 %d 行：只支持 `from datetime import "
                        "datetime|timedelta|date` 的单别名写法"
                        % node.lineno)
                    continue
                if names[0] not in DATETIME_REWRITE:
                    err("E_IMPORT", "第 %d 行：不支持从 datetime 导入 %s"
                                    % (node.lineno, names[0]))
                    continue
                if multiline:
                    err("E_IMPORT", "第 %d 行：需要改写的 import 必须写在一行内"
                                    % node.lineno)
                    continue
                repl = DATETIME_REWRITE[names[0]]
                if repl != "pass":
                    date_imported = date_imported or names[0] == "date"
                res["strip_lines"][node.lineno] = repl
                res["notes"].append("第 %d 行改写为 `%s`" % (node.lineno, repl))
                continue
            if root not in ALLOWED_IMPORTS:
                err("E_IMPORT", "第 %d 行：%s 不在 import 白名单里"
                                % (node.lineno, root))
            elif any(a.name == "*" for a in node.names):
                star_import = True

    # ---------------- C2 入口 ----------------
    entries = []
    weekly_only = []
    order_called = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else "")
        if name in ORDER_FUNCS:
            order_called = True
        if name not in REBALANCE_FUNCS:
            continue
        if not node.args:
            err("E_ENTRY", "第 %d 行：%s 没有传函数"
                           % (getattr(node, "lineno", 0), name))
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name):
            target = mod_defs.get(arg.id)
            if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                t = None
                for kw in node.keywords:
                    if kw.arg == "time":
                        t = _const_str(kw.value)
                if name == "run_daily":
                    # 只有 run_daily 会被 sandbox_runner 驱动
                    # （scheduled["daily"]）。run_weekly/monthly 注册进去
                    # 也不会执行，绝不能算「可执行入口」——否则「只用
                    # run_weekly」会被判 ok，然后跑出一条直线净值。
                    item = {"kind": name, "name": arg.id}
                    if t:
                        item["time"] = t
                    entries.append(item)
                else:
                    weekly_only.append("%s(%s)" % (name, arg.id))
            else:
                err("E_ENTRY", "第 %d 行：%s(%s) 里的 `%s` 不是模块级具名函数"
                               "（runner 按 __name__ 去模块 globals 取，取不到"
                               "就静默跳过）"
                               % (getattr(node, "lineno", 0), name, arg.id, arg.id))
        elif isinstance(arg, ast.Lambda):
            err("E_ENTRY", "第 %d 行：%s 不能传 lambda（它的 __name__ 是 "
                           "`<lambda>`，runner 取不到 → 静默跳过）"
                           % (getattr(node, "lineno", 0), name))
        elif _const_str(arg) is not None:
            err("E_ENTRY", "第 %d 行：%s 不能传字符串（字符串没有 __name__，"
                           "runner 取不到 → 静默跳过）"
                           % (getattr(node, "lineno", 0), name))
        else:
            err("E_ENTRY", "第 %d 行：%s 的第一个参数必须是模块级具名函数名"
                           % (getattr(node, "lineno", 0), name))

    for cb in FIXED_CB:
        node = mod_defs.get(cb)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries.append({"kind": "fixed_cb", "name": cb})

    if not entries:
        if weekly_only:
            err("I_SCHED", "只用了 %s 注册：沙箱只驱动 daily 调度，这些函数"
                           "从不会被执行（空跑）" % "、".join(weekly_only))
        else:
            err("E_ENTRY", "没有任何可执行入口：既没有 run_daily(顶层函数)，"
                           "也没有 before_trading_start / handle_data 等固定回调")
    elif weekly_only:
        # 有 daily 入口能跑，但 weekly/monthly 那几条仍是空挂 —— 不判死，
        # 必须让作者知道它们永远不会执行。
        warn("W_SCHED", "%s 不会被沙箱执行（只驱动 run_daily）"
                        % "、".join(weekly_only))

    # 去重（同一个函数既 run_daily 又被当成 fixed_cb 时只留一条不现实，
    # 这里只按 (kind,name) 去重）
    seen = set()
    uniq = []
    for e in entries:
        k = (e["kind"], e["name"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    res["entry"] = uniq

    # ---------------- C6 下单 ----------------
    res["uses_order"] = order_called
    if not order_called:
        err("E_ORDER", "没有任何下单调用（order / order_value / order_target / "
                       "order_target_value / order_target_percent）—— "
                       "只算指标不下单的代码不是策略")

    # ---------------- C4 危险内建 ----------------
    defined = _defined_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id in BANNED_BUILTINS and node.id not in defined:
            err("E_BUILTIN", "第 %s 行：禁止使用 `%s`"
                             % (getattr(node, "lineno", "?"), node.id))

    # ---------------- C5 占位函数 ----------------
    entry_names = {e["name"] for e in entries}
    for name, node in mod_defs.items():
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = _strip_docstring(node.body)
        empty = (len(body) == 0) or (
            len(body) == 1 and (
                isinstance(body[0], ast.Pass)
                or (isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and body[0].value.value is Ellipsis)
            )
        )
        raises_ni = (len(body) == 1 and isinstance(body[0], ast.Raise)
                     and _raise_name(body[0]) == "NotImplementedError")
        if raises_ni:
            err("E_STUB", "`%s` 只有 raise NotImplementedError —— 没写完" % name)
        elif empty and name in entry_names:
            # `def initialize(context): pass` 是合法写法（策略靠 handle_data
            # 或别处下单），不能当占位；只有**入口函数**是空壳才算没写。
            err("E_STUB", "`%s` 是入口函数但函数体为空（pass/...）" % name)
        elif empty:
            warn("W_STUB", "`%s` 函数体为空" % name)

    # ---------------- C7 能力清单 ----------------
    cap = api_capability()
    declared = cap["declared"]
    allowed_names = declared | set(dir(builtins)) | defined | \
        {"g", "JQ", "datetime"}

    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    unknown = sorted(x for x in used
                     if x not in allowed_names
                     and not (x.startswith("__") and x.endswith("__")))
    res["api_errors"] = unknown
    if unknown and not star_import:
        err("E_UNKNOWN_API", "沙箱没有这些名字：%s"
                             % ", ".join(unknown))
    elif unknown:
        warn("W_STAR_IMPORT", "用了 import *，跳过未知名字检查（%s）"
                              % ", ".join(unknown))

    used_api = sorted(used & declared)
    res["api_calls"] = used_api
    bad = sorted(set(used_api) & set(cap["unavailable"]))
    res["unavailable_apis"] = bad
    if bad:
        detail = "、".join("%s(%s)" % (b, cap["unavailable"][b]["reason"])
                          for b in bad)
        err("I_API", "调用了沙箱未实现的 API：%s" % detail)

    # ---------------- 时间语义 ----------------
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _attr_chain(node)
        if chain in BANNED_DATETIME_CHAINS or (
                date_imported and chain in BANNED_DATETIME_CHAIN_IF_DATE):
            err("E_DATETIME",
                "第 %s 行：`%s` 拿到的是真实系统时间（不可复现）。"
                "请用 `datetime.now()` / `datetime.today()`"
                % (getattr(node, "lineno", "?"), ".".join(chain)))

    return _finish(res)


def _finish(res: dict) -> dict:
    """决定 status 并把 errors/warnings 映射成旧版 risks 结构。"""
    codes = {e["code"] for e in res["errors"]}
    if not res["syntax_ok"]:
        res["status"] = "syntax_error"
    elif any(c.startswith("E_") for c in codes):
        res["status"] = "contract_failed"
    elif any(c.startswith("I_") for c in codes):
        res["status"] = "infeasible"
    else:
        res["status"] = "ok"
    res["ok"] = res["status"] == "ok"
    risks = []
    for e in res["errors"]:
        level = "info" if e["code"].startswith("I_") else "high"
        risks.append({"level": level, "kind": e["code"],
                      "detail": e["detail"]})
    for w in res["warnings"]:
        risks.append({"level": "medium", "kind": w["code"],
                      "detail": w["detail"]})
    res["risks"] = risks
    return res


def summary(res: dict) -> str:
    """一句话结论（表格/通知用）。"""
    st = res.get("status")
    if st == "ok":
        ent = "、".join(e["name"] for e in res.get("entry") or []) or "?"
        return "符合契约 %s（入口 %s，%d 行）" % (
            res.get("contract_version", CONTRACT_VERSION), ent,
            res.get("n_lines") or 0)
    if st == "syntax_error":
        d = next((e["detail"] for e in res.get("errors") or []), "")
        return "语法错误：%s" % d
    if st == "contract_failed":
        return "不符合契约：" + "；".join(
            e["detail"] for e in res.get("errors") or [])
    if st == "infeasible":
        return "沙箱无法运行：" + "；".join(
            e["detail"] for e in res.get("errors") or [])
    return "未知状态：%s" % st
