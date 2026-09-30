# -*- coding: utf-8 -*-
"""用本地 LLM 把策略文章抽成结构化字段，落 sa_strategy_digest。

字段是按「我拿到之后要干什么用」设计的，不是按「文章里有什么」设计的
------------------------------------------------------------
这篇文章我最后要做的三件事：
  1. 知道它**怎么实现的**（能不能照着写出来） -> steps / universe / params
  2. 知道它**跑出来什么效果**（作者说的可不可信） -> perf_claimed + perf_verified
  3. 知道它**什么情况下能用**（搬进我的模拟盘吗） -> applicable / unsuitable
                                    + portable_score（我们自己打的分）
所以 `perf_claimed` 和 `portable_score` 必须是**两个分开的东西**：
前者是作者自述（可能吹），后者是我们的判断。混在一起就分不清了。

为什么不直接爬文章里的指标和验证图
--------------------------------
实测一页 21 条帖有 7 条标题吹「年化 100%~710%」，还有「5 年回测年化 710%
（年化 60%）」这种自相矛盾的。所以**作者自述的数字一律标记为 claimed，
绝不当作事实**。真实效果要靠本地回测生成（backtest.py），
回测结果验证后把 perf_verified 改成 verified。

为什么要专门处理「脱敏」和「说明不足」
------------------------------------
社区里不少帖子作者把核心函数体用 `...` 省略（实测《实测复现年化526%的
低吸连阳首板策略》整篇如此）。如果直接喂给 LLM 问「实现步骤是什么」，
它会照着 `def get_buy(context): ...` 这个空壳**脑补**出一套听起来很合理的
选股逻辑 —— 那是我们自己编的，不是作者的策略。
所以：
  - 源码被脱敏 -> 明确告诉模型「这段代码的函数体是空的，不要推测」，
    并且 steps 里只能填能从变量名/注释/正文确证的部分。
  - 模型自己判断「说明不足，无法确定实现」-> needs_research=true，
    交给递归搜索去补（见 research.py），而不是硬编一个。

喂给模型的材料（按优先级）
----------------------
1. 正文（去掉图片和裸 URL，**保留代码块内容** —— 选股逻辑经常写在注释里）
2. 抽出来的源码（拼接后的完整版）
3. **评论区**：优先「含代码的」和「作者自己回的」，最多 N 条
   —— 这是你特别要求的，实测评论区经常是唯一的源码出处
4. 文章元信息（标题/作者/发布时间/回复数/克隆数）

不 import app（避免循环依赖）：连接由 app.py 通过 deps 传进来。
"""
import json
import re
import time
from datetime import datetime

import llm_advisor
import strategy_sections as SS

# ---------------- 提示词 ----------------

SYSTEM = """\
你在帮一个**个人自用的** A 股量化系统做「策略逆向工程」。读者只有你一个，
他要判断的是：「这篇社区文章里的策略，我能不能搬进自己的模拟盘跑一遍？」

你必须**严格区分「文章里写了什么」和「你自己推测的」**。这个区分是本任务
最重要���要求，原因很具体：
- 我实测过社区里一页 21 条帖子有 7 条标题吹年化 100%~710%，还有「5年回测
  年化710%（年化60%）」这种自相矛盾的。作者自述的数字**一律当作未经验证
  的声明**。
- 更糟的是，有些帖子作者把核心逻辑用 `...` 省略了（给函数签名，函数体空的）。
  如果你照着这种空壳推测「实现步骤」，我会拿到一套**你编的**策略，
  而我以为那是作者的。这比抽不出来糟糕得多。

所以规则：
1. 只写材料里能确证的内容。每个步骤尽量带上依据（哪句原话 / 哪个变量名）。
2. 材料没写的地方，**不要填你的先验**。宁可少写两条步骤。
3. 材料明确不足时，在 UNCERTAINTY 里说清缺什么，并把 RESEARCH 置 true。
4. 源码被省略（我会告诉你「函数体被作者用 ... 省略」）时，**绝对不要推测
   函数体里应该有什么**。可以记录「有 initialize 但没给选股逻辑」这类事实。

输出格式：**严格按下面的分段标记输出，不要 JSON，不要代码围栏，
不要任何开场白或解释文字。**

###TITLE
给这套策略起个中文短名，20 字以内
###SUMMARY
2~4 句，**总共不超过 100 字**，讲清它干什么、买卖什么、依据什么信号
###TYPE
从这些里选一个：多因子选股 / 择时择股 / 板块轮动 / 打板 / 网格 /
小市值 / 低估值 / 趋势跟踪 / 其他
###STEP
一步一行，格式：序号 | 做什么 | 怎么做（具体到指标/阈值/周期） | 依据（原文原句或代码变量名）
**每格不超过 30 字**。最多 12 步。
###UNIVERSE
选股范围/股票池怎么定的，一行一条，每条不超过 40 字。最多 5 条。
###PARAM
参数名: 取值，一行一个（如「调仓周期: 每周五」「持股数: 10」「止盈: 0.2」）。
**最多 15 个最关键的参数**，不要把源码里每个 g.xxx 都抄一遍。
###PERF
作者自述的效果，一行一个（如「年化: 14.1%」「最大回撤: -20%」「回测区间: 2019-2025」
「作者自认的问题: 无」；作者没写的就别写这一行）。**最多 6 行。**
###APPLICABLE
什么市场/市况/资金体量下适用，一行一条，每条不超过 40 字。最多 5 条。
###UNSUITABLE
明确不适用或有踩坑提示的场景，一行一条。最多 5 条。
###RISK
风险与坑，一行一条。最多 6 条。
###DEPS
依赖的数据/环境/库（如 Wind、财务数据、某 API），一行一条。最多 5 条。
###UNCERTAINTY
材料里哪些关键信息缺失或自相矛盾，**不超过 80 字**；没有就写「无」
###RESEARCH
写 true 或 false。若 true，用「true | 要额外去搜什么」的格式
###SCORE
格式：0~5 的整数 | 为什么是这个分（**理由不超过 60 字**）

关于 SCORE 那个 0~5（这是**我们自己的判断**，不是文章自述）：
  5 = 有完整可运行源码 + 只依赖日线/财务数据 + 逻辑自洽，可直接搬进模拟盘
  4 = 源码完整但用了平台专有 API，改写量小
  3 = 思路清楚、参数明确，但源码缺失或残缺，要照着描述重写
  2 = 只有思路轮廓，参数/阈值都没给，重写等于自己设计
  1 = 讲的是理念和战绩，没有可实现的细节
  0 = 根本无法判断（比如通篇公告/求助）

**非常重要：务必把每一段都写完再停。** 写得越紧凑越好，
不要复述原文、不要写开场白、不要写「综上所述」之类的话。
"""

USER_TMPL = """\
请从下面这篇社区策略文章里抽取结构化信息。

<article>
标题：{title}
作者：{author}
发布时间：{published}
回复数：{reply_count}　点赞：{like_count}　被克隆（有人拿去跑过）：{clone_count}

【正文】
{content}

【抽出来的源码】
{source}

【评论区（{n_replies} 条节选）】
{replies}
</article>

{src_note}

按上面说的分段标记格式输出。"""

def _src_note(src: dict | None) -> str:
    """告诉模型源码的「可信度状态」，避免它对空壳脑补。"""
    if not src:
        return ("【重要】这篇文章没有抽出任何代码块。如果你从正文描述里能"
                "推断出实现步骤，请务必在 uncertainty 里说明「无源码，"
                "步骤系根据文字描述推断」，并把 needs_research 置 true。")
    if src.get("redacted"):
        why = "；".join(src.get("stub_reasons") or [])[:300]
        return ("【重要】作者把核心逻辑用 `...` 省略了（脱敏，防抄袭）。"
                "已检测到 %d 处：%s\n"
                "**绝对不要推测这些省略处的函数体应该写什么。**"
                "只写你能从现有代码、变量名、注释、正文确证的部分；"
                "把它们记进 steps 时 evidence 填「作者省略」，"
                "并把 needs_research 置 true。" % (src.get("stub_sites", 0), why))
    return ("【源码状态】%d 个代码块共 %d 行，未检测到省略，看起来是完整的。"
            "注意：完整 ≠ 正确，作者自述的效果仍未经我们回测验证。"
            % (src.get("n_blocks", 1), src.get("raw_lines", 0)))


def _ts(v, n: int = 10) -> str:
    """把时间列转成短字符串。

    **别直接对 psycopg2 取出来的时间列做切片** —— 那是 datetime 对象，
    不是 str，`[:10]` 直接抛 TypeError。我在这上面栽了一次：
    「抽取」端点 1 秒就返回 ok:false，错误是
    `'datetime.datetime' object is not subscriptable`，
    排查方向还容易跑偏到 LLM 配置上去。
    """
    if not v:
        return ""
    if isinstance(v, (datetime,)):
        return v.strftime("%Y-%m-%d" if n <= 10 else "%Y-%m-%d %H:%M")
    return str(v)[:n]


def build_prompt(art: dict, src: dict | None, replies: list[dict]) -> tuple[str, str]:
    """拼出 (system, user)。replies 已按优先级排好序。"""
    parts = []
    for r in replies:
        tag = []
        if r.get("has_code"):
            tag.append("含代码")
        if r.get("is_author"):
            tag.append("作者本人")
        if r.get("backtest_name"):
            tag.append("关联回测「%s」" % r["backtest_name"][:30])
        head = "— %s%s：%s" % (r.get("author") or "匿名",
                              ("（%s）" % "/".join(tag)) if tag else "",
                              _ts(r.get("add_time")))
        parts.append(head + "\n" + (r.get("content") or "")[:REPLY_CHARS])
    user = USER_TMPL.format(
        title=art.get("title") or "", author=art.get("author") or "",
        published=_ts(art.get("published_at")),
        reply_count=art.get("reply_count"), like_count=art.get("like_count"),
        clone_count=art.get("clone_count"),
        content=(art.get("content_text") or "")[:14000],
        source=(src.get("code") if src else "（无）")[:9000] or "（无）",
        n_replies=len(replies), replies="\n\n".join(parts) or "（没有抓到评论）",
        src_note=_src_note(src))
    return SYSTEM, user


# ---------------- 调 LLM ----------------

def _starts_literal(text: str, i: int) -> int:
    """位置 i 处若是 true/false/null 的开头，返回它的长度，否则返回 0。

    大小写都认。**必须整词消费**：第一版只判断「首字母像不像字面量」，
    结果 `True` 的 `T` 放行、紧接着的 `rue` 被当成垃圾字符逐个丢掉 ——
    修完变成 `{"a": T}`，比原来还坏。
    """
    for k in ("false", "true", "null", "none", "False", "True", "None"):
        if text[i:i + len(k)].lower() == k.lower():
            return len(k)
    return 0


def repair_json(text: str) -> str:
    """把模型输出的「接近 JSON」修成真 JSON。

    为什么需要
    ----------
    本地跑的是 120B 模型（nemotron-3-super-120b），它**经常把 JSON 的字符串
    值写成 Python 的单引号**。实测一次真实输出：
        "evidence": '尾盘卖出（get_close_sell）两者完全一致'；'盈利>20%…'
    单引号在 JSON 里非法，json.loads 直接报
    `Expecting value: line 34 column 19`。

    这类错误会**反复出现**，所以不能靠「重试一次」解决 ——
    每次重试都是真金白银，而且大概率还是错（同一个模型、同一个毛病）。
    这里写一个容错扫描器，一次修好下面这几类问题：
      1. 字符串用了单引号（Python 习惯）
      2. 字符串里有裸换行 / 制表符
      3. 对象或数组末尾多了逗号（trailing comma）
      4. Python 的 True/False/None

    做法是**逐字符扫描**并记录状态（当前在不在字符串里、引号是哪种），
    只在字符串外做 True/None 替换和逗号清理 —— 靠正则全局替换会
    把字符串内容里的 "None" 也换掉，那是更隐蔽的数据损坏。
    """
    out: list[str] = []
    i, n = 0, len(text)
    dropped = 0
    # 当前字符串的引号类型与内容缓冲
    quote = ""            # '"' / "'" / '' 表示不在字符串里
    buf: list[str] = []
    # JSON 里能跟在字符串后面的合法字符
    after_ok = set(",}])")
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == quote:
                content = "".join(buf)
                j = i + 1
                # ---- 合并「字符串片段」----
                # 实测本地模型会把一句话拆成几个引号片段，中间还漏了
                # 裸文本：'前半'；'后半'  或  '…-10%''
                # 在 JSON 里这是彻底非法（字符串外出现裸字符、两个字符串
                # 之间少逗号）。这里把后续的裸文本和引号片段统统并进
                # 当前字符串 —— 直接丢掉会让 evidence 这类关键依据少一截。
                while True:
                    while j < n and text[j] in " \t\r\n":
                        j += 1
                    if j >= n:
                        break
                    cj = text[j]
                    if cj not in after_ok and cj not in ':{[' and cj not in "\"'":
                        k = j
                        while (k < n and text[k] not in "\"'"
                               and text[k] not in after_ok
                               and text[k] not in ':{['):
                            k += 1
                        seg = text[j:k].strip()
                        if seg:
                            content += " " + seg
                        j = k
                        continue
                    if cj in "\"'" and cj != quote:
                        # 开引号和闭引号**类型不一致**：模型用 ' 开、用 " 收。
                        # 实测原句是这样收尾的：
                        #   "evidence": '前半'；'中段'；'尾段'。"
                        # 把 " 当成新字符串的开头的话，它会一路吞到下一行
                        # 的 "no" 上，把中间的结构全吃掉。
                        j += 1
                        break
                    if cj in "\"'":
                        # '' 空引号对：模型句尾多打的那个引号，跳过
                        if j + 1 < n and text[j + 1] == cj:
                            j += 2
                            continue
                        e = text.find(cj, j + 1)
                        if e < 0 or "\n" in text[j:e]:
                            # 找不到同行的同型引号 —— 说明**这个引号本身
                            # 就是终止符**（模型最后一笔写成
                            #   '…-10%'。"
                            # 早一版在这里直接 break 而不消费它，于是结尾
                            # 的 `。` 掉到字符串外成了裸字符，JSON 报
                            # "Expecting ',' delimiter"，而报错行落在
                            # 完全无辜的 evidence 上，排查时被带偏了三轮。
                            j += 1
                            break
                        seg = text[j + 1:e].strip()
                        if seg:
                            content += " " + seg
                        j = e + 1
                        continue
                    break
                out.append('"' + content.replace('"', '\\"') + '"')
                quote, buf = "", []
                i = j
                continue
            if ch == "\n":
                # 双引号串里的裸换行要转义；单引号串直接在这里闭合 ——
                # 单引号是「模型不守 JSON 规矩」的产物，它的片段几乎从不
                # 跨行，让它继续跑会把后面的 `},` 结构全吞进字符串里，
                # 报错位置还会飘到几十行之外（我在这上面绕过一次）。
                if quote == "'":
                    out.append('"' + "".join(buf).replace('"', '\\"') + '"')
                    quote, buf = "", []
                    i += 1
                    continue
                buf.append("\\n")
                i += 1
                continue
            if quote == "'" and ch == '"':
                # **开引号 ' 、闭引号 "**（类型不一致）。实测原句：
                #   "evidence": 'update_dynamic_stop_loss … -10%'。"
                # 这个判断必须放在**主扫描循环**里，不能只放在下面的
                # 「合并片段」循环里 —— 否则字符串根本不会走到闭合那一步，
                # 会一路开到文件末尾，把剩下的结构全吞掉，报错位置飘到
                # 完全无关的行上（我在这上面查了三轮才定位到）。
                out.append('"' + "".join(buf).replace('"', '\\"') + '"')
                quote, buf = "", []
                i += 1
                continue
            if ch == "\r":
                i += 1
                continue
            if ch == "\t":
                buf.append("\\t")
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        # 字符串外
        if ch in "\"'":
            # 空引号对 ''：实测本地模型会在句尾多打一个引号
            # （'...-10%''）。不特判的话它会开一个字符串一路吞到
            # 下一行的引号，把整个结构吃掉。
            if ch == "'" and i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            # 孤立的多余单引号：后面紧跟 } ] ,  说明它只是个错打的引号，
            # 不是字符串的开头。开着它会把后面的结构全吞进字符串。
            if ch == "'":
                k = i + 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k >= n or text[k] in "}],":
                    i += 1
                    continue
            quote = ch
            buf = []
            i += 1
            continue
        if text.startswith("//", i):          # JS 风格注释
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        # ---- JSON 字面量：整词消费，别只放行首字母 ----
        lit = _starts_literal(text, i)
        if lit:
            out.append(text[i:i + lit])
            i += lit
            continue
        # ---- 字符串外的垃圾字符，直接丢 ----
        # 模型漏引号时会在结构之间留下裸标点（比如 `}` 后面跟一个 `。`）。
        # 这些字符在 JSON 里没有位置，留着必然报 "Expecting ',' delimiter"。
        if ch not in ",{}[]:\"/-0123456789" and not ch.isspace():
            if dropped < 8:
                print("[digest] 修复时丢弃了字符串外的杂字符 %r（位置 %d）"
                      % (ch, i))
            dropped += 1
            i += 1
            continue
        out.append(ch)
        i += 1
    if quote:                                  # 字符串没闭合
        tail = "".join(buf)
        # 失控的残串要丢。两种实测到的形态：
        #  ① 空的（或只有标点的）—— 模型用 ' 开、用 " 收，合并循环已经
        #     正常收尾了，末尾那个 " 又开出一个空串一路开到文件末尾；
        #  ② 里面裹着结构字符 —— 那个 " 其实是上一轮的收尾引号，它开出的
        #     字符串把后面的 `]` / `}` 吞了，补成字符串只会让 JSON 更烂。
        if tail.strip(" \t\r\n,;:.。，") and not set(tail) & set("[]{}"):
            out.append('"' + tail.replace('"', '\\"'))
    s = "".join(out)

    # Python 字面量 -> JSON（只在字符串外）
    s = re.sub(r'(?<![\w"])(True|False|None)(?![\w"])', lambda m: {
        "True": "true", "False": "false", "None": "null"}[m.group(1)], s)
    # 去掉对象/数组末尾的多余逗号
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _extract_json(text: str) -> dict:
    """从模型输出里挖 JSON，失败时先修再试。

    顺序很重要：**先剥代码围栏再取最外层花括号**，反过来会在围栏里的
    示例 JSON 上截断。
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
        t = t.strip()

    def _try(s: str) -> dict:
        try:
            v = json.loads(s)
            if isinstance(v, dict):
                return v
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]          # 模型偶尔包了一层数组
        except (TypeError, ValueError):
            pass
        repaired = repair_json(s)
        if repaired != s:
            v = json.loads(repaired)     # 还不行就让它抛出，报修完后的真实位置
            if isinstance(v, dict):
                return v
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v[0]
        return {}

    d = _try(t)
    if d:
        return d
    # 退而求其次：取最外层 {...}
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        d = _try(t[i:j + 1])
        if d:
            return d
    try:
        json.loads(repair_json(t))
    except (TypeError, ValueError) as exc:
        raise ValueError("模型输出修完还不是合法 JSON：%s ｜ 原文头 300 字：%s"
                         % (exc, t[:300])) from exc
    raise ValueError("模型输出里没有 JSON 对象。原文头 300 字：%s" % t[:300])


def call_llm(system: str, user: str, conf: dict, max_tokens: int = 16000
             ) -> tuple[dict, dict]:
    """调 Claude 抽取。返回 (结果 dict, 成本/元信息 dict)。失败抛 RuntimeError。

    max_tokens 默认给到 16000：实测这个本地模型**极其啰嗦**，用 4096 时
    `stop_reason=max_tokens`，写到 SCORE 之前就被截断了，整个抽取白跑
    （一次 99 秒 + 8000 token 换了个残缺结果）。同时提示词里加了逐段字数
    上限，两头一起收。

    **被截断不算失败**：分段格式是逐段独立的，前面写完的段照样解析得出来。
    所以截断时照样返回结果，只在 cost 里标 truncated，前端可以提示
    「这段可能不完整」。
    """
    if not conf.get("api_key"):
        raise RuntimeError("没配 Anthropic API key（config.yaml llm.api_key）")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic SDK 没装：pip install 'anthropic>=1.5'") from exc
    kw = {"api_key": conf["api_key"], "timeout": 180.0, "max_retries": 2}
    if conf.get("base_url"):
        kw["base_url"] = conf["base_url"]
    client = Anthropic(**kw)
    msgs = [{"role": "user", "content": user}]
    errs = []
    msg = None
    # 跟 llm_advisor 一样的降级思路：带 thinking/fallbacks 的完整特性只有
    # Anthropic 官方端点支持，代理网关会挂到超时 —— base_url 非空就直接走最简。
    attempts = []
    if not conf.get("base_url"):
        attempts.append(("full", dict(thinking={"type": "adaptive"},
                                      betas=["server-side-fallback-2026-07-01"],
                                      fallbacks="default")))
    attempts.append(("basic", None))
    for label, extra in attempts:
        try:
            if label == "full":
                msg = client.beta.messages.create(
                    model=conf["model"], max_tokens=max_tokens,
                    system=system, messages=msgs, **extra)
            else:
                msg = client.messages.create(
                    model=conf["model"], max_tokens=max_tokens,
                    system=system, messages=msgs)
            break
        except Exception as exc:            # noqa: BLE001
            errs.append("%s: %s: %s" % (label, type(exc).__name__, str(exc)[:120]))
    if msg is None:
        raise RuntimeError("Claude 调用失败：" + " | ".join(errs))
    if getattr(msg, "stop_reason", "") == "refusal":
        raise RuntimeError("被安全策略拒绝（stop_reason=refusal）")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude 返回空内容（stop_reason=%s）"
                           % getattr(msg, "stop_reason", "?"))
    usage = msg.usage
    cost = {
        "model": conf["model"],
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "stop_reason": getattr(msg, "stop_reason", ""),
        "raw_len": len(text),
        "truncated": getattr(msg, "stop_reason", "") == "max_tokens",
    }
    # 先按分段格式解析（主路径，几乎不会失败）；模型自作聪明输出了 JSON
    # 就走 repair_json 兜底。两条路都留着。
    res = SS.parse_sections(text)
    if not res.get("title_zh") and not res.get("steps"):
        res = _extract_json(text)
        res["_format"] = "json"
    else:
        res["_format"] = "sections"
    if cost["truncated"]:
        # 截断了就明说，别让「字段少」看起来像「文章里就没有」
        res["uncertainty"] = ((res.get("uncertainty") or "")
                              + "（本次抽取因输出超长被截断，后面的段可能缺失）"
                              ).strip()[:2000]
    return res, cost


# ---------------- 取材料 ----------------

REPLY_BUDGET = 26           # 最多喂多少条评论
REPLY_CHARS = 1400          # 每条评论最多多少字


def load_replies(cur, article_id: str) -> list[dict]:
    """取评论，**按信息价值排序**而不是时间顺序。

    排序依据（实测评论区里贴代码的多半在有实质回复的那些楼里）：
      作者本人的 > 含代码的 > 长文本 > 其他
    因为有的帖子 7000 多条回复，绝大多数是「谢谢」「学习了」，
    按时���倒序喂进去等于用预算喂垃圾。
    """
    cur.execute(
        """SELECT author, content, content_len, has_code, n_code_blocks,
                  is_author, backtest_name, add_time
           FROM sa_strategy_reply WHERE article_id=%s
           ORDER BY (CASE WHEN is_author THEN 0 WHEN has_code THEN 1
                          WHEN content_len > 120 THEN 2 ELSE 3 END),
                    content_len DESC LIMIT %s""",
        (article_id, REPLY_BUDGET))
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        # 掐掉纯灌水（"谢谢""学习了""mark"这类）
        t = (d["content"] or "").strip()
        if len(t) < 12 and not d["has_code"]:
            continue
        d["content"] = t[:REPLY_CHARS]
        out.append(d)
    return out


def load_article(cur, post_id: str) -> dict | None:
    cur.execute("SELECT * FROM sa_strategy_article WHERE post_id=%s", (post_id,))
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip([c[0] for c in cur.description], r))
    for k in ("tags",):
        if k in d and not isinstance(d[k], list):
            try:
                d[k] = json.loads(d[k] or "[]")
            except (TypeError, ValueError):
                d[k] = []
    return d


def load_source(cur, post_id: str) -> dict | None:
    cur.execute("SELECT lang, code, lines, redacted, stub_sites, stub_reasons,"
                " n_blocks, origin, origin_author FROM sa_strategy_source"
                " WHERE post_id=%s", (post_id,))
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip([c[0] for c in cur.description], r))
    if not isinstance(d.get("stub_reasons"), list):
        try:
            d["stub_reasons"] = json.loads(d.get("stub_reasons") or "[]")
        except (TypeError, ValueError):
            d["stub_reasons"] = []
    d["raw_lines"] = d.get("lines") or 0
    return d


# ---------------- 落库 ----------------

def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        # 模型偶尔把数组写成一行逗号分隔的字符串
        return [x.strip() for x in re.split(r"[,;；]", v) if x.strip()]
    return []


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def normalize(res: dict) -> dict:
    """把模型输出整成能直接写库的形状。"""
    steps = []
    for i, s in enumerate(_as_list(res.get("steps")), 1):
        if not isinstance(s, dict):
            steps.append({"no": i, "what": str(s)[:300], "detail": "",
                          "evidence": ""})
            continue
        steps.append({
            "no": _int_or(s.get("no"), i),
            "what": str(s.get("what") or "")[:300],
            "detail": str(s.get("detail") or "")[:1200],
            "evidence": str(s.get("evidence") or "")[:300],
        })
    try:
        score = int(float(res.get("portable_score") or 0))
    except (TypeError, ValueError):
        score = 0
    return {
        "title_zh": str(res.get("title_zh") or "")[:200],
        "summary": str(res.get("summary") or "")[:3000],
        "strategy_type": str(res.get("strategy_type") or "")[:32],
        "steps": steps,
        "universe": [str(x)[:300] for x in _as_list(res.get("universe"))][:30],
        # 注意：字典推导式外面**不能**直接写 [:40] —— 推导式产生的是 dict,
        # 切片只对 list/tuple 有效。必须先转成 list 再切。
        "params": dict(list({
            str(k)[:60]: (v if isinstance(v, (int, float)) else str(v)[:300])
            for k, v in _as_dict(res.get("params")).items()
        }.items())[:40]),
        "perf_claimed": {str(k)[:60]: str(v)[:200]
                         for k, v in _as_dict(res.get("perf_claimed")).items()},
        # 除非明确说了 verified，否则一律 claimed —— 作者的话就是 claimed
        "perf_verified": "verified" if str(
            res.get("perf_verified") or "").strip().lower() == "verified"
        else "claimed",
        "backtest_period": str(_as_dict(res.get("perf_claimed")).get(
            "backtest_period") or "")[:200],
        "applicable": [str(x)[:300] for x in _as_list(res.get("applicable"))][:20],
        "unsuitable": [str(x)[:300] for x in _as_list(res.get("unsuitable"))][:20],
        "risk_notes": [str(x)[:300] for x in _as_list(res.get("risk_notes"))][:20],
        "dependencies": [str(x)[:200] for x in _as_list(res.get("dependencies"))][:20],
        "portable_score": max(0, min(5, score)),
        "portable_why": str(res.get("portable_why") or "")[:1500],
        "uncertainty": str(res.get("uncertainty") or "")[:2000],
        "needs_research": _truthy(res.get("needs_research")),
        "research_hint": str(res.get("research_hint") or "")[:1000],
    }


def _truthy(v) -> bool:
    """宽松地判断真假。分段格式里模型可能写 true / True / 是 / Y / 有。"""
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in ("true", "1", "yes", "y", "是", "有", "需要")


def _int_or(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def completeness(d: dict) -> int:
    """0~100：几个必填字段抽到了。给个粗分，方便前端排序。"""
    keys = ("title_zh", "summary", "strategy_type", "steps", "universe",
            "params", "perf_claimed", "applicable")
    hit = 0
    for k in keys:
        v = d.get(k)
        if v and (not isinstance(v, (list, dict)) or len(v) > 0):
            hit += 1
    return int(hit / len(keys) * 100)


def save_digest(cur, article_id: str, res: dict, cost: dict, model: str):
    d = normalize(res)
    comp = completeness(d)
    cur.execute(
        """INSERT INTO sa_strategy_digest
           (article_id, title_zh, summary, strategy_type, steps, universe,
            params, perf_claimed, perf_verified, backtest_period, applicable,
            unsuitable, risk_notes, dependencies, portable_score, portable_why,
            uncertainty, needs_research, research_hint,
            extract_model, extract_cost, completeness, raw_response)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   %s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (article_id) DO UPDATE SET
             title_zh=EXCLUDED.title_zh, summary=EXCLUDED.summary,
             strategy_type=EXCLUDED.strategy_type, steps=EXCLUDED.steps,
             universe=EXCLUDED.universe, params=EXCLUDED.params,
             perf_claimed=EXCLUDED.perf_claimed,
             perf_verified=EXCLUDED.perf_verified,
             backtest_period=EXCLUDED.backtest_period,
             applicable=EXCLUDED.applicable, unsuitable=EXCLUDED.unsuitable,
             risk_notes=EXCLUDED.risk_notes, dependencies=EXCLUDED.dependencies,
             portable_score=EXCLUDED.portable_score,
             portable_why=EXCLUDED.portable_why,
             uncertainty=EXCLUDED.uncertainty,
             needs_research=EXCLUDED.needs_research,
             research_hint=EXCLUDED.research_hint,
             extract_model=EXCLUDED.extract_model,
             extract_cost=EXCLUDED.extract_cost,
             completeness=EXCLUDED.completeness,
             -- 这里必须用 EXCLUDED，不能再给一个占位符。
             -- 下面这条警告本身就是踩坑记录，所以**注释里绝不能出现占位符
             -- 的字面量**（否则 psycopg2 会把它当真占位符，报
             -- IndexError: tuple index out of range，跟 SQL、跟数据、
             -- 跟数据库全都不相干，排查极其费劲）。我写第一版注释时
             -- 顺手在括号里引用了一遍那个字面量，当场又踩了一次。
             -- scripts/check_ddl.py 现在会专门检查这一条。
             raw_response=EXCLUDED.raw_response, extract_at=now()""",
        (article_id, d["title_zh"], d["summary"], d["strategy_type"],
         json.dumps(d["steps"], ensure_ascii=False),
         json.dumps(d["universe"], ensure_ascii=False),
         json.dumps(d["params"], ensure_ascii=False),
         json.dumps(d["perf_claimed"], ensure_ascii=False),
         d["perf_verified"], d["backtest_period"],
         json.dumps(d["applicable"], ensure_ascii=False),
         json.dumps(d["unsuitable"], ensure_ascii=False),
         json.dumps(d["risk_notes"], ensure_ascii=False),
         json.dumps(d["dependencies"], ensure_ascii=False),
         d["portable_score"], d["portable_why"],
         d["uncertainty"], d["needs_research"], d["research_hint"],
         model,
         json.dumps(cost, ensure_ascii=False), comp,
         json.dumps(res, ensure_ascii=False)[:20000]))
    return d, comp


def enqueue_extract(cur, post_id: str, force: bool = False):
    """把文章排进抽取队列。

    内容没变（content_hash 相同）且已经抽过 -> 不重复排队，**省一次 LLM 调用**。
    这条很重要：LLM 调用是真花钱的，而同一篇文章可能被我反复重新抓取。
    """
    if not force:
        cur.execute(
            """SELECT a.needs_reextract, d.article_id
               FROM sa_strategy_article a
               LEFT JOIN sa_strategy_digest d ON d.article_id = a.post_id
               WHERE a.post_id=%s""", (post_id,))
        r = cur.fetchone()
        if r and r[1] and not r[0]:
            return False
    cur.execute(
        """INSERT INTO sa_strategy_extract_queue (article_id, state, updated_at)
           VALUES (%s,'pending',now())
           ON CONFLICT (article_id) DO UPDATE SET
             state='pending', try_count=0, next_at=NULL, updated_at=now()""",
        (post_id,))
    return True


def claim_batch(cur, limit: int = 3):
    """取待抽取的任务并标记为 doing（防并发重复抽）。

    用 UPDATE ... WHERE state='pending' RETURNING 原子认领，
    不用「先 SELECT 再 UPDATE」—— 那是竞态。
    """
    cur.execute(
        """UPDATE sa_strategy_extract_queue SET state='doing', updated_at=now()
           WHERE article_id IN (
             SELECT article_id FROM sa_strategy_extract_queue
             WHERE state='pending' AND (next_at IS NULL OR next_at <= now())
             ORDER BY updated_at LIMIT %s FOR UPDATE SKIP LOCKED)
           RETURNING article_id""", (int(limit),))
    return [r[0] for r in cur.fetchall()]


def mark_done(cur, article_id: str):
    cur.execute("UPDATE sa_strategy_extract_queue SET state='done', "
                "try_count=try_count+1, updated_at=now() "
                "WHERE article_id=%s", (article_id,))
    cur.execute("UPDATE sa_strategy_article SET needs_reextract=FALSE "
                "WHERE post_id=%s", (article_id,))


def mark_failed(cur, article_id: str, err: str):
    """失败退避。连续失败 3 次就标 skip，别再烧钱重试。"""
    cur.execute(
        """UPDATE sa_strategy_extract_queue
           SET state = CASE WHEN try_count+1 >= 3 THEN 'skip' ELSE 'failed' END,
               try_count = try_count+1,
               last_error = %s,
               -- 指数退避 2/4/8… 分钟
               next_at = now() + (interval '1 minute' * (2 ^ LEAST(try_count, 6))),
               updated_at = now()
           WHERE article_id=%s""", (str(err)[:800], article_id))
