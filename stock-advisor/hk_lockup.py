# -*- coding: utf-8 -*-
"""从 MinerU markdown 里抽「禁售/锁定」条款。

样本是真数据：智谱 02513 2026-09-13 配售+可转债公告（MinerU full.md）。
措辞以**该文件实际产出**为准，不按想象写正则。

智谱实测两条（2026-10-12，PyMuPDF 独立复核过第二处本来是完整的）：
  1) 「配售協議項下的本公司禁售承諾…自配售協議日期起至交割日期後60日止期間」
  2) 「認購協議項下的本公司禁售承諾…自認購協議日期起至發行日期後60日（包括首尾兩日）止期間」

⚠️ 我之前凭新闻印象说「90 日」是**错的** —— PDF 里两处都是 60 日。
   当时 PyMuPDF 把「後6」吞了，我拿残缺文本 + 新闻记忆补了个 90 上去。
   这条教训写进 AGENTS.md 了。
"""
from __future__ import annotations

import re

# 期限写法：60日 / 60 日 / 六十日 / 6 months / 6-month
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十二": 12}

# 「禁售」类关键词。**故意不用词表判语义**（AGENTS.md 记过「这不是新闻」
# 那次教训）—— 这里只需要找到**承诺的段落**，不需要判断「是不是真的解禁」。
LOCK_KW = ("禁售", "禁限", "禁售期", "鎖定", "锁定", "lock-up", "Lock-up",
           "lock up", "Lock Up", "限制出售", "不得出售", "承諾不出售", "承诺不出售")

# 期限表达：X日 / X日（包括首尾两日）/ X个月 / X个自然日
_DUR = re.compile(
    r"(\d{1,4}|[一二两三四五六七八九十十二])\s*"
    r"(个?自然日|日|天|个月|月|個月|星期|周|週)\s*"
    r"(?:（[^）]{0,12}）|\([^)]{0,12}\))?"
    r"(?:之?止期間|之?止期间|止期間|止期间)?"
)

_UNIT_DAYS = {"自然日": 1, "日": 1, "天": 1, "个月": 30, "月": 30, "個月": 30,
              "星期": 7, "周": 7, "週": 7}

# 承诺期限的**句式锚点**。期限数字只认出现在「起至…止期間」这个结构里的，
# 绝不取「段落里最长的那个期限」。
#
# ⚠️ 这里踩过一个极阴的 bug（智谱 2026-09-13 实测）：
#   真值是「自配售協議日期起至交割日期後**60日**止期間」，但同一段 420 字
#   窗口后面还有别的「6個月」。原实现取 max(days) —— 于是抽出 **180 天**，
#   一个**看着合理、实际完全错误**的数字。条款期限必须贴着句式锚点取。
_ANCHOR = re.compile(
    r"(?:起至|至|自).{0,24}?"
    r"(\d{1,4}|[一二两三四五六七八九十十二])\s*"
    r"(个?自然日|日|天|个月|月|個月|星期|周|週)\s*"
    r"(?:（[^）]{0,16}）|\([^)]{0,16}\))?"
    r"(?:之?止期間|之?止期间|止期間|止期间|止)"
)


def _dur_days(num: str, unit: str) -> int:
    n = _CN_NUM.get(num, None)
    if n is None:
        try:
            n = int(num)
        except ValueError:
            return 0
    return n * _UNIT_DAYS.get(unit, 0)


def _norm(s: str) -> str:
    """MinerU 的 markdown 里标题是 `##xxx`，句子间有换行/全角空格。
    归一化后才能跨行匹配（「發\r\n行日期後60日」这种）。"""
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", "", s)


def _type_of(kind: str) -> str:
    """区分两类，**语义完全不同，混在一起就是答非所问**。

    - `issuer_pledge` 发行人禁售承诺：「本公司不再配售/发行新股」。
      ⚠️ 这**不是**股份解禁 —— 配售的新股发行日即可流通。
    - `ipo_lockup` IPO 锁定：基石 6 个月 / 控股股东 6~12 个月 —— 这才是解禁。
    """
    k = _norm(kind)
    if ("配售協議" in k or "認購協議" in k or "认购协议" in k
            or "發行協議" in k or "发行协议" in k):
        return "issuer_pledge"
    return "ipo_lockup"


def find_clauses(md: str, max_hits: int = 12) -> list[dict]:
    """抽出所有禁售/锁定条款。返回结构化 dict 列表。

    抽不到就返回空列表 —— 调用方必须**如实说「未取到」**，
    不许拿别处的数字来填（AGENTS.md：展示层不该有能力弄丢数据）。
    """
    flat = _norm(md)
    hits: list[dict] = []
    seen: set = set()
    seen_dup: set = set()

    for kw in LOCK_KW:
        start = 0
        while True:
            i = flat.find(kw, start)
            if i < 0:
                break
            start = i + len(kw)
            # 取该关键词后的一段（承诺条款通常在标题后 400 字内）
            seg = flat[i:i + 420]
            # 标题 = 从关键词往前吃到最近的段落标记
            head = flat[max(0, i - 40):i]
            m = re.search(r"([^#]{0,40}$)", head)
            title = (m.group(1) if m else "") + kw

            # ⚠️ 期限**只从句式锚点里取**（「…起至…止期間」）。
            #    取段落里最长的期限会被后面的无关「6個月」带偏 ->
            #    抽出 180 天这种看着合理实际全错的数字（智谱实测）。
            am = _ANCHOR.search(seg)
            if not am:
                continue
            days = _dur_days(am.group(1), am.group(2))
            if not days:
                continue
            durs = []
            for dm in _DUR.finditer(seg):
                d = _dur_days(dm.group(1), dm.group(2))
                if d:
                    durs.append({"days": d, "raw": dm.group(0)})
            # 去重键用**条款所在位置**而不是标题：同一份公告里「禁售」字样
            # 常在标题和正文各出现一次，标题切片完全一样但其实是同一条款，
            # 不去重就会返回 3 条一模一样的记录（测试实测）。
            key = (i, days)
            if key in seen:
                continue
            seen.add(key)
            seen_txt = (seg[:90], days)
            if seen_txt in seen_dup:
                continue
            seen_dup.add(seen_txt)
            hits.append({
                "type": _type_of(title),
                "title": title[-40:],
                "days": days,
                "duration_raw": am.group(0),
                "all_durations": [d["raw"] for d in durs][:6],
                "excerpt": flat[max(0, i - 30):i + 200],
            })
            if len(hits) >= max_hits:
                return hits
    return hits