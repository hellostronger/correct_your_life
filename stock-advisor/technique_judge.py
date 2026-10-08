# -*- coding: utf-8 -*-
"""技法质量裁判：判断 `sa_analysis_techniques` 里抽出来的技法**是否真的有用**。

为什么需要它（实测 2026-10-08）
--------------------------------
`analysis_learn.EXTRACT_SYSTEM` 里的 `SCORE` 维度被修过（从「这套方法技术含量
多高」改成「对我做 A 股决策有没有用」），但**库里已有的条目还是老维度打的分**。
实测后果：

```
active 111 条，只有 23 条 hits>0（其余 88 条永远轮不上，limit=12）
实际注入的 12 条里 3 条跑题，且排在最前面：
  异构双模型交叉验算（LLM 评测方法论）  score 10.0
  多空美元中性组合实盘验证（美股多空）    score 9.0
  风格剥离残差 IC（量化因子）              score 9.0
```

跑题条目挤掉了真正有用的 A 股技法（正常化 EBIT / EPV / 三段式回报…）。
本模块就是**事后审计**：拿一批技法问「这条到底能不能套到任意一只 A 股上」。

与抽取的分工（都读同一张表，不新增第二套状态机）
------------------------------------------------
| | analysis_learn（抽取） | technique_judge（本模块） |
|---|---|---|
| 干什么 | 从文章抽技法 | 判断抽出来的对不对 |
| 用哪条 prompt | `EXTRACT_SYSTEM` | `load_judge_prompt()`（可插拔） |
| 写哪 | `status`/`score`/技法正文 | **只写 `review_note`**（前缀 `judge:`） |
| 改 status 吗 | 按 score 直通 active | **不改**，只给建议 |

**刻意不新增任何列**：AGENTS.md 要求「改数据库 schema 必须先问」，
而 `review_note` 这个列本来就是给人写审核意见的，正好够用。
`status` 仍然只能由人（或抽取时的打分逻辑）改 —— 人工兜底这条纪律不破。

「可插拔」怎么实现的（你要加 skill 就改这里）
-------------------------------------------
判据 prompt 不是写死的常量，而是 `load_judge_prompt()` 按优先级取：

1. `config.yaml` 的 `analysis_learn.judge_prompt_file`，或环境变量
   `SA_JUDGE_PROMPT_FILE` —— 指定的文件
2. `stock-advisor/skills/technique_judge/JUDGE.md`
3. `stock-advisor/skills/technique_judge/SKILL.md`
4. 内置 `DEFAULT_JUDGE_PROMPT`

**文件内容整体作为 system prompt**（只做 `{n}` 之类的占位替换）。
所以后面想换判据、想让它变成一个真正的 skill，直接往 2/3 号位置丢一个
markdown 文件即可，**不用改任何代码、不用重启以外的事**。
`GET /api/analysis/judge/config` 会返回当前用的是哪一份，页面上直接能看到。

不 import app（AGENTS.md 惯例）：连接/游标与 LLM 出口都由调用方注入。
这样离线断言可以完全不打网络、不烧 LLM 额度。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

#: 判据文件的候选位置（相对 BASE_DIR）。顺序即优先级。
SKILL_CANDIDATES = (
    "skills/technique_judge/JUDGE.md",
    "skills/technique_judge/SKILL.md",
    "skills/technique_judge.md",
)

#: 判定档位。`recommended_status` 是**建议**，不是执行 —— 改 status 仍然是人。
VERDICTS = ("useful", "weak", "offtopic", "duplicate")
VERDICT_LABEL = {
    "useful": "有用",
    "weak": "偏弱（有用但不够硬）",
    "offtopic": "跑题（套不到 A 股个股上）",
    "duplicate": "与已有条目重复",
}
VERDICT_RECOMMEND = {
    "useful": "active",
    "weak": "active",
    "offtopic": "rejected",
    "duplicate": "rejected",
}

#: 单次 LLM 调用里最多塞几条技法。批量是为了省钱：
#: 逐条判 120 条 = 120 次调用；按 10 条一批 = 12 次。
DEFAULT_BATCH = 10
DEFAULT_MAX_ITEMS = 120
REVIEW_NOTE_PREFIX = "judge:"


# --------------------------------------------------------------------------
# 判据 prompt（可插拔）
# --------------------------------------------------------------------------

DEFAULT_JUDGE_PROMPT = """\
你在给一个 **A 股个人自用系统**做「技法质量审计」。

这个系统会从公众号文章里抽取「判读规则」，然后把**生效状态**的技法注入到
每日盘前/盘后报告的 prompt 里，让模型按这些规则检查自己的分析有没有漏掉
关键维度。所以注入进来的每一条都会**占用模型的注意力**。

你的任务：判断下面每一条技法，**它到底能不能套到任意一只 A 股个股上**。

判定档位（只能选这四个之一）：

###VERDICT 取值
- useful    能直接套到任意一只 A 股上，判据明确（A 股估值/财务/资金/事件/
            风险/技术面 都算）
- weak      方向对但太泛、或需要大量前提才能用；有价值但不该排在前面
- offtopic  套不到 A 股个股上。以下**一律 offtopic**，哪怕方法本身很硬：
            ① LLM/机器学习的评测方法论（双模型交叉验算、任务分解、打分门禁）
            ② 海外资产尽调（房产/REITs/美元债/跨境电商）
            ③ 一级市场募资与 LP 出资决策（DPI/TVPI/GP 筛选）
            ④ 美股/期货/加密的跨市场相对价值与中性组合
            ⑤ 纯学术统计、纯编程/工程实践
- duplicate 与本批里另一条讲的是同一件事

**打分的教训（务必读）**：早期版本的抽取 prompt 把 SCORE 定义成
「这套方法的技术含量」，于是「异构双模型交叉验算」拿了 10 分、
「多空美元中性组合」拿了 9 分 —— 方法论上它们很硬，但**对我做 A 股决策
一点用没有**，反而按分数排到最前面，把真正有用的估值技法挤掉了。
**技术含量高 ≠ 对我有用。你判的是后者。**

拿不准的时候，倾向 offtopic：注入是有成本的，宁可少注入一条，
也不要让跑题的方法稀释模型注意力。

输出格式：**严格按下面的分段标记输出，不要 JSON、不要代码围栏、
不要任何开场白/解释/总结。每条一个块，块数必须与输入条数相同。**

###ITEM
输入里那条技法的编号（照抄，一个数字，不要改）
###VERDICT
useful / weak / offtopic / duplicate 四选一
###SCORE
建议分数，0~10 的整数。判据同「对我做 A 股决策有没有用」：
9-10 = 能直接套、判据硬；6-8 = 常规套路有用；3-5 = 很泛；
0-2 = 与 A 股个股无关。
###REASON
一句话理由，30 字以内，必须点明「为什么套不到/能套到 A 股上」。
"""


# --------------------------------------------------------------------------
# 适用性打标（2026-10-08 加）
# --------------------------------------------------------------------------
# 为什么需要：加 `applicable` 列只解决「以后抽的」，**存量 172 条全是
# `applicable='[]'`（=通用）**，于是 `generic_only` 过滤当前一条都排不掉 ——
# 实测注入块里仍然有「长协电价/动力煤/核电/控股火电」这种火电专用技法
# 和「异构双模型交叉验算」这种离题条目，**字段加了等于没加**。
# 重新抽一遍文章要花钱且会覆盖 review_note，所以这里只做**打标**：
# 让 LLM 判「通用 / 行业专属 / 离题」，行业专属的给出标签，离题的标出来供人否决。
#
# 与 judge 的关系：复用同一套机械（分批、dry_run 默认 True、严格解析），
# 但**写不同的列**（写 applicable，judge 只写 review_note）。

SCOPES = ("generic", "sector", "offtopic")

APPLICABLE_SYSTEM = """\
你是 A 股研究流程的适用性审核员。下面每条都是一条「分析技法」。
请对每条判断三件事：它能不能套到**任意一只 A 股**上；如果不能，它属于哪些行业。

###ITEM
输入里那条技法的编号（照抄，一个数字，不要改）
###SCOPE
三选一：
- generic   通用。任何 A 股都能套，前提里没有只有特定行业才有的东西
- sector    行业专属。前提里有只有某些行业才有的东西（如「寿险用 P/EV」
           「用长协电价判断火电」「剔除银行/券商的投资收益」）
- offtopic  离题。它根本不是 A 股/港股个股分析方法，而是
            海外房产尽调、一级市场募资、LLM/ML 评测方法论、纯学术统计等
###APPLICABLE
SCOPE=sector 时写行业名，逗号分隔，只能用这些：
银行 / 保险 / 券商 / 地产 / 医药 / 半导体 / 消费 / 白酒 / 电力火电 / 核电 /
煤炭 / 钢铁 / 有色 / 化工 / 汽车 / 军工 / 计算机 / 通信 / 传媒 / 农业 /
建筑 / 交运 / 机械 / 家电 / 纺服 / 零售
SCOPE=generic 或 offtopic 时这一行写「通用」。
###REASON
一句话，30 字以内。

严格按分段标记输出，不要 JSON、不要代码围栏、不要开场白。
"""

APPLICABLE_USER = """\
待打标的分析技法共 {n} 条。

【技法清单】
{catalog}

【逐条内容】
{items}
"""

JUDGE_USER = """\
下面是待审计的 {n} 条技法（来自公众号文章抽取，已注入决策 prompt）。
请逐条判定并按系统提示的格式输出。

{catalog}

【正文】
{items}
"""


def load_judge_prompt(explicit_path: str = "",
                      env: dict | None = None) -> tuple[str, str]:
    """取判据 prompt。返回 `(prompt, 来源说明)`。

    来源说明会出现在 `/api/analysis/judge/config` 里 —— 这样「现在用的是哪套
    判据」是**可观测**的，而不是猜。

    优先级：显式参数 > 环境变量 > skills/ 下的文件 > 内置默认。

    ⚠️ **显式指定的文件读不到时，不许静默降级到别的来源**（2026-10-08 离线断言
    逮到）。设想你把判据换成了自己的 skill 文件、路径写错了一位 ——
    如果悄悄退回内置默认，你以为在用自己的判据，实际一直在用旧的，而且
    **没有任何报错**。这正是 AGENTS.md 反复强调的「配置指向已失效的东西」
    那类静默失效。所以显式路径失败 = **退回内置 + 在来源说明里喊出来**，
    调用方/页面能立刻看到「配置没生效」。
    """
    env = env if env is not None else os.environ
    envp = (env.get("SA_JUDGE_PROMPT_FILE") or "").strip()

    # 第一层：显式指定（config / 环境变量）。失败即喊，不静默换来源。
    for origin, path in (("config", (explicit_path or "").strip()),
                         ("env:SA_JUDGE_PROMPT_FILE", envp)):
        if not path:
            continue
        try:
            p = Path(path)
            if not p.is_file():
                return DEFAULT_JUDGE_PROMPT, (
                    "⚠️ 配置指定的判据文件不存在，已退回内置默认：%s -> %s"
                    % (origin, path))
            text = p.read_text(encoding="utf-8").strip()
            if not text:
                return DEFAULT_JUDGE_PROMPT, (
                    "⚠️ 配置指定的判据文件是空的，已退回内置默认：%s -> %s"
                    % (origin, path))
            return text, "%s -> %s" % (origin, p)
        except Exception as exc:                              # noqa: BLE001
            return DEFAULT_JUDGE_PROMPT, (
                "⚠️ 读判据文件失败，已退回内置默认：%s（%s）" % (exc, path))

    # 第二层：skills/ 下的文件（可选增强，没有就用内置）
    tried = []
    for rel in SKILL_CANDIDATES:
        p = BASE_DIR / rel
        tried.append(str(p))
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    return text, "skills -> %s" % p
        except Exception:                                     # noqa: BLE001
            continue
    return DEFAULT_JUDGE_PROMPT, (
        "内置默认（未找到判据文件；放一个到下面任一路径即可替换，不用改代码：%s）"
        % ", ".join(tried))


# --------------------------------------------------------------------------
# 取待判条目
# --------------------------------------------------------------------------

#: 列名与顺序**只在这里定义一次**，SELECT 由它生成。
#: 为什么不能手写两份：列名/顺序漂移的后果是**静默取错字段**（不报错），
#: 比崩溃危险得多 —— 与 paper_trading._SETTLE_BUY_COLS 同一个理由。
_JUDGE_COLS = ("id", "name", "category", "rule", "indicators", "evidence",
               "score", "status", "source_title", "source_article_id",
               "learned_at")

JUDGE_SELECT = ("SELECT " + ", ".join(_JUDGE_COLS) +
                " FROM sa_analysis_techniques WHERE status = ANY(%s) "
                "ORDER BY score DESC, id DESC LIMIT %s")


def _rows_as_dicts(cur, rows) -> list[dict]:
    """把游标返回的行统一成 dict，**普通游标(tuple)和 RealDictCursor 都支持**。

    ⚠️ 这不是洁癖：本模块的调用方传的是 `conn.cursor()`（普通游标 → tuple），
    直接 `dict(r)` 会抛
    `TypeError: cannot convert dictionary update sequence element #0 to a sequence`。
    离线断言用假游标复现过这个崩溃，所以这里必须有归一化。
    （analysis_learn.build_injection_block 也是同一处理。）
    """
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            out.append(dict(r))
            continue
        desc = getattr(cur, "description", None) or _JUDGE_COLS
        out.append(dict(zip(_col_names(desc), r)))
    return out


class _Column:
    """psycopg2 `description` 元素的最小复刻：可下标、**不可 hash**。

    为什么专门造它：`psycopg2.extensions.Column` 既不是 tuple 也不是 list，
    所以 `isinstance(d, (tuple, list))` 是 False。若代码据此走进
    `list(desc)` 分支，`dict(zip(Columns, row))` 就会因为要 hash Column 而抛
    `TypeError: unhashable type: 'psycopg2.extensions.Column'`。
    ⚠️ 2026-10-08 端到端真打才发现 —— 离线断言当时给的假 description 是
    `("id",)` 这种 1 元素 tuple，恰好命中能工作的那条分支。
    **假数据形状和真实形状不一致，测试就是自欺。**
    """

    __hash__ = None          # 明确不可 hash，复现真实行为

    def __init__(self, name):
        self._name = name

    def __getitem__(self, i):
        return (self._name, None, None, None, None, None, None)[i]

    def __len__(self):
        return 7


def _col_names(desc) -> list:
    """把 `cursor.description` 归一成列名列表。**逐元素**判断，不看整体类型。

    支持三种形状：
      - `("id", "name", ...)`            —— 纯字符串序列（本模块 _JUDGE_COLS 兜底）
      - `(Column, Column, ...)`         —— psycopg2 普通游标（2.8+ 的真实形状）
      - `(("id", ...), ("name", ...))`   —— 老式 tuple 描述
    """
    names = []
    for d in desc:
        if isinstance(d, str):
            names.append(d)
            continue
        try:
            first = d[0]
        except Exception:                                     # noqa: BLE001
            names.append(str(d))
            continue
        names.append(first if isinstance(first, str) else str(first))
    return names


def fetch_for_judge(cur, statuses=("active", "pending"),
                    limit: int = DEFAULT_MAX_ITEMS) -> list[dict]:
    """取待判条目。

    默认只判 `active` + `pending`：`rejected` 是人已经否掉的，再判一遍是浪费
    LLM 额度。`status = ANY(%s)` 传列表 —— 走 psycopg2 参数位是安全的
    （AGENTS.md 记过 `IN (%s)` 传 list 会 IndexError，`= ANY(%s)` 才对）。
    """
    cur.execute(JUDGE_SELECT, (list(statuses), int(limit)))
    return _rows_as_dicts(cur, cur.fetchall())


def _norm_rule(rule: str) -> str:
    """判重用的归一化：只留中文/字母数字，去掉所有标点与空白。"""
    return re.sub(r"[^\w一-鿿]+", "", str(rule or "")).lower()


def find_duplicates(cur, statuses=("active", "pending"),
                    threshold: float = 0.72) -> list[dict]:
    """**不烧 LLM** 的规则判重：同一条技法被多篇文章重复抽出来。

    判据是 `rule` 归一化后的相似度（difflib）。不用 LLM 是因为这类重复
    靠字面就能看出来，而 LLM 一次调用要花钱。
    `UNIQUE (source_article_id, name)` 只防同一篇里的重名，**跨篇重复防不住** ——
    这正是要在这里补的洞。
    """
    import difflib

    rows = fetch_for_judge(cur, statuses, limit=1000)
    out: list[dict] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            na, nb = _norm_rule(a.get("rule")), _norm_rule(b.get("rule"))
            if not na or not nb:
                continue
            r = difflib.SequenceMatcher(None, na, nb).ratio()
            if r >= threshold:
                # 同 id 比自己没意义；同 source_article_id 的已被 UNIQUE 挡住
                out.append({"keep": a["id"], "drop": b["id"], "ratio": round(r, 3),
                            "keep_name": a.get("name"), "drop_name": b.get("name"),
                            "via": "difflib"})
    return out


# --------------------------------------------------------------------------
# 组装与解析
# --------------------------------------------------------------------------

def build_catalog(items: list[dict]) -> str:
    """待判条目的目录（编号 -> 技法）。编号从 1 开始，与解析对齐。"""
    out = []
    for i, it in enumerate(items, 1):
        cat = _flat(it.get("category")) or "未分类"
        ind = _flat(it.get("indicators"))
        try:
            ind_s = "、".join(json.loads(ind)) if ind.strip().startswith("[") else ind
        except Exception:                                      # noqa: BLE001
            ind_s = ind
        out.append("#%d [%s] %s（现有 score=%s）\n    指标：%s"
                   % (i, cat, _flat(it.get("name")), it.get("score"),
                      ind_s or "未标注"))
    return "\n".join(out)


def build_items_block(items: list[dict], max_rule: int = 600) -> str:
    out = []
    for i, it in enumerate(items, 1):
        out.append("### #%d\n技法名：%s\n分类：%s\n判读规则：%s\n原文依据：%s"
                   % (i, _flat(it.get("name")), _flat(it.get("category")),
                      _flat(it.get("rule"))[:max_rule],
                      _flat(it.get("evidence"))[:300] or "（空）"))
    return "\n\n".join(out)


def parse_verdicts(text: str, n_items: int) -> list[dict]:
    """解析 LLM 的分段标记输出。**丢掉编号对不上的块**。

    与 `analysis_learn.parse_techniques` 同一纪律：宁可少一条，也不要把
    模型编的/串位的判定当成真判定。所以：
      - `###ITEM` 的编号必须是 1..n 内的整数，且**不重复**
      - `###VERDICT` 必须在允许集合里，否则整块丢弃（不猜）
    """
    if not text:
        return []
    fields_of = ("ITEM", "VERDICT", "SCORE", "REASON")
    out: list[dict] = []
    seen: set[int] = set()
    for part in re.split(r"###ITEM", text)[1:]:
        f = {k: "" for k in fields_of}
        cur = None
        for line in part.strip().splitlines():
            s = line.strip()
            m = re.match(r"^###([A-Z_]+)\s*(.*)$", s)
            if m and m.group(1) in fields_of:
                cur = m.group(1)
                f[cur] = m.group(2).strip()
            elif cur:
                f[cur] += (("\n" if f[cur] else "") + s)
            elif s:
                cur = "ITEM"                 # 第一行是编号本身
                f["ITEM"] = s
        try:
            idx = int(_flat(f["ITEM"]).strip().splitlines()[0])
        except (ValueError, IndexError):
            continue
        if not (1 <= idx <= n_items) or idx in seen:
            continue
        v = _flat(f["VERDICT"]).strip().lower()
        if v not in VERDICTS:
            continue
        seen.add(idx)
        out.append({"idx": idx, "verdict": v,
                    "score": _to_float(f["SCORE"], None),
                    "reason": _flat(f["REASON"]).strip()[:200]})
    return out


# --------------------------------------------------------------------------
# 裁判主流程
# --------------------------------------------------------------------------

def judge_batch(llm_fn, items: list[dict], prompt: str,
                max_tokens: int = 4000) -> list[dict]:
    """判一批。`llm_fn(system, user, max_tokens=...)` 由调用方注入（便于离线断言）。

    返回与 `items` 等长、同序的结果列表（判不出来的那条 verdict 为空串）。
    """
    user = JUDGE_USER.format(
        n=len(items), catalog=build_catalog(items), items=build_items_block(items))
    raw = llm_fn(prompt, user, max_tokens=max_tokens)
    parsed = parse_verdicts(raw, len(items))
    by_idx = {p["idx"]: p for p in parsed}
    out: list[dict] = []
    for i in range(len(items)):
        p = by_idx.get(i + 1)
        out.append({
            "id": items[i]["id"],
            "name": _flat(items[i].get("name")),
            "verdict": (p or {}).get("verdict", ""),
            "suggest_score": (p or {}).get("score"),
            "reason": (p or {}).get("reason", ""),
            "parsed": bool(p),
        })
    return out


def run_judge(deps: dict, statuses=("active", "pending"),
              batch: int = DEFAULT_BATCH, max_items: int = DEFAULT_MAX_ITEMS,
              prompt: str = "", dry_run: bool = True,
              llm_fn=None, duplicates: list[dict] | None = None) -> dict:
    """跑一轮审计。

    `deps` 的键 —— **判据与 LLM 配置必须分开**（2026-10-08 踩过）：
      - `get_conn`          必需
      - `conf`              LLM 配置，形状同 `llm_advisor.load_llm_conf()`
                             （必须含 `api_key`）
      - `judge_prompt_file` 可选，判据文件路径；不给就走 load_judge_prompt 的优先级

    ⚠️ ��把判据配置塞进 `conf`：那会让 `llm_advisor.ask` 收到一个没有
    `api_key` 的 dict，直接 `KeyError: 'api_key'`。第一版就是这么错的，
    而且**只在真调 LLM 时才暴露** —— 离线断言全绿也照样漏。

    `dry_run=True`（默认）**只读不写** —— 判据要花钱调 LLM，
    与 `/api/analysis/learn` 同一个默认值纪律。
    `dry_run=False` 才写进 `review_note`（仍**不改 status**）。

    返回 `{judged, dry_run, prompt_source, items, summary, errors}`。
    """
    get_conn = deps["get_conn"]
    conf = dict(deps.get("conf") or {})
    if llm_fn is None:
        import llm_advisor
        if not conf:
            # 回落到真配置。注意**不是** DEFAULT_LLM_CONF —— 那个按
            # AGENTS.md 的设计是「模型名为空时的报错哨兵」，不是可用配置。
            conf = dict(llm_advisor.load_llm_conf())

        def llm_fn(system, user, max_tokens=4000):            # noqa: ANN001
            return llm_advisor.ask(system, user, conf=conf,
                                   max_tokens=max_tokens, thinking=True)

    if prompt:
        prompt_text, source = prompt, "调用方直接传入"
    else:
        prompt_text, source = load_judge_prompt(
            explicit_path=str(deps.get("judge_prompt_file") or ""))

    result = {"ok": True, "dry_run": bool(dry_run), "prompt_source": source,
              "judged": 0, "items": [], "errors": [], "duplicates": []}

    with get_conn() as conn, conn.cursor() as cur:
        items = fetch_for_judge(cur, statuses, limit=max_items)
        if duplicates is None:
            try:
                result["duplicates"] = find_duplicates(cur, statuses)
            except Exception as exc:                          # noqa: BLE001
                result["errors"].append("规则判重失败（不影响 LLM 判词）: %s" % exc)

    if not items:
        result["summary"] = {"useful": 0, "weak": 0, "offtopic": 0,
                             "duplicate": 0, "unparsed": 0}
        return result

    for start in range(0, len(items), max(1, int(batch))):
        chunk = items[start:start + max(1, int(batch))]
        try:
            result["items"].extend(judge_batch(llm_fn, chunk, prompt_text))
        except Exception as exc:                              # noqa: BLE001
            result["errors"].append("第 %d 条起的一批判定失败: %s"
                                    % (start + 1, exc))

    result["judged"] = sum(1 for i in result["items"] if i["verdict"])
    summary = {"useful": 0, "weak": 0, "offtopic": 0, "duplicate": 0, "unparsed": 0}
    for i in result["items"]:
        summary[i["verdict"] or "unparsed"] = summary.get(i["verdict"] or "unparsed", 0) + 1
    result["summary"] = summary

    # 规则判重的结果并进 summary（LLM 不一定看得出跨篇重复）
    if result["duplicates"]:
        summary["duplicate"] += len(result["duplicates"])

    if dry_run:
        return result

    # 只写 review_note，绝不动 status
    notes = []
    for i in result["items"]:
        if not i["verdict"]:
            continue
        notes.append((REVIEW_NOTE_PREFIX + " %s score=%s %s"
                      % (i["verdict"], i["suggest_score"], i["reason"])[:500],
                      datetime.now(), i["id"]))
    if notes:
        try:
            with get_conn() as conn, conn.cursor() as cur:
                for note, ts, tid in notes:
                    # 追加而不是覆盖：review_note 可能已有人工写的意见
                    cur.execute(
                        "UPDATE sa_analysis_techniques SET review_note = %s, "
                        "reviewed_at = %s WHERE id = %s",
                        (note, ts, tid))
                conn.commit()
            result["written"] = len(notes)
        except Exception as exc:                              # noqa: BLE001
            result["errors"].append("写 review_note 失败: %s" % exc)
            result["ok"] = False
    return result


def suggest_rejections(items: list[dict], min_score: float | None = 6.0) -> list[dict]:
    """从判词里挑出「建议否掉」的条目（**只是建议**，执行仍走人工审核端点）。

    双重确认：verdict 说是跑题/重复，**且它自己给的建议分数也低**
    （默认 `<= 6.0`）。

    为什么要分数门槛：模型偶尔会把一条好技法误判成跑题。
    如果它一边判 `offtopic` 一边又给 9 分，那是**自相矛盾** ——
    这种条目不该被自动建议否掉，应当退回人看。
    """
    out = []
    for i in items:
        v = i.get("verdict")
        if v not in ("offtopic", "duplicate"):
            continue
        s = i.get("suggest_score")
        if min_score is not None and s is not None and float(s) > min_score:
            continue
        out.append({"id": i.get("id"), "name": i.get("name"), "verdict": v,
                    "suggest_score": s, "reason": i.get("reason"),
                    "recommended_status": VERDICT_RECOMMEND[v]})
    return out


# --------------------------------------------------------------------------
# 小工具（与 analysis_learn 保持一致的实现，避免两套行为）
# --------------------------------------------------------------------------

def _flat(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        return " ".join(x for x in (str(k), _flat(val)) for k, val in v.items() if x)
    if isinstance(v, (list, tuple, set)):
        return " ".join(_flat(x) for x in v if _flat(x))
    return str(v)


def _to_float(v, default):
    try:
        return float(str(v).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return default


__all__ = [
    "DEFAULT_JUDGE_PROMPT", "JUDGE_USER", "VERDICTS", "VERDICT_LABEL",
    "VERDICT_RECOMMEND", "REVIEW_NOTE_PREFIX", "SKILL_CANDIDATES",
    "load_judge_prompt", "fetch_for_judge", "find_duplicates",
    "build_catalog", "build_items_block", "parse_verdicts", "judge_batch",
    "run_judge", "suggest_rejections",
]


# --------------------------------------------------------------------------
# 适用性打标：解析 + 执行（2026-10-08）
# --------------------------------------------------------------------------

def parse_scope_verdicts(text: str, n_items: int) -> list[dict]:
    """解析适用性打标输出。与 parse_verdicts 同一纪律：宁可少一条。

      - ITEM 编号必须落在 1..n 且不重复
      - SCOPE 必须在 SCOPES 里，否则整块丢弃（不猜）
      - APPLICABLE 交给 analysis_learn.parse_applicable 校验：
        不认识的标签会被丢掉，全不认识则退化成「通用」——
        一个对不上的标签等于没筛，但**看起来像筛过了**，比没标签更危险。
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[int] = set()
    for part in re.split(r"###ITEM", text)[1:]:
        f = {"ITEM": "", "SCOPE": "", "APPLICABLE": "", "REASON": ""}
        cur = None
        for line in part.strip().splitlines():
            s = line.strip()
            m = re.match(r"^###([A-Z_]+)\s*(.*)$", s)
            if m and m.group(1) in f:
                cur = m.group(1)
                f[cur] = m.group(2).strip()
            elif cur:
                f[cur] += (("\n" if f[cur] else "") + s)
            elif s:
                cur = "ITEM"
                f["ITEM"] = s
        try:
            idx = int(_flat(f["ITEM"]).strip().splitlines()[0])
        except (ValueError, IndexError):
            continue
        if not (1 <= idx <= n_items) or idx in seen:
            continue
        scope = _flat(f["SCOPE"]).strip().lower()
        if scope not in SCOPES:
            continue
        seen.add(idx)
        import analysis_learn as _al
        applicable = ("[]" if scope != "sector"
                      else _al.parse_applicable(f["APPLICABLE"]))
        if scope == "sector" and applicable == "[]":
            # 声称行业专属却给不出任何有效标签 -> 判据没落地，
            # 当成通用处理（宁可少筛，不要假装筛过）
            scope = "generic"
        out.append({"idx": idx, "scope": scope,
                    "applicable": applicable,
                    "reason": _flat(f["REASON"]).strip()[:200]})
    return out


def tag_applicable_batch(llm_fn, items: list[dict], prompt: str = "",
                         max_tokens: int | None = None) -> list[dict]:
    """打标一批。`llm_fn(system, user, max_tokens=...)` 由调用方注入。

    ⚠️ `max_tokens` **按批大小算**，不要写死（2026-10-08 实测踩到）：
    3 条 @3000 正常，**12 条 @3000 直接 `stop_reason=max_tokens` 返回空**，
    症状是「12 条全部解析不出来」，看着像 prompt/解析坏了，其实是输出被截断
    —— 每条要写 ITEM/SCOPE/APPLICABLE/REASON 四行，条数一多预算就不够。
    （同 AGENTS.md 记的「模型先写思维链再撞上限」那一类，只是这里
    原因是**条数**而不是抖动。）

    撞上限时**按比例加倍重试一次**，仍失败就整批放弃（宁可少打标，
    也不能把「空输出」当成「全部通用」写回去）。
    """
    system = prompt or APPLICABLE_SYSTEM
    user = APPLICABLE_USER.format(
        n=len(items), catalog=build_catalog(items),
        items=build_items_block(items))
    if max_tokens is None:
        max_tokens = 1200 + 900 * len(items)
    raw = ""
    budget = int(max_tokens)
    for attempt in range(2):
        try:
            raw = llm_fn(system, user, max_tokens=budget)
        except Exception as exc:                      # noqa: BLE001
            raw = ""
            if attempt == 1:
                raise
        if raw:
            break
        # 空输出和异常**都要**加倍预算后再试一次。
        # 原来只在 except 分支里加倍，于是「返回空串」这条路预算不变、
        # 白白重试一次一样的请求 —— 实测 budgets=[12000, 12000]。
        if attempt == 0:
            budget *= 2
    parsed = parse_scope_verdicts(raw or "", len(items))
    by_idx = {p["idx"]: p for p in parsed}
    out = []
    for i in range(len(items)):
        p = by_idx.get(i + 1)
        out.append({
            "id": items[i]["id"],
            "name": _flat(items[i].get("name")),
            "scope": (p or {}).get("scope", ""),
            "applicable": (p or {}).get("applicable"),
            "reason": (p or {}).get("reason", ""),
            "parsed": bool(p),
        })
    return out


def run_applicable_tag(deps: dict, statuses=("active", "pending"),
                       batch: int = DEFAULT_BATCH,
                       max_items: int = DEFAULT_MAX_ITEMS,
                       prompt: str = "", dry_run: bool = True,
                       llm_fn=None) -> dict:
    """给存量技法打适用性标签。

    `dry_run=True`（默认，**与 judge / learn 同一纪律**）：只读不写 ——
    判据要花钱调 LLM。`dry_run=False` 才写 `applicable` 列
    （**仍然不改 status**，离题条目只是被标出来供人否决）。

    只处理 `applicable` 为空数组的（= 通用但未确认），已经打过标的不重跑。
    """
    get_conn = deps["get_conn"]
    conf = dict(deps.get("conf") or {})
    if llm_fn is None:
        import llm_advisor

        def llm_fn(s, u, max_tokens=3000):
            return llm_advisor.ask(s, u, conf=conf, max_tokens=max_tokens,
                                   thinking=False)
    system = prompt or APPLICABLE_SYSTEM
    import analysis_learn as _al
    result = {"ok": True, "dry_run": bool(dry_run), "tagged": 0,
              "scanned": 0, "summary": {}, "items": [], "errors": []}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, category, rule, indicators, evidence, "
            "       COALESCE(NULLIF(applicable,''),'[]') AS applicable "
            "  FROM sa_analysis_techniques WHERE status = ANY(%s) "
            "   AND COALESCE(NULLIF(applicable,''),'[]') = '[]' "
            " ORDER BY score DESC, learned_at DESC LIMIT %s",
            (list(statuses), int(max_items)))
        items = _rows_as_dicts(cur, cur.fetchall())
        result["scanned"] = len(items)
        if not items:
            return result
        if dry_run:
            result["items"] = items
            return result
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            try:
                got = tag_applicable_batch(llm_fn, chunk, system)
            except Exception as exc:
                result["errors"].append("batch@%d: %s" % (i, exc))
                continue
            for g in got:
                if not g["parsed"] or g["scope"] not in SCOPES:
                    continue
                cur.execute(
                    "UPDATE sa_analysis_techniques SET applicable=%s, "
                    "  review_note = CASE WHEN review_note = '' THEN %s "
                    "                       ELSE review_note || ' | ' || %s END "
                    " WHERE id=%s",
                    (g["applicable"] or "[]",
                     "scope:%s" % g["scope"],
                     "scope:%s(%s)" % (g["scope"], (g["reason"] or "")[:80]),
                     g["id"]))
                result["tagged"] += 1
                k = g["scope"]
                result["summary"][k] = result["summary"].get(k, 0) + 1
        conn.commit()
        result["items"] = items
    return result
