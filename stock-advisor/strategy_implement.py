# -*- coding: utf-8 -*-
"""用 LLM 把策略摘要（sa_strategy_digest）重写成可跑的沙箱策略代码。

为什么需要这个模块
------------------
实测 307 篇聚宽社区文章里只有 13 篇抽得出源码（11 篇正文 + 2 篇评论区回复），
其余 295 篇只有 LLM 拆出来的结构化摘要（steps / universe / params）。
要让策略库真正「跑起来」，这 295 篇只能走「照着摘要重写」——
摘要里步骤、参数、阈值都齐了，缺的只是把它们组装成代码。

两条路径（origin 字段区分）：
  - 'llm'    ：无源码的摘要 → LLM 写完整实现 + 前置条件
  - 'source' ：已有可解析源码 → 直接复用源码，LLM 只补前置条件
               （源码是作者验证过的，重写只会引入误差）

沙箱能力边界（写进提示词，违反会被静态检查拒绝）
----------------------------------------------
  - 只允许 58 个 API（SANDBOX_APIS，与 jq_api.api_names() 对齐，离线测试钉死）
  - 没有 get_all_trade_days（用 get_price 的日期索引推日历）
  - 没有 get_call_auction / 分钟级数据（打板、竞价类策略不可实现，候选阶段剔除）
  - handle_data 每天收盘后跑一次，没有盘中时点语义
  - 禁止 import 网络/进程类模块（与 jq_sandbox.BANNED_IMPORTS 同口径）

前置条件（preconditions）是「每天运行」的闸门（用户 2026-10-22 要求
「每个策略应该有个前置条件的」）：机器可判的规则 vocabulary 只有三种
  always_true / above_ma / below_ma
  （标的或指数收盘 vs MA(n)；rules 之间 AND）。评估在 strategy_daily 里做，
  这里只负责生成和校验格式。

本模块不 import app（避免循环依赖）：连接由 app.py 通过 deps 传进来，
LLM 调用走 llm_advisor.ask() 统一出口（task='deep'，效果优先）。
"""
import ast
import builtins
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# 沙箱 API 白名单 —— 与 jq_api.api_names() 对齐（运行时注入里另有两个
# 非 API 名：annotations 是 from __future__ 残留、api_names 是清单函数本身，
# 策略语义上不算 API，不进白名单）。写死成常量（而不是运行时 import jq_api）
# 是为了：① 不拉起 pandas；② 提示词需要这份清单。漂移由离线测试钉死。
# --------------------------------------------------------------------------
SANDBOX_APIS = {
    "FixedSlippage", "OrderCost", "Position",
    "PriceRelatedSlippage", "StepRelatedSlippage",
    "attribute", "balance", "cash_flow", "filter", "fundamentals",
    "get_all_securities", "get_all_securities_dict", "get_bars",
    "get_billboard_list_industry", "get_circulating_market_cap",
    "get_current_data", "get_drawdown", "get_extras", "get_fundamentals",
    "get_fundamentals_continuously", "get_index_stocks", "get_industry",
    "get_industry_stocks", "get_jiqin_quota", "get_market_cap",
    "get_money_flow", "get_price", "get_price_change_rate", "get_slope",
    "get_turnover_rate", "get_valuation", "get_zipline_data",
    "history", "income", "is_st", "is_st_stock", "is_suspended",
    "log", "market", "order", "order_target", "order_target_percent",
    "order_target_value", "order_value", "query", "run_daily",
    "run_monthly", "run_weekly", "set_benchmark", "set_max_stock_num",
    "set_min_stock_num", "set_option", "set_order_cost", "set_slippage",
    "use_real_price", "valuation",
}

# 与 jq_sandbox.BANNED_IMPORTS 同口径（那边是权威；这里只是提前拦一道，
# 真正的闸门在沙箱 static_scan）。
BANNED_IMPORTS = {
    "subprocess", "socket", "shutil", "ctypes", "multiprocessing",
    "urllib", "urllib2", "http", "ftplib", "smtplib", "telnetlib",
    "paramiko", "requests", "aiohttp", "pty", "signal", "resource",
    "sysconfig", "imp", "importlib", "os", "sys", "time", "datetime",
    "random",
}

# 这些标准库在沙箱镜像里可能装了，但策略**没有正当用途**，且逐版本不可控
# —— 让 LLM 只用内置函数 + 白名单 API。math 例外（纯计算无副作用）。
ALLOWED_IMPORTS = {"math"}

# 不可在沙箱实现的策略特征（打板/竞价/分钟级）。只在**没有源码**的路径上
# 剔除；有源码的走沙箱实测说话，不靠关键词猜。
INFEASIBLE_KW = ["竞价", "打板", "涨停板", "分钟级", "分时", "盘口",
                 "level-2", "level2", "L2", "集合竞价"]

RULE_TYPES = {"always_true", "above_ma", "below_ma"}

IMPL_DDL = """
CREATE TABLE IF NOT EXISTS sa_strategy_impl (
    article_id    VARCHAR(64) PRIMARY KEY
                  REFERENCES sa_strategy_article(post_id) ON DELETE CASCADE,
    title         VARCHAR(200) NOT NULL DEFAULT '',
    origin        VARCHAR(8)  NOT NULL DEFAULT 'llm',
    code          TEXT        NOT NULL DEFAULT '',
    preconditions JSONB       NOT NULL DEFAULT '{}'::jsonb,
    precond_text  TEXT        NOT NULL DEFAULT '',
    n_lines       SMALLINT    NOT NULL DEFAULT 0,
    syntax_ok     BOOLEAN     NOT NULL DEFAULT FALSE,
    api_errors    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    risks         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    status        VARCHAR(16) NOT NULL DEFAULT 'draft',
    model         VARCHAR(64) NOT NULL DEFAULT '',
    attempts      SMALLINT    NOT NULL DEFAULT 0,
    note          TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_si_status ON sa_strategy_impl (status, updated_at DESC);
"""

DEFAULT_IMPL_CONF = {
    "enabled": True,
    "min_score": 4,        # 只实现 portable_score >= 此值的摘要
    "max_attempts": 2,     # 静态校验失败时的重写次数（带错误反馈）
    "task": "deep",        # 写代码用 deep；前置条件抽取用 standard
    "max_tokens": 16000,
}


def load_impl_conf() -> dict:
    conf = dict(DEFAULT_IMPL_CONF)
    try:
        import yaml
        p = BASE_DIR / "config.yaml"
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            got = data.get("strategy_impl") or {}
            conf.update({k: v for k, v in got.items() if v is not None})
    except Exception:
        pass
    return conf


def _ensure_tables(deps: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(IMPL_DDL)
        conn.commit()


# --------------------------------------------------------------------------
# 候选与可行性
# --------------------------------------------------------------------------

def load_candidates(cur, min_score: int = 4) -> list[dict]:
    """摘要 LEFT JOIN 源码 LEFT JOIN 已有实现，一次拿全决策所需信息。

    返回行：digest 全字段（title_zh/summary/steps/universe/params/
    applicable/unsuitable/risk_notes/dependencies/portable_score/
    strategy_type/perf_claimed）+ src_ok（源码语法可解析）+ impl_status
    （已有实现的状态，NULL = 没有）。
    """
    cur.execute("""
        SELECT d.article_id, d.title_zh, d.summary, d.strategy_type,
               d.steps, d.universe, d.params, d.applicable, d.unsuitable,
               d.risk_notes, d.dependencies, d.portable_score,
               d.portable_why, d.perf_claimed, d.backtest_period,
               (s.syntax_ok IS TRUE)              AS src_ok,
               COALESCE(s.syntax_ok, FALSE)       AS src_syntax_ok,
               s.code                            AS src_code,
               i.status                          AS impl_status
        FROM sa_strategy_digest d
        LEFT JOIN sa_strategy_source s ON s.post_id = d.article_id
        LEFT JOIN sa_strategy_impl   i ON i.article_id = d.article_id
        WHERE d.portable_score >= %s
        ORDER BY d.portable_score DESC, d.title_zh
    """, (min_score,))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _texts(d: dict) -> str:
    """把 JSONB 字段摊平成文本，供可行性判定与提示词共用。"""
    parts = [d.get("title_zh") or "", d.get("summary") or ""]
    for key in ("steps", "universe", "dependencies", "applicable",
                "risk_notes", "unsuitable"):
        v = d.get(key)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    return "\n".join(parts)


def sandbox_feasible(d: dict) -> tuple[bool, str]:
    """没有源码的摘要，用关键词粗筛「沙箱跑不了」的形态。

    只挡**结构上不可能**的（竞价/分钟级/打板）；关键词误伤的代价是
    少实现一个策略，而把打板策略硬塞进日线沙箱只会产出假回测。
    """
    text = _texts(d)
    for kw in INFEASIBLE_KW:
        if kw in text:
            return False, "沙箱无竞价/分钟级数据：%s" % kw
    return True, ""


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

_SYSTEM_IMPL = """\
你在为一个**个人自用的 A 股量化系统**写策略代码。它有一个模拟聚宽 API 的
沙箱回测器：读入日线 CSV，逐日推进，执行策略代码里的回调，输出逐日净值、
持仓与成交。你写的代码会被静态检查 + 沙箱回测验证，跑不通就是失败。

**沙箱能力边界（违反任何一条都会被静态检查拒绝）：**

1. 只允许调用下面这些 API（全是内置名，直接用，不需要 import）：
   {apis}
2. 没有 get_all_trade_days、没有 get_trade_days —— 交易日历从
   get_price(...) 返回的 DataFrame 日期索引推导（如 sorted(set(df.index))）。
3. 没有 get_call_auction、没有分钟级/分时数据、没有盘口。所有判断只能
   基于日线 OHLCV 与财务字段。
4. handle_data(context, data) 每个交易日**收盘后**跑一次；没有盘中时点。
   run_daily(func, time) 可用但 time 不区分盘中时点（都按当日收盘口径）。
5. 必须定义 initialize(context)。
6. 禁止 import 任何模块（math 可以）；禁止 open/exec/eval/__import__。
   需要随机数时用确定性的方法（如按代码排序取前 N），保证回测可复现。
7. 下单用 order_value / order_target_value / order_target_percent；
   持仓与现金查 context.portfolio（positions / available_cash / total_value）。
   order_target(code, 0) 清仓。
8. 代码必须**完整可运行**：函数体里不许 `...`、不许 pass 占位、
   不许「此处省略」。摘要没写的细节，选保守实现并在该行加注释
   `# NOTE: 摘要未明确，按XX实现`，但**不许留空**。
9. g. 前缀的全局变量可用（g.xxx 在 initialize 里赋值）。
10. 防御式写法：数据不足（列缺失 / 空池）时跳过该日调仓并 return，
    不许抛异常——沙箱里抛异常 = 当日全部逻辑失效。

**输出格式（严格两段，围栏标记，前后不要任何解释文字）：**

```python
<完整策略代码>
```

```json
<前置条件 JSON，见下>
```

前置条件 JSON 结构（type 只能是 always_true / above_ma / below_ma）：
{{
  "precond_text": "不超过 80 字的人读版前置条件（什么时候这个策略该运行）",
  "rules": [
    {{"type": "above_ma", "code": "159531.XSHE", "n": 20, "desc": "标的池基准站上MA20才开仓"}}
  ]
}}
rules 为空列表或只含 {{"type":"always_true"}} 表示无条件运行。
above_ma / below_ma 的 code 是带后缀的聚宽代码（如 000300.XSHG / 159531.XSHE），
语义：该标的**最新收盘价**在它自己 MA(n) 之上/之下；多条规则之间是 AND。

写代码时时刻想着：这份代码要在「只有日线数据」的沙箱里跑出与摘要描述
一致的买卖行为。宁可保守简单，不要花哨。"""

_SYSTEM_PRECOND = """\
你在为一个量化策略生成**机器可判的运行前置条件**。读者是一个每天自动
运行策略的系统：它先检查前置条件，满足才跑这个策略。

前置条件 JSON 结构（type 只能是 always_true / above_ma / below_ma）：
{{
  "precond_text": "不超过 80 字的人读版前置条件",
  "rules": [
    {{"type": "above_ma", "code": "000300.XSHG", "n": 20, "desc": "大盘站稳20日线"}}
  ]
}}
above_ma / below_ma 的 code 是带后缀的聚宽代码，语义：该标的最新收盘价
在它自己 MA(n) 之上/之下；多条规则之间 AND。策略本身自带择时/风控的，
前置条件应该少而保守（它是「要不要跑」的闸门，不是策略逻辑的复制品）。

只输出一个 JSON 对象，不要围栏、不要解释。"""


def _fmt(v, indent: str = "  ") -> str:
    if isinstance(v, (list, dict)):
        try:
            return json.dumps(v, ensure_ascii=False, indent=2)
        except Exception:
            return str(v)
    return str(v)


def build_impl_user(d: dict) -> str:
    apis = ", ".join(sorted(SANDBOX_APIS))
    lines = [
        "请把下面这份策略摘要实现成完整的沙箱策略代码。",
        "",
        "## 策略名\n%s" % (d.get("title_zh") or "(无)"),
        "## 摘要\n%s" % (d.get("summary") or "(无)"),
        "## 类型\n%s" % (d.get("strategy_type") or ""),
    ]
    if d.get("steps"):
        lines.append("## 实现步骤（按顺序执行）\n" + _fmt(d["steps"]))
    if d.get("universe"):
        lines.append("## 标的池\n" + _fmt(d["universe"]))
    if d.get("params"):
        lines.append("## 参数\n" + _fmt(d["params"]))
    if d.get("applicable"):
        lines.append("## 适用场景（前置条件的素材）\n" + _fmt(d["applicable"]))
    if d.get("unsuitable"):
        lines.append("## 不适用场景（要避开/降仓的）\n" + _fmt(d["unsuitable"]))
    if d.get("risk_notes"):
        lines.append("## 风险与坑\n" + _fmt(d["risk_notes"]))
    if d.get("perf_claimed"):
        lines.append("## 作者自述效果（未验证，仅参考，不要照着凑数字）\n"
                     + _fmt(d["perf_claimed"]))
    lines.append("")
    lines.append("记住：只允许这 %d 个 API —— %s" % (len(SANDBOX_APIS), apis))
    lines.append("输出两段：```python 完整代码``` 然后 ```json 前置条件```。")
    return "\n".join(lines)


def build_precond_user(d: dict) -> str:
    lines = [
        "给下面这个策略生成前置条件 JSON。",
        "",
        "## 策略名\n%s" % (d.get("title_zh") or "(无)"),
        "## 摘要\n%s" % (d.get("summary") or "(无)"),
        "## 标的池\n" + _fmt(d.get("universe") or []),
        "## 参数\n" + _fmt(d.get("params") or {}),
        "## 适用场景\n" + _fmt(d.get("applicable") or []),
        "## 不适用场景\n" + _fmt(d.get("unsuitable") or []),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 解析与静态校验
# --------------------------------------------------------------------------

def _parse_blocks(text: str) -> tuple:
    """从 LLM 输出里抠出 (代码, 前置条件 dict)。宽松解析，两段各自独立。"""
    code, pre = None, None
    blocks = re.findall(r"```(?:python|py|json)?[ \t]*\r?\n(.*?)```",
                        text or "", re.S)
    for b in blocks:
        b = b.strip("\r\n \t")
        if code is None and (b.startswith(("import ", "from ", "def ", "#"))
                             or "def initialize" in b):
            try:
                ast.parse(b)
                code = b
                continue
            except SyntaxError:
                pass
        if pre is None:
            s = b.strip()
            if s.startswith("{"):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        pre = obj
                        continue
                except Exception:
                    pass
    # 兜底①：代码围栏没闭合（max_tokens 截断）——取到末尾再剪掉 json 段
    if code is None:
        m = re.search(r"```(?:python|py)?[ \t]*\r?\n(.+)$", text or "", re.S)
        if m:
            cand = re.split(r"```", m.group(1))[0].strip("\r\n \t")
            try:
                ast.parse(cand)
                code = cand
            except SyntaxError:
                pass
    # 兜底②：json 没围栏 —— 从第一个含 rules 的 { 做括号配平
    if pre is None:
        i = (text or "").find('"precond_text"')
        if i >= 0:
            start = text.rfind("{", 0, i)
            if start >= 0:
                depth, end = 0, -1
                in_str, esc = False, False
                for j in range(start, len(text)):
                    c = text[j]
                    if in_str:
                        if esc:
                            esc = False
                        elif c == "\\":
                            esc = True
                        elif c == '"':
                            in_str = False
                        continue
                    if c == '"':
                        in_str = True
                    elif c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                if end > start:
                    try:
                        obj = json.loads(text[start:end + 1])
                        if isinstance(obj, dict):
                            pre = obj
                    except Exception:
                        pass
    return code, pre


def _defined_names(tree: ast.AST) -> set:
    """代码里自己定义/绑定的名字（函数、参数、赋值、导入、异常名…）。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and not isinstance(
                node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def check_code(code: str) -> dict:
    """静态校验。返回：
    {syntax_ok, api_errors, risks, has_initialize, n_lines}
    syntax_ok=False → 代码跑不了；api_errors 非空 → 用了沙箱没有的 API。
    risks 只报告不拦（与 jq_sandbox.static_scan 同口径）。
    """
    out = {"syntax_ok": False, "api_errors": [], "risks": [],
           "has_initialize": False, "n_lines": 0}
    if not code or not code.strip():
        out["risks"].append({"level": "high", "kind": "empty", "detail": "代码为空"})
        return out
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        out["risks"].append({"level": "high", "kind": "syntax",
                             "detail": "line %s: %s" % (exc.lineno, exc.msg)})
        return out
    out["syntax_ok"] = True
    out["n_lines"] = len(code.split("\n"))

    funcs = [n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out["has_initialize"] = "initialize" in funcs
    if not out["has_initialize"]:
        out["risks"].append({"level": "high", "kind": "missing",
                             "detail": "没有 initialize(context)"})

    # banned imports（提前拦一道，真闸门在沙箱 static_scan）
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = (a.asname or a.name).split(".")[0]
                if root in BANNED_IMPORTS:
                    out["risks"].append({"level": "high", "kind": "import",
                                         "detail": root})
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and root not in ALLOWED_IMPORTS:
                out["risks"].append({"level": "high", "kind": "import",
                                     "detail": root})

    # 占位符（.../pass 充当函数体）——LLM 偷懒的形态
    stubs = re.findall(r"^\s*(?:\.\.\.|pass)\s*$", code, re.M)
    if stubs:
        out["risks"].append({"level": "high", "kind": "stub",
                             "detail": "%d 处 .../pass 占位" % len(stubs)})

    # 未知 API：Load 的名字 ∉ (白名单 ∪ builtins ∪ 代码自己定义的)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    allowed = (SANDBOX_APIS | set(dir(builtins))
               | _defined_names(tree) | {"g", "JQ"})
    unknown = sorted(used - allowed)
    if unknown:
        out["api_errors"] = unknown
        out["risks"].append({"level": "high", "kind": "api",
                             "detail": "沙箱没有这些函数: %s"
                                       % ", ".join(unknown)})
    return out


def check_preconditions(pre: dict) -> tuple:
    """前置条件格式校验。返回 (规范后的 dict, 错误列表)。"""
    errs = []
    if not isinstance(pre, dict):
        return {}, ["前置条件不是 JSON 对象"]
    rules = pre.get("rules")
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        return {}, ["rules 不是数组"]
    norm = []
    for r in rules:
        if not isinstance(r, dict):
            errs.append("规则不是对象: %r" % (r,))
            continue
        t = str(r.get("type") or "")
        if t not in RULE_TYPES:
            errs.append("未知规则 type: %r" % t)
            continue
        item = {"type": t, "desc": str(r.get("desc") or "")[:120]}
        if t in ("above_ma", "below_ma"):
            c = str(r.get("code") or "").strip()
            if not re.match(r"^\d{6}\.(XSHG|XSHE)$", c):
                errs.append("code 格式应为 6位.XSHG/XSHE: %r" % c)
                continue
            item["code"] = c
            n = r.get("n", 20)
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = 20
            item["n"] = max(2, min(500, n))
        norm.append(item)
    out = {
        "precond_text": str(pre.get("precond_text") or "")[:200],
        "rules": norm,
    }
    return out, errs


def default_preconditions(d: dict) -> dict:
    """LLM 没给出合法前置条件时的兜底：拿摘要的适用场景当文本，规则恒真。"""
    app = d.get("applicable")
    text = "；".join(str(x) for x in app[:3]) if isinstance(app, list) else ""
    return {"precond_text": text[:200] or "无条件（摘要未给出前置条件）",
            "rules": [{"type": "always_true", "desc": "未生成，默认无条件"}]}


# --------------------------------------------------------------------------
# 落库
# --------------------------------------------------------------------------

_IMPL_COLS = ("article_id", "title", "origin", "code", "preconditions",
              "precond_text", "n_lines", "syntax_ok", "api_errors", "risks",
              "status", "model", "attempts", "note")


def save_impl(cur, article_id: str, *, title: str, origin: str, code: str,
              precond: dict, precond_text: str, check: dict, model: str,
              attempts: int, note: str) -> None:
    """upsert。status 恒为 'draft' —— validated/rejected 由沙箱验证（#12）写。"""
    ok = bool(check["syntax_ok"] and check["has_initialize"]
              and not check["api_errors"])
    cur.execute("""
        INSERT INTO sa_strategy_impl
            (article_id, title, origin, code, preconditions, precond_text,
             n_lines, syntax_ok, api_errors, risks, status, model,
             attempts, note, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,%s,%s, now())
        ON CONFLICT (article_id) DO UPDATE SET
            title = EXCLUDED.title,
            origin = EXCLUDED.origin,
            code = EXCLUDED.code,
            preconditions = EXCLUDED.preconditions,
            precond_text = EXCLUDED.precond_text,
            n_lines = EXCLUDED.n_lines,
            syntax_ok = EXCLUDED.syntax_ok,
            api_errors = EXCLUDED.api_errors,
            risks = EXCLUDED.risks,
            status = 'draft',
            model = EXCLUDED.model,
            attempts = EXCLUDED.attempts,
            note = EXCLUDED.note,
            updated_at = now()
    """, (article_id, title[:200], origin, code,
          json.dumps(precond, ensure_ascii=False), precond_text[:200],
          min(check["n_lines"], 32000), ok,
          json.dumps(check["api_errors"], ensure_ascii=False),
          json.dumps(check["risks"], ensure_ascii=False),
          model, attempts, note[:2000]))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _llm(llm_call, system: str, user: str, conf: dict, task: str) -> str:
    if llm_call is None:
        import llm_advisor
        llm_call = llm_advisor.ask
    return llm_call(system, user, max_tokens=int(conf.get("max_tokens", 16000)),
                    task=task)


def implement_one(cur, d: dict, llm_call=None, conf: dict | None = None) -> dict:
    """无源码摘要 → LLM 写完整实现。静态校验失败时带错误反馈重试。"""
    conf = conf or load_impl_conf()
    max_attempts = max(1, int(conf.get("max_attempts", 2)))
    task = conf.get("task", "deep")
    aid = d["article_id"]
    title = d.get("title_zh") or aid

    feedback = ""
    last = {"code": "", "pre": None, "check": check_code(""), "raw_len": 0}
    for attempt in range(1, max_attempts + 1):
        user = build_impl_user(d)
        if feedback:
            user += ("\n\n## 上一次尝试被静态检查拒绝，必须修正：\n" + feedback
                     + "\n请重新输出完整的两段（python + json）。")
        text = _llm(llm_call, _SYSTEM_IMPL.format(
            apis=", ".join(sorted(SANDBOX_APIS))), user, conf, task)
        code, pre = _parse_blocks(text)
        check = check_code(code or "")
        last = {"code": code or "", "pre": pre, "check": check,
                "raw_len": len(text or "")}
        errs = []
        if not code:
            errs.append("输出里解析不出 Python 代码块")
        elif not check["syntax_ok"]:
            errs.append("语法错误: %s" % json.dumps(
                check["risks"], ensure_ascii=False))
        elif not check["has_initialize"]:
            errs.append("缺少 initialize(context)")
        elif check["api_errors"]:
            errs.append("用了沙箱没有的 API: %s（白名单只有 %d 个）"
                        % (", ".join(check["api_errors"]), len(SANDBOX_APIS)))
        if not errs:
            break
        feedback = "\n".join("- " + e for e in errs)
    else:
        pass  # 循环走完仍失败：用最后一次结果落库，note 记原因

    code = last["code"]
    check = last["check"]
    pre, pre_errs = check_preconditions(last["pre"] or {})
    # 部分规则非法时保留合法的（check_preconditions 已剔除坏规则），
    # 只有规范化结果整体为空才退摘要兜底。
    if not pre.get("rules") and not pre.get("precond_text"):
        pre = default_preconditions(d)
        pre_errs = pre_errs or ["LLM 前置条件缺失，用摘要兜底"]

    ok = bool(code and check["syntax_ok"] and check["has_initialize"]
              and not check["api_errors"])
    notes = []
    if not ok:
        notes.append("静态校验未通过（draft 保留供人工看）")
    if pre_errs:
        notes.append("前置条件: " + "; ".join(pre_errs[:3]))
    model = str(conf.get("task", "deep"))
    save_impl(cur, aid, title=title, origin="llm", code=code, precond=pre,
              precond_text=pre.get("precond_text", ""), check=check,
              model=model, attempts=max_attempts, note="；".join(notes))
    return {"article_id": aid, "title": title, "origin": "llm",
            "ok": ok, "n_lines": check["n_lines"],
            "api_errors": check["api_errors"],
            "n_rules": len(pre.get("rules", [])), "note": "；".join(notes)}


def reuse_source(cur, d: dict, llm_call=None, conf: dict | None = None) -> dict:
    """有可解析源码 → 直接复用，LLM 只生成前置条件。"""
    conf = conf or load_impl_conf()
    aid = d["article_id"]
    title = d.get("title_zh") or aid
    code = d.get("src_code") or ""
    check = check_code(code)
    # 源码是作者原版：api_errors 只记录不否决（沙箱实测才是真闸门），
    # 但语法必须可解析（否则 run_in_docker 直接白跑）。
    try:
        text = _llm(llm_call, _SYSTEM_PRECOND.format(),
                    build_precond_user(d), conf, "standard")
        _c, pre = _parse_blocks(text)
        if pre is None:
            s = text.strip()
            if s.startswith("{"):
                try:
                    pre = json.loads(s)
                except Exception:
                    pre = None
    except Exception:
        pre = None
    pre, pre_errs = check_preconditions(pre or {})
    if not pre.get("rules") and not pre.get("precond_text"):
        pre = default_preconditions(d)
        pre_errs = pre_errs or ["LLM 前置条件缺失，用摘要兜底"]
    notes = []
    if pre_errs:
        notes.append("前置条件兜底: " + "; ".join(pre_errs[:3]))
    if check["api_errors"]:
        notes.append("源码用了沙箱外 API（待沙箱实测）: %s"
                     % ", ".join(check["api_errors"][:8]))
    save_impl(cur, aid, title=title, origin="source", code=code,
              precond=pre, precond_text=pre.get("precond_text", ""),
              check=check, model="standard",
              attempts=1, note="；".join(notes))
    return {"article_id": aid, "title": title, "origin": "source",
            "ok": bool(check["syntax_ok"]),
            "n_lines": check["n_lines"],
            "api_errors": check["api_errors"],
            "n_rules": len(pre.get("rules", [])), "note": "；".join(notes)}


def implement_batch(deps: dict, *, min_score: int | None = None,
                    limit: int = 20, force: bool = False,
                    article_ids: list | None = None,
                    llm_call=None, conf: dict | None = None) -> dict:
    """批量实现。返回 {done, skipped, results}。

    跳过规则：
      - 已有实现且非 force
      - 无源码且沙箱不可行（打板/竞价/分钟级）
      - conf.enabled = false 时只列候选不调 LLM
    """
    conf = conf or load_impl_conf()
    if min_score is None:
        min_score = int(conf.get("min_score", 4))
    enabled = bool(conf.get("enabled", True))
    results, skipped = [], []
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(IMPL_DDL)   # 幂等；--run 单独跑也不能依赖 --list 先建过表
        conn.commit()
        cands = load_candidates(cur, min_score)
        if article_ids:
            want = set(str(a) for a in article_ids)
            cands = [c for c in cands if c["article_id"] in want]
            missing = want - {c["article_id"] for c in cands}
            for m in sorted(missing):
                skipped.append({"article_id": m, "reason": "摘要不存在或分数不足"})
        done = 0
        for d in cands:
            aid = d["article_id"]
            if d.get("impl_status") and not force:
                skipped.append({"article_id": aid,
                                "reason": "已有实现 status=%s（force 可重做）"
                                          % d["impl_status"]})
                continue
            if d.get("src_ok"):
                if not enabled:
                    skipped.append({"article_id": aid, "reason": "enabled=false（有源码待复用）"})
                    continue
                r = reuse_source(cur, d, llm_call, conf)
            else:
                feas, why = sandbox_feasible(d)
                if not feas:
                    skipped.append({"article_id": aid, "reason": why})
                    continue
                if not enabled:
                    skipped.append({"article_id": aid, "reason": "enabled=false（待 LLM 实现）"})
                    continue
                r = implement_one(cur, d, llm_call, conf)
            conn.commit()
            results.append(r)
            done += 1
            if done >= limit:
                break
    return {"done": done, "skipped": skipped, "results": results}


def list_impls(cur) -> list[dict]:
    cur.execute("""
        SELECT article_id, title, origin, n_lines, syntax_ok, api_errors,
               precond_text, preconditions, status, model, attempts, note,
               updated_at
        FROM sa_strategy_impl
        ORDER BY updated_at DESC
    """)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# 独立运行：python strategy_implement.py --list / --run [article_id ...]
# --------------------------------------------------------------------------

def _main_conn():
    import os
    import psycopg2
    env = {}
    env_path = BASE_DIR.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return psycopg2.connect(
        host=env.get("DB_HOST", "101.43.25.101"),
        port=int(env.get("DB_PORT", "5432")),
        user=env.get("DB_USERNAME", "jqeval"),
        password=env.get("DB_PASSWORD", ""),
        dbname=env.get("DB_DATABASE", "dify"),
    )


if __name__ == "__main__":
    import argparse
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列候选，不调 LLM")
    ap.add_argument("--run", nargs="*", default=None, metavar="article_id",
                    help="实现指定 article_id（不给 = 全部候选）")
    ap.add_argument("--min-score", type=int, default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    deps = {"get_conn": _main_conn}
    conf = load_impl_conf()
    if args.list or (args.run is None and not args.list):
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            _ensure_tables(deps)
            cands = load_candidates(cur, args.min_score
                                    if args.min_score is not None
                                    else int(conf.get("min_score", 4)))
        for c in cands:
            feas, why = sandbox_feasible(c) if not c.get("src_ok") else (True, "")
            tag = "src" if c.get("src_ok") else ("feasible" if feas else "INFEASIBLE")
            print("%-6s score=%d %-24s %s %s"
                  % (tag, c.get("portable_score") or 0, c["article_id"][:24],
                     (c.get("title_zh") or "")[:36],
                     ("| " + why) if why else ""))
        print("candidates=%d" % len(cands))
    if args.run is not None:
        ids = [a for a in args.run if a] or None
        out = implement_batch(deps, min_score=args.min_score,
                              limit=args.limit, force=args.force,
                              article_ids=ids, conf=conf)
        for r in out["results"]:
            print("[done] %-24s %-8s ok=%s lines=%s rules=%s %s"
                  % (r["article_id"][:24], r["origin"], r["ok"],
                     r["n_lines"], r["n_rules"], r["note"]))
        for s in out["skipped"]:
            print("[skip] %-24s %s" % (s["article_id"][:24], s["reason"]))
        print("done=%d skipped=%d" % (out["done"], len(out["skipped"])))
