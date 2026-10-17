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
from datetime import date, timedelta

import hk_lockup as HK

SYSTEM = """你是港股公告条款抽取器。只输出 JSON，不要任何解释。

任务：从给定的港股公告全文（Markdown，已由 MinerU 解析）中，抽取所有
「股份禁售 / 限售 / 锁定 / 禁止出售」类条款。

严格的硬性要求：

1. 只输出 JSON，格式：
   {"clauses": [{"type": "ipo_lockup"|"issuer_pledge", "party": "谁承诺的",
                 "days": 整数天数, "raw": "原文里的期限表述", "quote": "原文逐字片段",
                 "shares": 股数或 null, "pct": 百分比或 null,
                 "anchor_date": "YYYY-MM-DD 或 null"}]}

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

5. 原文明确写了被锁定的股数或比例，就填 shares / pct，否则填 null。
   anchor_date：条款的起算日（如上市日、交割日）在原文里出现过就填
   YYYY-MM-DD，否则填 null。**这三个字段填 null 不影响条款本身成立 ——
   请照常把条款报出来。**（2026-10-15 实测：这里若写成「对不上就作废」，
   同一批公告的召回会从 13 条掉到 2 条。）

6. 找不到就返回 {"clauses": []}。**绝对不要编造**，空数组是正确且被接受的答案。
   不要因为「公告里看起来应该有禁售条款」就硬凑一条。

7. 一份公告里同一段承诺只输出一次（标题和正文重复提到不算两条）。"""

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


# ---- 股数/比例的正则兜底（2026-10-15）----
# ⚠️ 为什么要有：实测把 shares/pct 交给 LLM 抽，结果**一条都没给**
#   （28 份公告、5 条条款，shares 全为 None）。而股数在港股公告里是
#   高度规整的数字串（「35,600,000股」「12.5%」），用正则更稳、更便宜，
#   也不会因为模型保守而整列空白。
# ⚠️ 只在 quote **附近**找，且**必须带「股」/「%」单位** ——
#   裸数字满篇都是（日期、金额、页码），无单位的数字一律不认。

_SH_RE = re.compile(r"([\d][\d,，\s]{2,}[\d])\s*股")
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*[%％]")


def guess_shares(ctx: str):
    """从条款上下文里找被锁定的股数。找不到返回 None（绝不猜）。"""
    if not ctx:
        return None
    flat = re.sub(r"\s+", "", ctx)
    m = _SH_RE.search(flat)
    if not m:
        return None
    return _pos_int(m.group(1))


def guess_pct(ctx: str):
    """从条款上下文里找**被锁定股份的占比**。

    ⚠️ 光有 `%` 号不够 —— 公告里到处都是百分比（佣金率、利率、
       其他交易的占比）。必须前面紧跟「佔/占」，才说明它是
       「占（已发行）股本 X%」这个语义。
    ⚠️ 找不到就 None：页面会拿它显示「占 X%」，宁可空着。
    """
    if not ctx:
        return None
    flat = re.sub(r"\s+", "", ctx)
    for m in _PCT_RE.finditer(flat):
        head = flat[max(0, m.start() - 20):m.start()]
        if "佔" in head or "占" in head:
            v = _pct(m.group(1))
            if v is not None:
                return v
    return None


def _ctx_window(quote: str, flat_src: str, span: int = 400) -> str:
    """取 quote 在原文里的**邻域**（前后各 span 字）。

    ⚠️ 为什么不能只用 quote + raw（2026-10-15 实测）：被锁定的**股数**
       通常写在**另一句**里 —— 期限句是
       「自大宗交易協議日期起直至配售事項完成後60日止」，
       而股数在紧邻的「不會出售 35,600,000 股股份」。
       两句相邻但不重叠，只在 quote 里找股数必然全空
       （这正是「28 份公告 0 条股数」的原因）。
    ⚠️ 邻域**不能大**：整篇里「XX股」遍地都是，窗口一大会把别的条款/
       别的交易的股数算到这一条上。±400 字覆盖「期限句 + 紧随的
       股数句」，又不至于跨到下一节。
    """
    try:
        key = HK._norm(quote or "")
        if len(key) < 10:
            return quote or ""
        i = flat_src.find(key)
        if i < 0:
            return quote or ""
        a = max(0, i - span)
        b = min(len(flat_src), i + len(key) + span)
        return flat_src[a:b]
    except Exception:                              # noqa: BLE001
        return quote or ""


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



# ---------------- 起算日 / 股数 / 比例（2026-10-15）----------------
# ⚠️ 每个字段都走**同一条纪律**：要么原文里能逐字核对上，要么返回 None。
#    抽不到就留空，绝不拿公告发布日 / 猜测值去填 ——
#    页面会拿它算日历日和乘股价，编一个数字比留空危害大得多。

_CN_NUM2 = {"一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十二": 12,
            "十八": 18, "二十四": 24}


def _pos_int(v):
    """股数：正整数才收。字符串「35,600,000股」也吃。"""
    if v is None:
        return None
    if isinstance(v, str):
        m = re.search(r"[\d][\d,\.\s]*", v)
        if not m:
            return None
        v = m.group(0)
    try:
        n = int(float(str(v).replace(",", "").replace("，", "").strip()))
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 10 ** 13 else None


def _pct(v):
    """比例：0 < x <= 100。"""
    try:
        f = float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    if not (0 < f <= 100) or f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, 4)


# ---- 中文数字日期（2026-10-15）----
# ⚠️ 为什么要有：港股公告的日期**大量用中文数字**（「二零二六年六月二十二日」），
#   而逐字核对原来只认阿拉伯格式 —— 实测 01888 两份公告上模型给出的
#   2026-06-22 / 2026-03-20 是对的，却被核对器整条扔掉，页面永远「缺起算日」。
#   两个方向都要：ISO -> 中文候选（核对用）；中文/阿拉伯 -> ISO（正则兜底用）。
_CN_DIGIT = "零一二三四五六七八九十"
_CN_VAL = {ch: i for i, ch in enumerate("零一二三四五六七八九")}
_CN_VAL.update({"〇": 0, "两": 2, "兩": 2})


def _cn_num(n: int) -> str:
    """1~99 的月份/日写法：一…十、十一…十九、二十、二十一…三十一。"""
    if n <= 10:
        return _CN_DIGIT[n]
    if n < 20:
        return "十" + _CN_DIGIT[n - 10]
    t, o = divmod(n, 10)
    return _CN_DIGIT[t] + "十" + (_CN_DIGIT[o] if o else "")


def _iso_to_cn(iso: str) -> list:
    """'2026-06-22' -> ['二零二六年六月二十二日', '二〇二六年六月二十二日']。

    年份里的「零」还有「〇」的写法（二〇二六），两个都生成。
    """
    try:
        y, mo, d = (int(x) for x in str(iso).split("-"))
    except (TypeError, ValueError, AttributeError):
        return []
    ys = "".join(_CN_DIGIT[int(ch)] for ch in str(y))
    out = ["%s年%s月%s日" % (ys, _cn_num(mo), _cn_num(d))]
    if "零" in ys:
        out.append("%s年%s月%s日" % (ys.replace("零", "〇"), _cn_num(mo), _cn_num(d)))
    return out


def _cn_val(s: str):
    """中文数字 -> int：「六」6、「十二」12、「二十」20、「二十二」22、「三十一」31。"""
    if not s:
        return None
    if "十" not in s:
        return _CN_VAL.get(s) if len(s) == 1 else None
    i = s.index("十")
    head, tail = s[:i], s[i + 1:]
    if head and head not in _CN_VAL:
        return None
    if tail and tail not in _CN_VAL:
        return None
    tens = _CN_VAL[head] if head else 1          # 「十」本身 = 10
    return tens * 10 + (_CN_VAL[tail] if tail else 0)


_CN_DATE_RE = re.compile(
    r"([零〇一二三四五六七八九]{2,4})年([一二三四五六七八九十]{1,3})月"
    r"([一二三四五六七八九十]{1,3})日")
_AR_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_S = (r"(?:[零〇一二三四五六七八九]{2,4}年[一二三四五六七八九十]{1,3}月"
           r"[一二三四五六七八九十]{1,3}日|\d{4}年\d{1,2}月\d{1,2}日)")


def _to_iso(s: str):
    """「2026年1月8日」/「二零二六年一月八日」-> '2026-01-08'；其余 None。"""
    m = _AR_DATE_RE.search(s or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)),
                        int(m.group(3))).isoformat()
        except ValueError:
            return None
    m = _CN_DATE_RE.search(s or "")
    if not m:
        return None
    try:
        y = int("".join(str(_CN_VAL[ch]) for ch in m.group(1)))
        mo, d = _cn_val(m.group(2)), _cn_val(m.group(3))
        if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
            return None
        return date(y, mo, d).isoformat()
    except (KeyError, ValueError, TypeError):
        return None


def _iso_forms(iso: str) -> list:
    """ISO 日期 -> 原文里可能出现的写法（阿拉伯 4 种 + 中文数字写法）。

    `_anchor_date` 与「交割日/完成日起算」提取共用同一组写法 ——
    核对口径只能有一套，两边各写一份迟早漂移（AGENTS.md 记过副本漂移）。
    """
    try:
        y, mo, d = (int(x) for x in str(iso).split("-"))
    except (TypeError, ValueError, AttributeError):
        return []
    # 原文里可能是「2026年7月16日」/「2026年07月16日」/「16 July 2026」
    return ["%d年%02d月%02d日" % (y, mo, d), "%d年%d月%d日" % (y, mo, d),
            "%d/%02d/%d" % (d, mo, y), "%d-%02d-%d" % (y, mo, d)] + _iso_to_cn(iso)


def _date_in_source(iso: str, flat_src: str) -> bool:
    """ISO 日期是否能在原文里**逐字**找到（任一写法即可）。"""
    return any(x in (flat_src or "") for x in _iso_forms(iso))


def _anchor_date(c, flat_src: str):
    """anchor_date 必须能在**原文里逐字找到**，否则 None。

    逐字核对是这个模块一贯的做法（见 `_quote_in_source`）：模型爱把
    「2026年7月16日」自己推成「2026-07-16」再当成原文，
    那样算出来的日历日会**看着精确、实际错**。
    """
    raw = str(c.get("anchor_date") or "").strip()
    if not raw or not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return None
    y, mo, d = (int(x) for x in raw.split("-"))
    if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    if not _date_in_source(raw, flat_src):
        return None
    return raw


# 「上市日期」锚点的正则兜底（2026-10-15）。
# ⚠️ 为什么必须有：招股章程里锁定期表格写「自本招股章程日期起至上市日期起計
#   12個月屆滿為止」，而**上市日期的值**在几十万字符之外（定义节 / 时间表 /
#   「H股开始买卖」节），`_focus` 窗口只截锁定期表格周边 —— 模型看不见日期
#   就填不出 anchor_date（硬填也会被逐字核对拒掉）。校验端手里是**全文**，
#   直接找；找到**唯一**日期才用 —— 多个不同值说明有歧义，宁可不要。
#
# ⚠️ 00100 招股章程实测踩出的两个坑（2026-10-15），改这条正则必须两个都防住：
#   ① 真实定义写的是「預計將為2026年1月9日或前後」——「預計」不是「預期」，
#      原写法漏掉它，返回 None；
#   ② 而它把「穩定價格期間自上市日期開始，並預期於2026年2月5日…屆滿」里的
#      **稳价期满日**当成了上市日 —— 返回错误日期比返回 None 更糟（看着精确、
#      实际错，正是本模块逐字核对要防的那类）。加负向前瞻 `(?!開始|屆滿)`：
#      「上市日期」后紧跟这两个词的是「自上市日期起算」句，不是日期定义。
#   实测修后：02513 分块一（22.3 万字符）仍唯一命中 2026-01-08；
#   00100 全文唯一命中 2026-01-09。
#
# ⚠️ 06658 招股章程实测又踩出一个（2026-10-15）：它写「預期H股**於**2026年6月15日
#   （星期一）在聯交所開始買賣」——没有「將」字，`將於` 写死就整条漏掉（全文 46 万
#   字符 0 命中）。把「將」改成可选 + 新增 `_L_TT2` 认时间表形式
#   （「預期H股開始在聯交所買賣.2026年6月15日(星期一)上午九時正」）。
#   全量 51 份公告实测：此改动只让 06658 从 {} 变 {2026-06-15}，其余 48 份
#   （含 00100/02513）逐份不变、零误伤。
_L_DEF = re.compile(r"上市日期(?!開始|屆滿)[^。；]{0,60}?"
                    r"(?:預期(?:將)?(?:為|於)|預計(?:將)?(?:為|於)|即為|即)\s*(%s)"
                    % _DATE_S)
_L_START = re.compile(r"預期\s*(?:H股|股份)\s*(?:將\s*)?於\s*(%s)[^。；]{0,20}?"
                      r"開始(?:於聯交所)?買賣" % _DATE_S)
_L_TT = re.compile(r"(?:H股|股份)\s*於\s*聯交所(?:主板)?\s*開始買賣[^<]{0,12}"
                   r"</td>\s*<td>\s*(%s)" % _DATE_S)
_L_TT2 = re.compile(r"預期\s*(?:H股|股份)\s*開始(?:在)?聯交所買賣[^<]{0,6}(%s)"
                    % _DATE_S)


def _listing_anchor(raw: str, quote: str, flat_src: str):
    """条款期限挂在「上市日期」上、但模型没给出起算日 -> 从全文找上市日期。

    gate 认三种写法：上市日期 / 上市後 / 上市時 —— 00100 招股章程实测
    「…保留至上市後滿六個月」「自…起至上市日期起計24個月當日止期間」都要走
    这条兜底。found 必须**唯一**，多个不同值一律 None。
    """
    txt = HK._norm((raw or "") + " " + (quote or ""))
    if not any(k in txt for k in ("上市日", "上市後", "上市時")):
        return None
    found = set()
    for rx in (_L_DEF, _L_START, _L_TT, _L_TT2):
        for m in rx.finditer(flat_src or ""):
            iso = _to_iso(m.group(1))
            if iso:
                found.add(iso)
    return found.pop() if len(found) == 1 else None


# ---- 「交割日/完成日/发行日起算」条款的起算日提取（2026-10-16）----
# ⚠️ 为什么需要（13 条此类条款全量实测，全部真实数据）：
#   这一类条款的期限挂在「交割日期 / 發行日期 / 完成日期 / 結算日期」上，
#   而那个日期的**值**写在文档别处（定义表 / 「應於DATE…（「交割日期」）
#   完成」这类句子）。`_focus` 窗口里只有条款本身、没有值，模型多数时候
#   填不出日期就退回最近的一个 —— 实测 13 条中 7 条起算日算错或缺失：
#   01888 拿公告日（06-17/03-18）、09696 拿公告日（02-03/02-04）、
#   00136 拿配售协议日（01-26）、01368 两条拿 None。
#   校验端手里是**全文**，且这些日期的写法高度规整，直接定位比让模型猜
#   可靠得多 —— 规则层定位到的日期**覆盖**模型值。
#
# 三个句式（都从上面这批真实条款里归纳，不是拍脑袋）：
#   P1 行内完成句：…應於DATE…(「交割日期」)完成      （02513 两份）
#   P2 结算句：    …X之結算預期將於DATE進行           （01888 两份）
#   P3 定义表行：  「〔Q〕交割日期」指DATE            （定义节）
# ⚠️ 定义表要**先剥掉 HTML 标签**：MinerU 把表格解析成
#   `「交割日期」</td><td>指</td><td>2026年2月11日`，不剥标签就永远匹配不上
#   （`」指`中间隔着 `</td><td>`）。
#
# 评分：期限表述（raw+quote）里出现完整词 Q+C（3 分）> 只出现前修饰 Q
# （2 分，如「配售」）> 只出现核心词（1 分，如「交割」）。
# **最高分组里日期唯一才用，否则 None** —— 宁缺勿错：
# 页面会显示「缺起算日」，而不是一个看着精确、实际错的日期。
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
_CLOSE_MARK = re.compile(r"交割日(?:期)?後|發行日(?:期)?後|完成日(?:期)?後"
                         r"|結算日(?:期)?後|完成後")
_CLOSE_Q = r"(?:可換股債券|配售|認購|債券)"
_CLOSE_C = r"(?:交割日(?:期)?|發行日(?:期)?|完成日(?:期)?|結算日(?:期)?)"
_CLOSE_FULL = r"(?:%s)?%s" % (_CLOSE_Q, _CLOSE_C)
# ⚠️ 完整词必须**连前修饰一起捕获**（2026-10-16 实测修正）：
#   00100-pl-0710 定义表三条是「債券交割日」07-16、「發行日期」07-16、
#   「配售交割日期」07-14，条款分别写「配售交割日期後60日」「債券交割日後60日」。
#   旧写法只捕获核心 `(交割日...)`、丢掉前修饰 → 两条条款都只看到「交割日」
#   → 07-14/07-16 同分撞车、整条弃权（实测 00100 第 1 条就是 None）。
_CLOSE_TERM_RE = re.compile(r"^(%s)?(%s)$" % (_CLOSE_Q, _CLOSE_C))
_CLOSE_P1 = re.compile(r"應於(%s)[^。；]{0,150}?[（(]「?(%s)」?[）)]"
                       r"[^。；]{0,12}?完成" % (_DATE_S, _CLOSE_FULL))
_CLOSE_P2 = re.compile(r"([^，。；「」]{0,12}?之?)結算預期將於(%s)進行" % _DATE_S)
_CLOSE_P3 = re.compile(r"「(%s)」指(%s)" % (_CLOSE_FULL, _DATE_S))
_TRIG_RE = re.compile(r"交割|發行|完成|結算|上市")


def _is_closing_clause(raw: str, quote: str) -> bool:
    """期限是否挂在「交割/完成/发行/结算日期」上（而不是上市日期）。

    只认这几种写法；上市日期系的条款（`上市日期後`/`上市日期起計`）**不**走
    本节 —— 它们有 `_listing_anchor` 那条独立链路，两条互不干扰。
    """
    txt = HK._norm((raw or "") + " " + (quote or ""))
    return _CLOSE_MARK.search(txt) is not None


def _term_score(term: str, txt: str) -> int:
    """期限词与条款正文的契合度：3=完整词、2=前修饰、1=核心词、0=不提。

    「完整词」容忍 日期/日 的口语变体（「債券交割日」vs「債券交割日期」）——
    判例：09696-0204b 的条款正文写「交割日後滿90日」，定义表写「交割日期」，
    不做变体就 3 分变 1 分、无法用它和别的候选拉开距离。
    """
    m = _CLOSE_TERM_RE.match(term or "")
    if not m:
        return 0
    q, c = m.group(1), m.group(2)
    variants = [q + c] if q else [c]
    if c.endswith("日期"):
        variants.append((q or "") + c[:-1])          # 交割日期 -> 交割日
    else:
        variants.append((q or "") + c + "期")        # 債券交割日 -> 債券交割日期
    if any(v in txt for v in variants):
        return 3
    if q and q in txt:
        return 2
    core = c[:-2] if c.endswith("日期") else c[:-1]
    return 1 if core and core in txt else 0


def _closing_pick(raw: str, quote: str, flat_src: str):
    """从全文定位「交割/完成/发行/结算日期」的**值**。定位不到返回 None。"""
    txt = HK._norm((raw or "") + " " + (quote or ""))
    ts = _TAG_RE.sub("", HK._norm(flat_src or ""))
    cands = []                                    # [(score, iso)]
    for m in _CLOSE_P1.finditer(ts):
        cands.append((_term_score(m.group(2), txt), _to_iso(m.group(1))))
    for m in _CLOSE_P2.finditer(ts):
        q = re.search(r"可換股債券|配售|認購|債券", m.group(1) or "")
        cands.append((_term_score((q.group(0) if q else "") + "結算日期", txt),
                      _to_iso(m.group(2))))
    for m in _CLOSE_P3.finditer(ts):
        cands.append((_term_score(m.group(1), txt), _to_iso(m.group(2))))
    # 逐字核对：候选提取自剥标签副本，日期必须能回到**原文**（防标签产物）。
    # 0 分的候选直接丢 —— 宁缺勿错，绝不让一个和条款正文毫无词汇交集的
    # 定义行日期来当起算日。
    cands = [(s, x) for s, x in cands
             if s >= 1 and x and _date_in_source(x, flat_src)]
    if not cands:
        return None
    best = max(s for s, _ in cands)
    values = {x for s, x in cands if s == best}
    return values.pop() if len(values) == 1 else None


def _near_trigger(base, flat_src: str, span: int = 80):
    """模型给的起算日必须**贴近**交割/发行/完成/结算/上市这类触发词。

    提取不到交割日时最后一道闸：一个日期若在全文离这些词都太远
    （>±span），基本是协议日期/公告日期被拿来充数 —— 丢弃（宁缺勿错）。
    提取成功时不走这里（提取结果本身就来自触发词句）。
    """
    if not base:
        return None
    ts = _TAG_RE.sub("", HK._norm(flat_src or ""))
    for form in _iso_forms(base):
        start = 0
        while True:
            i = ts.find(form, start)
            if i < 0:
                break
            if _TRIG_RE.search(ts[max(0, i - span):i + len(form) + span]):
                return base
            start = i + 1
    return None


def _add_months(d: date, n: int) -> date:
    """日历月加法：2026-01-08 + 12 個月 = 2027-01-08（而不是 +360 天 = 01-03）。

    月末截断：01-31 + 1 個月 = 02-28（闰年 02-29）。
    """
    m0 = d.month - 1 + n
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    last = 31 if m == 12 else (date(y, m + 1, 1) - date(y, m, 1)).days
    return date(y, m, min(d.day, last))


def _unlock_date(anchor: str, days: int, raw: str = ""):
    """起算日 + 期限 -> 解禁日。**算不出就返回 None**，不要近似。

    ⚠️ 期限按「N 個月」写时走**日历月**运算（招股章程锁定表格几乎全是
    「上市日期起計12個月屆滿」这种写法，+N×30 天会早好几天）。
    raw 缺失或不是月口径时保持原样的按天加法。
    """
    try:
        days = int(days)
        y, mo, d = (int(x) for x in str(anchor).split("-"))
        base = date(y, mo, d)
    except (TypeError, ValueError, AttributeError):
        return None
    if raw and days > 0 and days % 30 == 0 and re.search(r"個月|个月", str(raw)):
        n = days // 30
        if 1 <= n <= 120:
            return _add_months(base, n)
    return base + timedelta(days=days)



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
    # 🔴 没有股数就没有解禁金额 —— 下游页面靠这个区分
    #   「不涉及股份」与「金额未知」，绝不让缺字段渲染成 0 元。
    # ⚠️ 模型给的股数/比例**几乎总是 None**（实测 28 份公告 5 条条款里 0 条给了），
    #   所以用正则从**原文中 quote 的邻域**兜底，再退回模型值。
    ctx = _ctx_window(quote, flat_src) or (quote + " " + raw)
    shares = guess_shares(ctx) or _pos_int(c.get("shares"))
    pct = guess_pct(ctx) or _pct(c.get("pct"))
    # issuer_pledge 按定义锁的是「不许再发新股」= 0 股被锁，
    # 就算上下文里有别的股数也不该算成解禁金额。
    if typ == "issuer_pledge":
        shares, pct = None, None
    base = (_anchor_date(c, flat_src)
            or _listing_anchor(raw, quote, flat_src))
    # 交割/完成/发行/结算日起算的条款：先用全文定位**覆盖**模型值；
    # 定位不到时才退到模型值，且必须贴近触发词（否则宁缺勿错）。
    # 非此类条款**一行都不变**（上市日期系走 `_listing_anchor` 原链路）。
    if _is_closing_clause(raw, quote):
        base = _closing_pick(raw, quote, flat_src) or _near_trigger(base, flat_src)
    return {
        "anchor_event": str(c.get("anchor_event") or "")[:40],
        "anchor_date": base,
        "shares": shares,
        "pct": pct,
        "type": typ,
        "party": str(c.get("party") or "")[:60],
        "days": days,
        "raw": raw[:40],
        "quote": quote[:300],
        "source": "llm",
    }


def extract_clauses(md: str, name: str, code: str, date: str,
                    ask_fn=None, max_chars: int = 24000,
                    max_batches: int = 8) -> dict:
    """LLM 抽 + 校验。返回

        {"clauses": [...], "rejected": n, "raw": 模型原文, "error": str|None}

    ⚠️ 一条都没抽到时 **必须**返回 ok=False 之类的可读状态，调用方据此显示
    「未取到」—— 绝不返回空列表让界面看起来像「本来就没有禁售」。

    ⚠️ 公告超长时分批抽（2026-10-16）。旧实现只抽 `_focus` 装得下的那一批：
    MINIMAX 招股章程（68.9 万字符）23 段相关片段共 38836 字，`_focus` 只
    装得下 23760 —— 「自其各自禁售承諾日期起至上市日期滿六個月之日止」
    那段正好被扔掉，**整份文档 0 条款、缓存里也是空的**。实测那条只在
    **第 2 批**里出现。分批后每批独立调 LLM、批间合并去重。

    返回里可能多两个键（仅当确有缺口时）：
        "partial": True, "note": "⚠ …" —— 调用方**必须**把 note 显示出来
    且**不得写缓存**（否则「没查完」会被固化 30 天）。
    - 单批路径与旧实现**逐字节一致**（同一 prompt、同一返回结构）。
    """
    flat = HK._norm(md or "")
    if not flat:
        return {"clauses": [], "rejected": 0, "error": "公告正文为空"}

    if ask_fn is None:
        import llm_advisor
        ask_fn = llm_advisor.ask

    batches = _focus_batches(md, max_chars)
    n_all = len(batches)
    max_batches = max(1, int(max_batches or 0))
    if n_all > max_batches:
        batches = batches[:max_batches]

    good, rejected = [], 0
    n_fail_exc, n_fail_parse = 0, 0
    first_exc = None
    raw_out = None                     # 首个**可解析**批次的原文
    raw_fail = None                    # 首个**不可解析**批次的原文
    ok_batches = 0
    for ctx in batches:
        # 只给模型相关片段：禁售类关键词周边 + 前后文。全量 5 万字既贵又稀释注意力。
        user = USER_TMPL % (name or "", code or "", date or "", ctx)
        try:
            text = ask_fn(SYSTEM, user)
        except Exception as exc:        # noqa: BLE001
            n_fail_exc += 1
            if first_exc is None:
                first_exc = str(exc)[:200]
            continue
        d = _extract_json(text)
        if not isinstance(d, dict) or not isinstance(d.get("clauses"), list):
            n_fail_parse += 1
            if raw_fail is None:
                raw_fail = (text or "")[:400]
            continue
        ok_batches += 1
        if raw_out is None:
            raw_out = (text or "")[:600]
        for c in d["clauses"]:
            v = _validate(c, flat)
            if v:
                good.append(v)
            else:
                rejected += 1

    if ok_batches == 0:
        # 全部批次都没跑成：单批时的错误字符串与旧实现**逐字相同**
        parts = []
        if first_exc is not None:
            parts.append("LLM 调用失败：%s" % first_exc)
        if n_fail_parse:
            parts.append("模型输出无法解析为 JSON（不是「没有条款」）")
        err = "；".join(parts)
        if len(batches) > 1:
            err += "（全部 %d 批均未成功）" % len(batches)
        out = {"clauses": [], "rejected": 0, "error": err}
        if raw_fail is not None:
            out["raw"] = raw_fail
        return out

    # 同一条款去重（模型也可能把标题和正文各报一次；分批后还可能跨批重复）
    uniq, seen = [], set()
    for v in good:
        k = (v["type"], v["days"], v["quote"][:40])
        if k not in seen:
            seen.add(k)
            uniq.append(v)
    out = {"clauses": uniq, "rejected": rejected, "raw": raw_out, "error": None}
    notes = []
    if n_all > len(batches):           # 批数封顶：后面还有没查过的片段
        notes.append("⚠ 公告超长：相关片段共 %d 批，只抽取了前 %d 批"
                     "（后面可能还有条款未查）" % (n_all, len(batches)))
    if n_fail_exc + n_fail_parse:
        notes.append("⚠ 分批抽取：%d/%d 批失败（可能有条款未查到）"
                     % (n_fail_exc + n_fail_parse, len(batches)))
    if notes:
        out["partial"] = True
        out["note"] = "；".join(notes)
    return out


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


def _focus_batches(md: str, max_chars: int) -> list:
    """`_focus` 的分批版：相关片段**一段不漏**，每批装到 ≤ max_chars。

    为什么需要（2026-10-16 实测）：`_focus` 装不下时**跳过**该片段
    （`continue`），于是 MINIMAX 招股章程（68.9 万字符、23 段相关片段共
    38836 字）只装进前 23760 字，真正写着禁售承诺的那段（文档 46.1 万
    字符处）被丢掉 → 整份文档 0 条款。

    规则（窗口/合并逻辑与 `_focus` 完全一致，只是不丢弃片段）：
      - 短文档 / 无关键词 / 单批装得下 → 输出与 `_focus` **逐字节相同**；
      - 单个 span 超过 max_chars 就切开（重叠 200 字：条款跨切口不至于断掉）；
      - max_chars 下限 300 —— 切分步进是 max_chars-200，小于该值会原地打转。
    """
    max_chars = max(300, int(max_chars or 0))
    flat = HK._norm(md)
    if len(flat) <= max_chars:
        return [md]
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
        return [md[:max_chars]]
    # 合并相邻区间（与 `_focus` 同一逻辑）
    marks.sort()
    spans = []
    for i in marks:
        a, b = max(0, i - 300), min(len(flat), i + 700)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    # 单个 span 超长 -> 切开，留 200 字重叠
    pieces = []
    for a, b in spans:
        if b - a <= max_chars:
            pieces.append((a, b))
            continue
        pos = a
        while pos < b:
            end = min(b, pos + max_chars)
            pieces.append((pos, end))
            if end >= b:
                break
            pos = end - 200
    # 按文档序装箱；装不下就开新批（**不跳过任何片段**）
    batches, cur, total = [], [], 0
    for a, b in pieces:
        n = b - a
        if cur and total + n > max_chars:
            batches.append(cur)
            cur, total = [], 0
        cur.append((a, b))
        total += n
    batches.append(cur)
    return ["\n\n[…]\n\n".join(flat[a:b] for a, b in batch)
            for batch in batches]