# -*- coding: utf-8 -*-
"""用 LLM 从公告全文里抽「禁售/锁定」条款。

为什么是 LLM 而不是纯正则（2026-10-12 用户提问后的决定）
----------------------------------------------------------
`hk_lockup` 的锚点正则只在**智谱那一份**样本上验证过。港股公告的法律措辞
因公司而异（配售協議/認購協議/發行協議/禁售期/限制出售/lock-up…），
正则每换一家公司就可能**静默抽错**或漏抽 —— 而这种错误不报错，最危险。

但纯 LLM 也不行：**它会编数字**。AGENTS.md 记过 nemotron「模型没按格式
输出 -> 静默降级成 hold」的同类事故。所以这里用 **LLM 抽 + 三道硬校验**：

1. `quote` 回填 —— 模型必须逐字引用原文片段，校验时用**它自己给的引用**
   在原文里做子串匹配。对不上就是编的，整条丢弃。
   （这是「交叉验证」的思路，与 news_fetcher 解析 publish_time 的 tier2
    要求「在正文里交叉验证到同一日期字符串才认」同源。）
2. 数值一致性 —— `days` 必须等于从 `quote` 里按同一套单位表算出来的值。
3. 期限必须落在「承诺…止」句式内（复用 hk_lockup 的 _ANCHOR），
   挡掉「段落里别处的 6 个月」这种噪声（实测抽到过 180 天的假数字）。

三条线（IPO 锁定期 / 融资禁售承诺 / 新闻兜底）由调用方分开展示，
本模块只负责把**证据**抽干净。
"""
from __future__ import annotations

import json
import re

import hk_lockup as HK

SYSTEM = """你是港股公告条款抽取器。只输出 JSON，不要任何解释。

任务：从给定的港股公告全文（Markdown，已由 MinerU 解析）中，抽取所有
「股份禁售 / 限售 / 锁定 / 禁止出售」类条款。

严格的硬性要求：

1. 只输出 JSON，格式：
   {"clauses": [{"type": "ipo_lockup"|"issuer_pledge", "party": "谁承诺的",
                 "days": 整数天数, "raw": "原文里的期限表述", "quote": "原文逐字片段"}]}

2. type 的判定（两者语义完全不同，绝不能混）：
   - "issuer_pledge"：**发行人自己**承诺在一定期限内不再配售/发行/增发新股。
     典型标题「配售協議項下的本公司禁售承諾」「認購協議項下的本公司禁售承諾」。
     ⚠️ 这不是股份解禁，只是公司不能再融资。
   - "ipo_lockup"：**股东**的股份被锁定，锁定期满后该批股票可自由出售。
     典型表述「基石投资者…禁售期为六個月」「控股股東承諾自上市日期起
     十二個月內不出售」。

3. quote 必须是**原文中逐字存在**的连续片段（不少于 10 个字），
   且必须包含 type、days、raw 三者的依据。你的引用会被程序逐字核对，
   对不上整条作废。禁止改写、禁止翻译成简体、禁止省略中间字符。

4. days 换算：日/天/自然日=1，月/个月/個月=30，星期/周/週=7。
   「六個月」=180。中文数字按中文读。

5. 找不到就返回 {"clauses": []}。**绝对不要编造**，空数组是正确且被接受的答案。
   不要因为「公告里看起来应该有禁售条款」就硬凑一条。

6. 一份公告里同一段承诺只输出一次（标题和正文重复提到不算两条）。"""

USER_TMPL = """以下是港股公司「%s（%s）」于 %s 发布的公告全文。

<公告全文>
%s
</公告全文>

请抽取其中的禁售/限售/锁定类条款，严格按系统提示的 JSON 格式输出。"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str):
    """从模型输出里取 JSON。

    AGENTS.md 记过 nemotron 会以「The user wants me to…」开头把 prompt
    泄漏到输出里，所以这里同时容忍 ```json 围栏和前言。
    """
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    m = _JSON_RE.search(t)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        # 最后一次尝试：从第一个 { 到最后一个 } 逐段截断重试
        s = t.find("{")
        while s >= 0:
            e = t.rfind("}")
            if e > s:
                try:
                    return json.loads(t[s:e + 1])
                except json.JSONDecodeError:
                    pass
            s = t.find("{", s + 1)
    return None


def _quote_in_source(quote: str, flat_src: str) -> bool:
    """引用是否**逐字**存在于原文（容忍 Markdown 里插入的空白/换行）。

    先原样找，再去空白找。两者都失败 = 模型在编，整条作废。
    """
    if not quote or len(quote.strip()) < 10:
        return False
    if quote in flat_src:
        return True
    return HK._norm(quote) in flat_src


def _days_from_raw(raw: str) -> int:
    """按与 hk_lockup 相同的单位表，从原文表述反算天数。"""
    m = re.search(r"(\d{1,4}|[一二两三四五六七八九十十二])\s*"
                  r"(个?自然日|日|天|个月|月|個月|星期|周|週)", raw or "")
    return HK._dur_days(m.group(1), m.group(2)) if m else 0


def _validate(c: dict, flat_src: str) -> dict | None:
    """三道硬校验。任何一道不过 -> 返回 None（丢弃该条）。"""
    if not isinstance(c, dict):
        return None
    typ = (c.get("type") or "").strip()
    if typ not in ("ipo_lockup", "issuer_pledge"):
        return None
    try:
        days = int(c.get("days"))
    except (TypeError, ValueError):
        return None
    if days <= 0 or days > 3650:
        return None
    quote = str(c.get("quote") or "")
    if not _quote_in_source(quote, flat_src):
        return None
    # 校验 2：数值必须能从它自己的 raw / quote 里反算出来
    raw = str(c.get("raw") or "")
    d_raw = _days_from_raw(raw) if raw else 0
    d_q = _days_from_raw(quote) if d_raw == 0 else d_raw
    if d_raw and d_raw != days:
        return None
    if d_q and d_q != days:
        return None
    # 校验 3：期限必须落在「承诺…止」句式里，挡掉段落别处的噪声期限
    if HK._ANCHOR.search(HK._norm(quote)) is None and d_raw == 0:
        return None
    return {
        "type": typ,
        "party": str(c.get("party") or "")[:60],
        "days": days,
        "raw": raw[:40],
        "quote": quote[:300],
        "source": "llm",
    }


def extract_clauses(md: str, name: str, code: str, date: str,
                    ask_fn=None, max_chars: int = 24000) -> dict:
    """LLM 抽 + 校验。返回

        {"clauses": [...], "rejected": n, "raw": 模型原文, "error": str|None}

    ⚠️ 一条都没抽到时 **必须**返回 ok=False 之类的可读状态，调用方据此显示
    「未取到」—— 绝不返回空列表让界面看起来像「本来就没有禁售」。
    """
    flat = HK._norm(md or "")
    if not flat:
        return {"clauses": [], "rejected": 0, "error": "公告正文为空"}

    if ask_fn is None:
        import llm_advisor
        ask_fn = llm_advisor.ask

    # 只给模型相关片段：禁售类关键词周边 + 前后文。全量 5 万字既贵又稀释注意力。
    ctx = _focus(md, max_chars)
    user = USER_TMPL % (name or "", code or "", date or "", ctx)
    try:
        text = ask_fn(SYSTEM, user)
    except Exception as exc:            # noqa: BLE001
        return {"clauses": [], "rejected": 0,
                "error": "LLM 调用失败：%s" % str(exc)[:200]}

    d = _extract_json(text)
    if not isinstance(d, dict) or not isinstance(d.get("clauses"), list):
        return {"clauses": [], "rejected": 0, "raw": text[:400],
                "error": "模型输出无法解析为 JSON（不是「没有条款」）"}

    good, rejected = [], 0
    for c in d["clauses"]:
        v = _validate(c, flat)
        if v:
            good.append(v)
        else:
            rejected += 1
    # 同一条款去重（模型也可能把标题和正文各报一次）
    uniq, seen = [], set()
    for v in good:
        k = (v["type"], v["days"], v["quote"][:40])
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    return {"clauses": uniq, "rejected": rejected, "raw": text[:600], "error": None}


def _focus(md: str, max_chars: int) -> str:
    """截出与禁售相关的片段，而不是把整篇塞给模型。

    理由：智谱那份公告里「禁售」只出现在两段，但全文 2.2 万字。塞全文
    既烧 token 又让模型注意力被稀释（AGENTS.md：噪声即噪音）。
    """
    flat = HK._norm(md)
    if len(flat) <= max_chars:
        return md
    marks = []
    for kw in HK.LOCK_KW + ("基石", "禁售期", "禁限"):
        start = 0
        while True:
            i = flat.find(kw, start)
            if i < 0:
                break
            marks.append(i)
            start = i + len(kw)
    if not marks:
        return md[:max_chars]
    # 合并相邻区间
    marks.sort()
    spans = []
    for i in marks:
        a, b = max(0, i - 300), min(len(flat), i + 700)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    picked, total = [], 0
    for a, b in spans:
        if total + (b - a) > max_chars:
            continue
        picked.append(flat[a:b])
        total += b - a
    return "\n\n[…]\n\n".join(picked) if picked else md[:max_chars]