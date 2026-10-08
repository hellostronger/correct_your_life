# -*- coding: utf-8 -*-
"""分析技法学习库：从公众号文章里抽「别人用了什么指标、怎么判读」，
落 `sa_analysis_techniques`，再按需注入报告/决策 prompt。

为什么要有这个
--------------
`sa_mp_articles` 里有一批 Greenwald 价值投资分析报这类文章，套路高度固定
（实测样本：正常化 EBIT → EPV 零增长锚 → 三段式净预期回报 → vs WACC → 评级）。
它们的技术含量在于**判读规则**（"TTM PE 便宜但分母是爆款高峰，应换五年均值
正常化"），而不是某个具体数字。这套规则现在只存在于人读的文本里，
而我们的 LLM 决策 prompt 完全不知道它。

三个不可省的设计约束（都是踩过坑才有的）
----------------------------------------
1. **必须带原文依据**（`evidence`）。像 `strategy_extract.py` 一样严格区分
   「文章写了什么」和「我推测的」：每条技法要指回原文片段。没有依据的
   技法**丢弃**，不降权保留 —— 否则我们会把模型的先验当成别人的经验注入决策。

2. **人工审核 / AI 自进化是同一个字段的两端**（`status`），不是两套代码：
   - `pending`  → 待审核（人工审核模式下抽取的产物停在这）
   - `active`   → 生效（AI 自进化模式下 LLM 打分达标就直接进这里）
   - `rejected` → 人否掉
   审核 = 改 status。抽取 = 写 pending/active。**没有第二条路径**，
   所以「AI 学歪了」时人只要一条 UPDATE 就能止损，不必改代码。

3. **注入必须可回滚且有上限**（`build_injection_block`）：
   - point-in-time：只注入 `learned_at <= 调用时刻` 的（防未来函数，
     与 `paper_memory.get_past_context` 同一纪律）
   - 只注入 `source_article_id` 在库里的（文章被删则技法自动失效）
   - 按 `score` 取前 N 条，单条裁剪，总长有硬上限 —— prompt 膨胀会让模型
     注意力被稀释，反而变差
   - 每条独立 try/except：一条脏数据不许弄丢整块（渲染层不该有能力丢数据）

不 import app（AGENTS.md 惯例，避免循环依赖 + 拖慢 import）：
连接/游标由调用方传入。LLM 出口复用 `llm_advisor.ask()`。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

# ---------------- 表结构（幂等 DDL，由调用方在 init_db 里跑） ----------------

DDL = """
CREATE TABLE IF NOT EXISTS sa_analysis_techniques (
    id                bigserial PRIMARY KEY,
    -- 来源文章（外键不建：文章可能先被清理，注入时按 id 存在性过滤）
    source_article_id bigint NOT NULL,
    source_url        text   NOT NULL DEFAULT '',
    source_title      text   NOT NULL DEFAULT '',
    -- 技法本体
    name              text   NOT NULL,          -- 技法名，如「正常化盈利」
    category          text   NOT NULL DEFAULT '',   -- 估值/技术/资金/基本面/风险
    rule              text   NOT NULL,          -- 判读规则（可直接注入 prompt）
    indicators        text   NOT NULL DEFAULT '[]',  -- JSON 数组：涉及的指标
    evidence          text   NOT NULL,          -- 原文依据片段（**必须非空**）
    -- 适用性（2026-10-08 加）：JSON 数组，['银行','保险'] 之类。
    -- 空数组 = 通用（任何 A 股都能套）。非空 = 只对这些行业有意义。
    -- 为什么加：实测注入的 12 条里约 5 条是行业专属（寿险 P/EV、
    -- 火电长协电价、半导体竞品全栈…），却一股脑灌进每只票的 prompt；
    -- 而表里原本**没有任何字段能表达「这条对谁适用」**。
    applicable        text   NOT NULL DEFAULT '[]',
    -- 审核与进化
    status            text   NOT NULL DEFAULT 'pending',
    score             real   NOT NULL DEFAULT 0,   -- 0~10，LLM 打分或人工打分
    review_note       text   NOT NULL DEFAULT '',
    mode              text   NOT NULL DEFAULT 'ai',   -- 产出它的模式：ai|manual
    hits              int    NOT NULL DEFAULT 0,     -- 被注入过几次（观测用）
    learned_at        timestamptz NOT NULL DEFAULT now(),
    reviewed_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_article_id, name)
);
CREATE INDEX IF NOT EXISTS idx_anal_tech_status_score
    ON sa_analysis_techniques (status, score DESC, learned_at);
-- 幂等 DDL 不等于能演进的 DDL：CREATE TABLE IF NOT EXISTS 对**已存在的表**
-- 不会补列，所以加列必须同时给 ALTER（check_ddl.py 专门查这一条）。
ALTER TABLE sa_analysis_techniques
    ADD COLUMN IF NOT EXISTS applicable text NOT NULL DEFAULT '[]';
"""

# ---------------- 抽取用的筛选启发式 ----------------

# 指标词表：只用于**筛候选文章**（粗筛），真正的技法判定交给 LLM。
# 词表刻意保守：宁可少抽，不可把纯新闻抽成技法。
INDICATOR_WORDS = [
    # 估值
    "PE", "PB", "PEG", "PS", "EV/EBIT", "EV/NOPAT", "DCF", "WACC", "ROIC", "ROE",
    "毛利率", "净利率", "现金流", "自由现金流", "股息率", "分红", "市值",
    "正常化", "周期", "资本成本", "永续增长", "安全边际", "内在价值", "估值",
    # 技术面
    "均线", "MACD", "KDJ", "RSI", "BOLL", "布林", "金叉", "死叉", "背离",
    "量价", "成交量", "换手率", "K线", "形态", "支撑", "压力", "趋势",
    # 资金/筹码
    "资金流", "北向", "龙虎榜", "筹码", "主力", "融资融券", "大宗交易",
    # 事件/风险
    "解禁", "增发", "减持", "质押", "商誉", "存货", "应收",
    # 情绪/交易
    "涨停", "连板", "题材", "仓位", "止损", "止盈", "回撤", "夏普", "回测",
    "阿尔法", "贝塔", "超额收益",
]

# 一眼就是「纯新闻/公告搬运」的特征，命中则直接跳过（不浪费 LLM 调用）
SKIP_PATTERNS = [
    re.compile(r"^【?(公告|快讯|早报|午报|晚报|涨停复盘|龙虎榜速览)】?"),
    re.compile(r"(上证指数|创业板指)\s*(收评|开盘|午评)"),
    re.compile(r"免责声明.{0,40}(本文|本报)"),
]

MIN_CHARS = 1500          # 太短的通常是快讯
MAX_CHARS = 30000         # 超长的截断送 LLM，省 token


def _flatten(v) -> str:
    """把 str/dict/list 摊平成纯文本。

    公众号正文里 emoji、特殊标记常以 dict/list 混入（AGENTS.md 记过
    `shortTitle` 是 `list[dict]` 的同类陷阱），str() 出来是 Python repr。
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        parts = []
        for k, val in v.items():
            parts.append(str(k))
            parts.append(_flatten(val))
        return " ".join(p for p in parts if p)
    if isinstance(v, (list, tuple, set)):
        return " ".join(_flatten(x) for x in v)
    return str(v)


def indicator_hits(text: str) -> list[str]:
    """文章命中的指标词（粗筛用，大小写不敏感但保留原词表形态）。"""
    low = (text or "").lower()
    out = []
    for w in INDICATOR_WORDS:
        wl = w.lower()
        if wl in low or (len(w) <= 3 and w in (text or "")):
            out.append(w)
    return out


def looks_like_analysis(title: str, text: str) -> bool:
    """粗筛：是否值得花一次 LLM 调用。保守 —— 宁可漏，不可把新闻当技法。"""
    title = title or ""
    text = text or ""
    if len(text) < MIN_CHARS:
        return False
    head = (title + "\n" + text[:400]).strip()
    for pat in SKIP_PATTERNS:
        if pat.search(head):
            return False
    hits = indicator_hits(text)
    # 至少 5 个不同指标词，且正文够长（长文通常才是分析而非快讯）
    return len(hits) >= 5


# ---------------- 抽取 prompt ----------------

EXTRACT_SYSTEM = """\
你在帮一个 **A 股个人自用系统**做「分析技法萃取」。读者只有一位使用者，
他要回答的问题是：**这些专业博主的文章里，反复在用哪些指标和判读套路？
我要把它们学会，用在自己的分析里。**

最重要的纪律：**只萃取文章里真实出现的技法，不许补充你自己的先验。**

为什么这条纪律最重要（实测背景）：这些文章里有大量「作者自述」和营销话术，
如果你把没写的方法当成作者的方法注入我的决策 prompt，我就会拿着**你编的**
规则去做真金白银的决策 —— 那比抽不出来糟糕得多。

所以：
1. 每条技法**必须**在 `EVIDENCE` 里给出原文依据（一句话即可，原样引用）。
2. 没有原文依据的想法，**直接丢弃**，不要写成「EVIDENCE: （无）」。
3. 「作者用了某指标」≠「该指标有效」。你只萃取**他怎么用的**。
4. 数字/结论（目标价、涨幅预测）**一律不萃取** —— 那是作者的结论，不是技法。
5. 判读规则要写成**可复用的句式**（能套到别的股票上），不要写成
   「本文认为 XX 股低估」这种一次性表述。
6. **适用范围必须是 A 股/港股个股的二级市场分析**，否则整条丢弃。
   判据是「这条规则能不能套到任意一只 A 股上」。以下都**不算**，哪怕
   方法本身很硬：海外房产/REITs 尽调（托管账户、期现房成交比）、
   一级市场募资与 LP 出资决策（DPI/TVPI、GP 筛选）、**LLM/机器学习
   评测方法论**（双模型交叉验算、任务分解、打分门禁）、纯学术统计。

   ⚠️ 实测踩过：这些文章常含 PE/估值/回测/回撤 等指标词，能通过粗筛；
   于是它们被抽成 8~10 分的「硬方法」，按 score 排序反而**挤掉了真正
   有用的 A 股技法**（12 条注入里 8 条是这类）。方法论本身的「技术含量高」
   和「对我做 A 股决策有用」是两件事，**后者才是这里要打的分**。

输出格式：**严格按下面的分段标记输出，不要 JSON，不要代码围栏，
不要任何开场白、解释或总结文字。每条技法一个块。**

###TECHNIQUE
技法名（8 字以内，能概括这套方法）
###CATEGORY
分类，从这些里选一个：估值 / 技术面 / 资金面 / 基本面 / 事件 / 风险 / 方法论
###RULE
判读规则，一到两句，必须可迁移到任意股票。写清「什么条件 → 得出什么判断」。
###APPLICABLE
这条技法**只对哪些行业有意义**。逗号分隔，或写「通用」。
- 判据：这条规则里如果出现了**只有特定行业才有的东西**，就写那个行业；
  没有行业专属前提、任何 A 股都能套，就写「通用」。
- 行业名用这些里的：银行 / 保险 / 券商 / 地产 / 医药 / 半导体 / 消费 /
  白酒 / 电力火电 / 核电 / 煤炭 / 钢铁 / 有色 / 化工 / 汽车 / 军工 /
  计算机 / 通信 / 传媒 / 农业 / 建筑 / 交运 / 机械 / 家电 / 纺服 / 零售
- 例：「寿险用 P/EV 而非 P/B」→ `保险`；「用长协电价与动力煤价判断火电」→ `电力火电, 煤炭`；
  「正常化 ROE 剔除投资收益」→ `银行, 券商, 保险`；「DCF 折现」→ `通用`
###INDICATORS
涉及的指标，逗号分隔（如：正常化EBIT, ROIC, WACC）。没有就留空行。
###EVIDENCE
原文依据，原样引用一句话。**这一行不许为空。**
###SCORE
这条技法**对 A 股/港股个股分析的可复用性**打分，0~10 的整数：
8+ = 能直接套到任意一只 A 股上、且有明确判据的硬方法；
5-7 = 常规分析套路，有用但不够硬；
3 以下 = 与个股分析无关（海外房产、一级市场募资、LLM 评测方法论…）
或泛泛而谈。**打的是「对我做 A 股决策有没有用」，不是「这套方法技术上
厉不厉害」** —— 一套很厉害但用不到 A 股上的方法，就是 3 分。

最多萃取 8 条。质量优先于数量：宁可只输出 2 条扎实的，也不要凑数。
"""

EXTRACT_USER = """\
以下是待分析的公众号文章。请按系统提示的格式萃取其中的分析技法。

【标题】{title}
【公众号】{mp_name}
【发布于】{published_at}

【正文】
{body}
"""


# ---------------- 解析 ----------------

_SECTION_ORDER = ["TECHNIQUE", "CATEGORY", "RULE", "APPLICABLE",
                  "INDICATORS", "EVIDENCE", "SCORE"]
_VALID_CATEGORIES = {"估值", "技术面", "资金面", "基本面", "事件", "风险", "方法论"}

# 允许的行业标签（prompt 里给了同一份清单，这里做校验）。
# 不在表内的标签**丢弃**而不是放行 —— 标签是用来过滤的，
# 放进来一个「半导体制造」这种没法和股票行业对上的标签等于没打。
# 「通用」是保留字：解析成空数组，表示任何 A 股都能套。
GENERIC_TAG = "通用"
VALID_INDUSTRIES = {
    "银行", "保险", "券商", "地产", "医药", "半导体", "消费", "白酒",
    "电力火电", "核电", "煤炭", "钢铁", "有色", "化工", "汽车", "军工",
    "计算机", "通信", "传媒", "农业", "建筑", "交运", "机械", "家电",
    "纺服", "零售",
}


def parse_applicable(raw: str) -> str:
    """把 ###APPLICABLE 那一行规整成 JSON 数组字符串。

    「通用」/空/认不出的 → `[]`（= 通用，任何票都能注入）。
    非通用但全都不在 VALID_INDUSTRIES 里 → 也当 `[]`，
    **宁可当通用也不要留一个对不上的标签**：标签对不上等于过滤失效，
    而失效的标签比没有标签更危险（看起来筛过了，其实没筛）。
    """
    s = _flatten(raw).strip()
    if not s or GENERIC_TAG in s or s in ("通用", "ALL", "all"):
        return "[]"
    parts = [x.strip() for x in re.split(r"[,，、;；/|\s]+", s) if x.strip()]
    keep = []
    for p in parts:
        p = p.replace("行业", "").strip()
        if p in VALID_INDUSTRIES and p not in keep:
            keep.append(p)
        elif GENERIC_TAG in p:
            return "[]"
    return json.dumps(keep, ensure_ascii=False)


def parse_techniques(text: str) -> list[dict]:
    """解析 LLM 输出的分段标记 → 技法列表。**丢弃没有 evidence 的块**。

    这是「严格区分文章写了什么 vs 我推测的」的执行点：
    宁可少一条技法，也不接受模型编的。
    """
    if not text:
        return []
    blocks = []
    parts = re.split(r"###TECHNIQUE", text)
    for part in parts[1:]:
        part = part.strip()
        if not part:
            continue
        fields = {"TECHNIQUE": "", "CATEGORY": "", "RULE": "", "APPLICABLE": "",
                  "INDICATORS": "", "EVIDENCE": "", "SCORE": ""}
        current = None
        for line in part.splitlines():
            stripped = line.strip()
            m = re.match(r"^###([A-Z_]+)\s*(.*)$", stripped)
            if m and m.group(1) in fields:
                current = m.group(1)
                fields[current] = m.group(2).strip()
            elif current:
                fields[current] += ("\n" if fields[current] else "") + stripped
            elif stripped:
                # split('###TECHNIQUE') 之后的第一行就是技法名本身，
                # 它前面没有标记可以匹配 —— 不在这里接住它，所有字段都会是空的
                # （离线断言实测：漏了这一行，34 项里 8 项失败且症状是「一条都抽不出」）。
                current = "TECHNIQUE"
                fields["TECHNIQUE"] = stripped
        fields = {k: _flatten(v).strip() for k, v in fields.items()}
        blocks.append(fields)

    out = []
    for f in blocks:
        name = f["TECHNIQUE"].strip()[:60]
        rule = f["RULE"].strip()
        evidence = f["EVIDENCE"].strip()
        if not name or not rule:
            continue
        # ⚠️ 关键：没有原文依据的一律丢弃，不降权保留
        if not evidence or evidence in ("（无）", "(无)", "无", "N/A", "n/a"):
            continue
        # 依据太短的多半是模型编的占位符
        if len(evidence) < 8:
            continue
        cat = f["CATEGORY"].strip() or "方法论"
        for valid in _VALID_CATEGORIES:
            if valid in cat:
                cat = valid
                break
        else:
            cat = "方法论"
        try:
            score = float(f["SCORE"].strip().split()[0])
        except (ValueError, IndexError):
            score = 5.0
        score = max(0.0, min(10.0, score))
        inds = [x.strip() for x in re.split(r"[,，、;；]", f["INDICATORS"]) if x.strip()]
        out.append({
            "name": name, "category": cat, "rule": rule,
            "applicable": parse_applicable(f["APPLICABLE"]),
            "indicators": json.dumps(inds[:12], ensure_ascii=False),
            "evidence": evidence[:800], "score": score,
        })
    return out


# ---------------- 抽取落库 ----------------

def upsert_techniques(cur, article_id: int, url: str, title: str,
                      techniques: list[dict], mode: str = "ai",
                      auto_activate_score: float = 7.0) -> int:
    """写技法。`mode` 决定 status：

    - `mode='manual'` → 一律 `pending`（人工审核）
    - `mode='ai'`    → `score >= auto_activate_score` 直接 `active`，
                       其余 `pending`（AI 自进化，但低分的不直接生效）

    UNIQUE(source_article_id, name) + ON CONFLICT DO UPDATE：同一篇文章
    重跑会更新而不是堆积（抽取会重试，同一篇文章可能跑两次）。
    """
    n = 0
    for t in techniques:
        status = "active" if (mode == "ai"
                              and t["score"] >= auto_activate_score) else "pending"
        cur.execute(
            "INSERT INTO sa_analysis_techniques "
            "(source_article_id, source_url, source_title, name, category, rule, "
            " applicable, indicators, evidence, status, score, mode) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (source_article_id, name) DO UPDATE SET "
            " category=EXCLUDED.category, rule=EXCLUDED.rule, "
            " applicable=EXCLUDED.applicable, "
            " indicators=EXCLUDED.indicators, evidence=EXCLUDED.evidence, "
            " score=EXCLUDED.score, mode=EXCLUDED.mode",
            (article_id, (url or "")[:500], (title or "")[:300],
             t["name"], t["category"], t["rule"],
             t.get("applicable") or "[]",
             t["indicators"], t["evidence"], status, t["score"], mode))
        n += 1
    return n


# ---------------- 注入 ----------------

MAX_INJECT_TECHNIQUES = 12
MAX_INJECT_CHARS = 2600
PER_ITEM_CHARS = 320


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_active(cur, as_of, limit: int, sectors: list[str] | None = None,
                 generic_only: bool = False):
    """取 active 且来源文章仍在的技法，按 score 降序。

    「来源文章仍在」这条过滤的理由：技法是**从某篇文章里学的**。文章被
    清理后我们无法复核依据，原样注入就成了不可审计的先验 —— 与 AGENTS.md
    强调的「每条规则要能指回原文」冲突。

    sectors（2026-10-08 加）：给了就只取「通用」或「命中该行业」的技法。
    `applicable` 存的是 JSON 数组，用 `jsonb` 运算而不是 LIKE ——
    LIKE '%银行%' 会把「银行」当成「银行业绩」也命中，且无法表达多标签。

    ⚠️ 排序里 `learned_at DESC` 只是同分决胜，**不是相关性**：
    实测 161 条 active 里有 45 条同为 9.0，取 12 条时同分区间基本按
    「最近学的」选。所以 score 相同时不要指望它挑到更相关的。
    """
    params: list = [as_of]
    where = ("t.status = 'active' AND t.learned_at <= %s "
             "  AND EXISTS (SELECT 1 FROM sa_mp_articles a "
             "                WHERE a.id = t.source_article_id) ")
    # 兼容旧库：该列可能还不存在（DDL 未跑）。用一次探测决定要不要引用它。
    have_col = _has_applicable_column(cur)
    if generic_only and have_col:
        where += " AND COALESCE(NULLIF(t.applicable,''),'[]') = '[]' "
    elif sectors and have_col:
        # 通用(空数组) 或 与目标行业有交集
        where += (" AND (COALESCE(NULLIF(t.applicable,''),'[]') = '[]' "
                  "      OR EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                  "          COALESCE(NULLIF(t.applicable,''),'[]')::jsonb) e "
                  "          WHERE e = ANY(%s))) ")
        params.append(list(sectors))
    params.append(limit)
    cols = ("t.id, t.name, t.category, t.rule, t.score, t.evidence, "
            "       t.source_article_id, t.source_title")
    if have_col:
        cols += ", t.applicable"
    cur.execute(
        "SELECT " + cols +
        " FROM sa_analysis_techniques t WHERE " + where +
        " ORDER BY t.score DESC, t.learned_at DESC LIMIT %s",
        tuple(params))
    return cur.fetchall() or []


_APPLICABLE_COL_CACHE: dict[str, bool] = {}


def _has_applicable_column(cur) -> bool:
    """该表有没有 applicable 列（老库可能还没跑 ALTER）。

    缓存起来是因为它在 fetch_active 的热路径上，而
    information_schema 查询不便宜。缓存的是**列存在性**，
    跑完 ALTER 后进程内需要重启才会重查 —— 那种情况下读出来的是
    `applicable='[]'` 的默认值（= 通用），行为退回改动前，不会出错。
    """
    key = "has_applicable"
    if key in _APPLICABLE_COL_CACHE:
        return _APPLICABLE_COL_CACHE[key]
    ok = False
    try:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            " WHERE table_name='sa_analysis_techniques' AND column_name='applicable'")
        ok = bool(cur.fetchone())
    except Exception:
        ok = False
    _APPLICABLE_COL_CACHE[key] = ok
    return ok


def build_injection_block(cur, limit: int = MAX_INJECT_TECHNIQUES,
                          as_of=None, max_chars: int = MAX_INJECT_CHARS,
                          bump_hits: bool = False,
                          sectors: list[str] | None = None,
                          generic_only: bool = False) -> str:
    """组装注入 prompt 的技法文本。**任何一条坏了都不许影响其余**。

    过滤链（每一条都有存在的理由）：
      1. `status='active'`           —— 只有生效的才注入（pending 等人工审核）
      2. `learned_at <= as_of`       —— point-in-time，防未来函数
      3. 来源文章仍在 `sa_mp_articles` —— 文章没了，技法随之失效
      4. `applicable` 命中（可选）    —— 行业专属的技法别灌给不相干的票
      5. 按 score 降序取 limit 条

    sectors / generic_only（2026-10-08 加）：注入块目前是**整篇报告共用**
    一段文本（`_anal_tech_block` 只调一次），拿不到「当前这只票是什么行业」。
    所以没传 sectors 时默认 `generic_only=True`：宁可少注入，也不要把
    「寿险用 P/EV」灌进贵州茅台。**按票过滤需要 per-stock 注入**，
    那是 daily_reports 的结构改动，尚未做 —— 传 sectors 才会启用。
    """
    if as_of is None:
        as_of = datetime.now()
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        as_of = datetime.combine(as_of, datetime.min.time())
    try:
        # 没给 sectors 就退化成「只注入通用技法」——共用块拿不到行业信息
        if sectors is None and not generic_only:
            generic_only = True
        rows = fetch_active(cur, as_of, limit, sectors=sectors,
                            generic_only=generic_only)
    except Exception as exc:
        print(f"[anal_tech] 注入查询失败（不注入）: {exc}", flush=True)
        return ""

    if not rows:
        return ""

    lines = ["【已学习的分析技法（来自历史文章，经审核的判读规则）】"]
    total = len(lines[0])
    used_ids = []
    for r in rows:
        try:
            d = dict(r) if not isinstance(r, dict) else r
            name = _flatten(d.get("name"))[:40]
            rule = _flatten(d.get("rule"))
            cat = _flatten(d.get("category"))[:8]
            if not rule:
                continue
            head = f"- {cat}·{name}"
            budget = max(60, PER_ITEM_CHARS - len(head))
            body = rule[:budget]
            if len(rule) > budget:
                body += "…"
            item = f"{head}：{body}"
            if total + len(item) > max_chars:
                break
            lines.append(item)
            total += len(item)
            if d.get("id") is not None:
                used_ids.append(d["id"])
        except Exception as exc:
            # 一条渲染失败只影响它自己（渲染层不该有能力弄丢数据）
            print(f"[anal_tech] 单条技法渲染失败（跳过）: {exc}", flush=True)
            continue

    if len(lines) == 1:
        return ""
    lines.append("（用这些技法**检查你的分析是否漏了关键维度**，"
                 "但不要求逐条套用；与本股情况不符就说明理由后跳过）")

    if bump_hits and used_ids:
        try:
            cur.execute("UPDATE sa_analysis_techniques SET hits = hits + 1 "
                        "WHERE id = ANY(%s)", (used_ids,))
        except Exception as exc:
            print(f"[anal_tech] hits 计数失败（不影响注入）: {exc}", flush=True)

    return "\n".join(lines)
