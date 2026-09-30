# -*- coding: utf-8 -*-
"""把 LLM 提示词从「JSON」改成「分段标记」格式。

为什么换格式（这是被实测逼出来的，不是偏好）
------------------------------------------
最早我让模型输出 JSON，配一个自己写的容错解析器（repair_json，
那个解析器本身留下了，单引号/尾逗号/Python 字面量都能修，见下）。
但真实跑下来，一篇 4213 字符的输出**修了三轮 still 解析不了**：
本地跑的 120B 模型（nemotron-3-super-120b）会把一句话拆成
`'前半'；'中段'；'尾段'。"` 这种形态 —— 字符串外有裸文本、结尾用
另一种引号收尾。它不是「JSON 写错了一点」，是**根本按 JSON 的规则在写**。

继续修解析器就是在跟一个不打算守规矩的输出格式搏斗，每轮还要烧钱。
所以改成**无嵌套、无引号、无逗号分隔**的分段格式：

    ###STEP
    1. 做什么 | 怎么做 | 依据
    2. …

解析就是按 `###KEY` 切段，**没有任何可能出错的转义**。120B 模型对这个
格式的成功率远高于 JSON。

代价：多写一个解析器。收益：把一整类「解析失败」从根上消掉。

如果模型仍然输出了 JSON（它有时会自作聪明），parse_sections 会识别出来
并交给 repair_json 兜底 —— 两条路都留着。
"""
import re

# 段名 -> 内部字段名
SECTION_KEYS = {
    "TITLE": "title_zh",
    "SUMMARY": "summary",
    "TYPE": "strategy_type",
    "STEP": "steps",
    "UNIVERSE": "universe",
    "PARAM": "params",
    "PERF": "perf_claimed",
    "APPLICABLE": "applicable",
    "UNSUITABLE": "unsuitable",
    "RISK": "risk_notes",
    "DEPS": "dependencies",
    "UNCERTAINTY": "uncertainty",
    "RESEARCH": "needs_research",
    "SCORE": "portable",
}

HEAD_RE = re.compile(r"^#{2,4}\s*([A-Z][A-Z_]{1,20})\s*$", re.M)


def parse_sections(text: str) -> dict:
    """把分段格式的输出解析成 dict。认不出来就返回 {}。

    每段的解析规则（都刻意做得宽松）：
      - 列表段：每行去掉 `-`/`*`/数字前缀就是一条
      - PARAM / PERF：`键: 值` 一行一个，键重复不覆盖（保留第一条）
      - STEP：`序号 | 做什么 | 怎么做 | 依据`，按 | 切，缺项留空
      - RESEARCH：`true|要搜什么` 或单独一个 true/false
      - SCORE：`分数|理由`
    """
    if not text:
        return {}
    heads = list(HEAD_RE.finditer(text))
    if not heads:
        return {}
    out: dict = {}
    for idx, m in enumerate(heads):
        key = SECTION_KEYS.get(m.group(1))
        if not key:
            continue
        end = heads[idx + 1].start() if idx + 1 < len(heads) else len(text)
        body = text[m.end():end].strip()
        if key in out:            # 同名段出现了两次，合并而不是覆盖
            out[key] = out[key] + "\n" + body
        else:
            out[key] = body

    res: dict = {}
    res["title_zh"] = _flat(out.get("title_zh", ""))[:200]
    res["summary"] = _flat(out.get("summary", ""))[:3000]
    res["strategy_type"] = _flat(out.get("strategy_type", ""))[:32]
    res["steps"] = _parse_steps(out.get("steps", ""))
    res["universe"] = _lines(out.get("universe", ""))[:30]
    res["params"] = _kv(out.get("params", ""))
    res["perf_claimed"] = _kv(out.get("perf_claimed", ""))
    res["applicable"] = _lines(out.get("applicable", ""))[:20]
    res["unsuitable"] = _lines(out.get("unsuitable", ""))[:20]
    res["risk_notes"] = _lines(out.get("risk_notes", ""))[:20]
    res["dependencies"] = _lines(out.get("deps", ""))[:20]
    res["uncertainty"] = _flat(out.get("uncertainty", ""))[:2000]

    # RESEARCH：true/false + 补搜方向
    r = out.get("needs_research", "")
    flag = r.lower().startswith("true") or r.startswith("是") or r.strip() in ("Y", "y")
    res["needs_research"] = flag
    hint = re.sub(r"^\s*(true|false|是|否|y|n)\s*[|｜:：]?\s*", "", r, flags=re.I)
    res["research_hint"] = hint.strip()[:1000] if flag else hint.strip()[:1000]

    # SCORE：分数|理由
    sc = out.get("portable", "").strip()
    m = re.match(r"\s*(\d)\s*[|｜:：]?\s*(.*)", sc, re.S)
    if m:
        res["portable_score"] = m.group(1)
        res["portable_why"] = m.group(2).strip()[:1500]
    else:
        res["portable_score"] = "0"
        res["portable_why"] = sc[:1500]
    return res


def _flat(s: str) -> str:
    """段落正文压成一行（去掉列表符号和多余空白）。"""
    s = re.sub(r"^\s*[-*·]\s*", "", s or "", flags=re.M)
    return re.sub(r"\s+", " ", s).strip()


def _lines(s: str) -> list[str]:
    """列表段：每行一条。"""
    out = []
    for ln in (s or "").splitlines():
        ln = re.sub(r"^\s*[-*·]?\s*(\d+[.、)]\s*)?", "", ln).strip()
        if ln:
            out.append(ln)
    return out


def _kv(s: str) -> dict:
    """`键: 值` 解析。整段没有冒号就当成 {'': 整段}（不丢内容）。"""
    out: dict = {}
    for ln in (s or "").splitlines():
        ln = re.sub(r"^\s*[-*·]?\s*", "", ln).strip()
        if not ln:
            continue
        m = re.split(r"[:：]", ln, 1)
        if len(m) != 2 or not m[0].strip():
            continue
        k = m[0].strip()[:60]
        if k not in out:         # 重复键保留第一条，不让后面的覆盖
            out[k] = m[1].strip()[:300]
    if not out and s and s.strip():
        return {"": s.strip()[:300]}
    return out


def _parse_steps(s: str) -> list[dict]:
    """`序号 | 做什么 | 怎么做 | 依据` 逐行解析。

    也容忍用制表符或多个空格当分隔（模型经常不按 | 来）。
    """
    out = []
    for i, ln in enumerate((s or "").splitlines(), 1):
        raw = ln.strip()
        if not raw:
            continue
        parts = re.split(r"\s*[|｜]\s*|\t+|\s{3,}", raw)
        parts = [p.strip() for p in parts if p.strip() != ""]
        if not parts:
            continue
        no = i
        if re.match(r"^\d+[.、)]?$", parts[0]):
            no = int(re.sub(r"\D", "", parts[0]) or i)
            parts = parts[1:]
        if not parts:
            continue
        out.append({
            "no": no,
            "what": parts[0][:300],
            "detail": (parts[1] if len(parts) > 1 else "")[:1200],
            "evidence": (parts[2] if len(parts) > 2 else "")[:300],
        })
    return out
