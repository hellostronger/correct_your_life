# -*- coding: utf-8 -*-
"""消息面判据 —— 模拟盘与实盘评审**共用的大脑**（2026-11-14 用户裁决）。

用户原话：「模拟盘与实盘买卖评审应该除了产出操作不一样，其他应该一样，
这个大脑应该是一样的，融合两个的大脑消息面判据，不要再搞特殊了」。

本模块是这个裁决的落点：**同一份**新闻挑选规则、同一份去重、同一份
情绪判据权重，模拟盘（喂 LLM 交易员）与实盘买卖评审（trade_review 规则
打分）都从这里取 —— 两边看到的消息面永远一致。

纯模块约束（与 trade_review 同款）：
- 不 import app、不碰库、不联网 —— SQL 取数在 app.py，这里只做纯计算；
- 阈值/权重常量集中在这里，两边引用，禁止任何一边私自改口径。

三个入口：
- ``pick_news(general, rating)``：评级/目标价新闻优先占名额 + 内容去重；
- ``face_rows(rows, side)``：方向化判据（利空对买入是 con、对卖出是 pro），
  实盘评审用；
- ``face_summary(rows)``：方向中性的判据摘要（把权重摆给 LLM 看），
  模拟盘喂交易员用。face_rows 与 face_summary 出自同一组常量与同一份
  数数逻辑，区别只在呈现方向。

背景（为什么必须有「评级优先占名额」）：智谱这种热点票一天能来 200+ 条
关联行业新闻，按时间倒序的任何 LIMIT 都会把前一天的「汇丰下调智谱目标价」
挤到 200 名开外 —— 而这恰恰是买卖决策最该看的一条。app.py 的
`_rating_news_rows` 在 SQL 里定向捞，这里负责把它排到名额前面。
"""
from __future__ import annotations

import re as _re

# ---------------- 口径常量（两边共用，改这里就是改两边） ----------------

# 标题里命中这些词的通用新闻，优先占名额（评级/目标价类是决策最该看的）
RATING_KW = ("评级", "目标价", "下调", "上调")

# 每条利空/利好新闻的分值与封顶。机构下调目标价/评级这类利空单条往往就够重，
# 但同一条事常被多家媒体转载（sentiment 都标 neg），去重后仍可能有多条
# 相关报道 —— 封顶是必须的，不许把一次降级放大成「十面埋伏」。
NEWS_NEG_W, NEWS_NEG_CAP = 8.0, 20.0
NEWS_POS_W, NEWS_POS_CAP = 5.0, 10.0

HEADLINE_CLIP = 60          # 单条标题进判据文案的截断长度
HEADLINES_IN_TEXT = 2       # 判据文案里带出的标题条数
RATING_QUOTA = 4            # 评级类新闻最多占的名额
NEWS_QUOTA = 10             # 传入决策/评审的新闻总名额
MACRO_QUOTA = 4             # 行业/宏观类新闻最多占的名额（2026-11-15 用户反馈
                            # 「很多重复的消息占用了预算」——智谱的 10 个名额里
                            # 8 个被「广发策略/北京供热/周大生」这类**标题互不
                            # 重复、但与该股无关**的全市场要闻吃掉。标题去重对
                            # 它们无能为力，必须按相关性分层限流）
TITLE_SIM_TH = 0.90        # 语义去重阈值：标题向量余弦 ≥ 此值判为同一件事
                            # （2026-11-15 用户问「向量聚类去重可以不」→ 可以，
                            # 且 embedding 通道现成：bge-m3 标题向量入库时算一次
                            # 存 sa_news.title_vec，这里纯本地比对、不联网 ——
                            # 网关挂了/向量缺失自动退回纯规则，不阻塞决策）


# ---------------- 去重（同一件事多家转载合并成一条） ----------------

def title_key(title: str) -> str:
    """标题的粗相似键：剥掉常见新闻前缀后取前 12 个实义字符。

    中文转载标题的套路是「快讯：X」「港股午间丨X」「机构：X」—— 同一件事
    加不同前缀。只剥一层已知前缀（冒号/竖线/空格前的短前缀，≤6 字），
    再取前 12 字做键。**故意不用** difflib 相似度 —— 那要 O(n²) 两两比较，
    这个函数会被 200 条的列表调用，宁可粗一点也要快。
    """
    s = str(title or "").strip()
    m = _re.match(r"^[\w\u4e00-\u9fff]{1,6}\s*[：:|丨\-—]\s*(.+)$", s)
    if m:
        s = m.group(1)
    s = _re.sub(r"[\s\W_]+", "", s)
    return s[:12]


def _norm_time(v) -> str:
    """publish_time 统一成 'YYYY-MM-DDTHH:MM' 字符串再比较/输出。

    DB 出来是 datetime 对象（str() 得 'YYYY-MM-DD HH:MM:SS'，**空格**分隔）、
    /api/news 出来是 ISO 字符串（**T** 分隔）—— 混在一起比较会错序
    （空格 0x20 < 'T' 0x54，'…T17:10' 竟大于 '… 18:00'）。这里统一成
    T 分隔、切到分钟。
    """
    return str(v or "")[:16].replace(" ", "T")


# ---------------- 语义聚类（标题向量，纯本地） ----------------

def _cos(a, b) -> float:
    """余弦相似度（纯 python，不依赖 numpy —— 这个模块要保持零三方依赖，
    向量最多 1024 维 × 几十条，纯 python 也就几十万次乘法，够快）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na * nb) ** 0.5)


def _semantic_merge(kept: list[dict]) -> list[dict]:
    """第二轮聚类：前缀 key 不同的「换标题转载」（「汇丰看空智谱」vs
    「智谱遭外资行下调目标价」）靠标题向量余弦 ≥ TITLE_SIM_TH 合并。

    与 title_key 轮同款保留策略：时间更早的当首发内容，dup_count 累加，
    被并掉的标题记进 dup_titles（呈现层可见「谁和谁合并了」）。
    没有向量的行跳过 —— 网关挂了/存量未回填时这层自动空转。
    """
    vecs = [x.get("title_vec") for x in kept]
    for i, v in enumerate(vecs):
        if not isinstance(v, list) or not v:
            vecs[i] = None
    out: list[dict] = []
    out_vecs: list[list | None] = []
    for i, x in enumerate(kept):
        v = vecs[i]
        target = -1
        if v:
            for j, w in enumerate(out_vecs):
                if w and _cos(v, w) >= TITLE_SIM_TH:
                    target = j
                    break
        if target < 0:
            out.append(dict(x))
            out_vecs.append(v)
            continue
        old = out[target]
        old["dup_count"] = old.get("dup_count", 1) + x.get("dup_count", 1)
        t = str(x.get("title") or "").strip()
        if t and t not in old.setdefault("dup_titles", []):
            old["dup_titles"].append(t)
        # 更早发布的当首发（与 title_key 轮一致）。⚠️ 换首发后 dup_titles
        # 要跟着换：原首发的标题（现在被并掉了）进 dup_titles，新首发的
        # 标题从 dup_titles 里去掉 —— 否则「谁和谁合并了」就记反了。
        ot, nt = old.get("publish_time") or "", str(x.get("publish_time") or "")
        if not ot or (nt and nt < ot):
            old_title = str(old.get("title") or "").strip()
            for k in ("title", "media", "source", "sentiment",
                      "publish_time", "url", "code"):
                if k in x:
                    old[k] = x[k]
            lst = old.get("dup_titles") or []
            if old_title and old_title != t and old_title not in lst:
                lst.append(old_title)
            if t in lst:
                lst.remove(t)
            out_vecs[target] = v
    return out


def dedup_news(rows: list[dict]) -> list[dict]:
    """同一条新闻的多次转载合并成一条（保留最早首发，标 dup_count）。

    「机构下调智谱目标价」这类消息会被新浪/腾讯/智通财经各发一遍，每条
    都标 neg —— 不去重的话：名额被同一件事占掉、按条数计分被放大成 N 倍。
    **两轮串联**：
      1. 前缀归一轮（title_key）：「快讯：X」型转载，O(n) 粗合并；
      2. 语义一轮（_semantic_merge）：换标题转载，标题向量余弦 ≥
         TITLE_SIM_TH 合并 —— 向量由调用方随行带（sa_news.title_vec，
         入库时 bge-m3 算一次），这里纯本地比对；缺向量的行自动跳过。
    保留行上的全部字段，但 **title_vec 是内部字段，返回前剥离**
    （1024 维浮点数组不该流进 API 响应/LLM 上下文）。
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    for x in rows if isinstance(rows, list) else []:
        if not isinstance(x, dict):
            continue
        k = title_key(x.get("title") or "")
        if not k:
            continue
        if k not in seen:
            kept = dict(x)
            kept["publish_time"] = _norm_time(kept.get("publish_time"))
            kept["dup_count"] = 1
            seen[k] = kept
            order.append(k)
            continue
        old = seen[k]
        # 同一件事：时间更早（或没有时间）的当首发
        ot, nt = old.get("publish_time") or "", _norm_time(x.get("publish_time"))
        if not ot or (nt and nt < ot):
            new = dict(x)
            new["publish_time"] = nt
            new["dup_count"] = old.get("dup_count", 1) + 1
            seen[k] = new
        else:
            old["dup_count"] = old.get("dup_count", 1) + 1
    merged = _semantic_merge([seen[k] for k in order])
    for x in merged:
        x.pop("title_vec", None)
    return merged


# ---------------- 名额分配（评级优先 + 去重） ----------------

def pick_news(general: list[dict], rating: list[dict] | None = None,
              quota: int = NEWS_QUOTA, rating_quota: int = RATING_QUOTA,
              tokens: list[str] | None = None) -> list[dict]:
    """把「定向捞的评级新闻」和「通用时间倒序新闻」合成一份名单。

    规则（与实盘评审完全同一份，模拟盘也用它）：
      1. 评级/目标价定向捞的（rating，app._rating_news_rows 产物）排最前，
         最多 rating_quota 条；
      2. 通用新闻里标题命中 RATING_KW 的也算优先，补进优先层；
      3. 剩下的按**相关性分层**（2026-11-15 加）：标题含股票名/搜索词
         （tokens，来自 sa_watchlist 的 keywords 配置）的「公司级/行业级」
         新闻优先占名额；**不含任何 token 的全市场要闻**（「广发策略：
         …」「北京15日起正式供热」）至多占 MACRO_QUOTA 条、排在名单尾部
         —— 标题去重合并不了它们（互不重复），但它们对个股决策近乎零信息，
         放任流入就是 10 个名额被吃掉 8 个；
      4. 全量过一遍 dedup_news —— 评级层与通用层会捞到同一条，必须去重。

    tokens 为空（取不到股票名/配置）时退化为旧行为：不分层，全部视为相关
    —— 宁可保守也不因缺配置而比旧口径差。名单上宏观条目带 ``rel="macro"``
    标记，其余带 ``rel="hit"``，便于呈现层/调试区分。
    """
    general = [x for x in (general or []) if isinstance(x, dict)]
    rating = [x for x in (rating or []) if isinstance(x, dict)]
    toks = [t for t in (tokens or []) if t]

    def _rel(x) -> bool:
        t = str(x.get("title") or "")
        return (not toks) or any(k in t for k in toks) \
            or any(k in t for k in RATING_KW)

    prio_ids = {id(x) for x in rating}
    prio = list(rating) + [x for x in general
                           if id(x) not in prio_ids
                           and any(k in str(x.get("title") or "") for k in RATING_KW)]
    rest = [x for x in general if id(x) not in prio_ids
            and x not in prio]
    rel_rows = [x for x in rest if _rel(x)]
    macro_rows = [x for x in rest if not _rel(x)]
    merged = dedup_news(prio[:max(rating_quota, 0)] + rel_rows + macro_rows)

    final = [x for x in merged if _rel(x)][:max(quota, 0)]
    room = max(0, min(max(quota, 0) - len(final), max(MACRO_QUOTA, 0)))
    for x in [x for x in merged if not _rel(x)][:room]:
        final.append(x)
    for x in final:
        x["rel"] = "macro" if not _rel(x) else "hit"
    return final


# ---------------- 情绪判据（同一个大脑的两种呈现） ----------------

def _counted(rows: list[dict], sentiment: str) -> list[dict]:
    """数情绪条数 —— **跳过 rel="macro" 的全市场要闻**（2026-11-15）。

    「广发策略：…」「北京供热」这类宏观文 sentiment 常被采集端误标 neg，
    混进计分就把利空条数虚增成 N 倍。分层限流后它们仍留在名单里给 LLM 看
    （市场大环境），但**机械计分只数与该股相关的**（无 rel 标记的视为相关
    —— 兼容未经 pick_news 的旧调用方）。
    """
    out = []
    for x in rows if isinstance(rows, list) else []:
        if not isinstance(x, dict) or x.get("rel") == "macro":
            continue
        if str(x.get("sentiment") or "").lower() == sentiment:
            out.append(x)
    return out


def _headline(items: list[dict], n: int = HEADLINES_IN_TEXT) -> str:
    parts = []
    for x in items[:n]:
        t = str(x.get("title") or "").strip()
        if not t:
            continue
        t = t if len(t) <= HEADLINE_CLIP else t[:HEADLINE_CLIP - 1] + "…"
        m = str(x.get("media") or x.get("source") or "").strip()
        dup = x.get("dup_count") or 1
        suffix = "（%s%s）" % (m, "，×%d家" % dup if dup > 1 else "") if m else ""
        parts.append("「%s」%s" % (t, suffix))
    return "；".join(parts)


def face_rows(rows: list[dict], side: str) -> list[dict]:
    """方向化消息面判据 —— 实盘评审（trade_review）用。

    返回 [{kind: 'con'|'pro', text, weight, inv}]，同方向同文案只有一条。
    利空：对买入是反对项、对卖出是支持项；利好反向。
    """
    side = str(side or "").lower()
    neg, pos = _counted(rows, "neg"), _counted(rows, "pos")
    out: list[dict] = []
    if side == "buy":
        if neg:
            out.append({
                "kind": "con",
                "text": "近期 %d 条利空新闻：%s" % (len(neg), _headline(neg)),
                "weight": min(NEWS_NEG_CAP, NEWS_NEG_W * len(neg)),
                "inv": "利空被证伪、或出现同量级的正面催化（财报/回购/大单），"
                       "这条反对意见才撤销"})
        if pos:
            out.append({
                "kind": "pro",
                "text": "近期 %d 条利好新闻：%s" % (len(pos), _headline(pos)),
                "weight": min(NEWS_POS_CAP, NEWS_POS_W * len(pos)),
                "inv": ""})
    elif side == "sell":
        if neg:
            out.append({
                "kind": "pro",
                "text": "近期 %d 条利空新闻支持减仓：%s" % (len(neg), _headline(neg)),
                "weight": min(NEWS_NEG_CAP, NEWS_NEG_W * len(neg)),
                "inv": "利空落地后股价没跌（坏消息钝化），减仓的紧迫性下降"})
        if pos:
            out.append({
                "kind": "con",
                "text": "近期 %d 条利好新闻，卖出等于逆着消息面：%s" % (
                    len(pos), _headline(pos)),
                "weight": min(NEWS_POS_CAP, NEWS_POS_W * len(pos)),
                "inv": ""})
    return out


def face_summary(rows: list[dict]) -> str:
    """方向中性的消息面判据摘要 —— 模拟盘喂 LLM 交易员用。

    与 face_rows 同一组常量、同一份数数逻辑，只是把「两个方向各值多少分」
    都摆出来，让 LLM 与实盘规则打分站在同一口径上 —— **不是**让它照抄
    结论，而是保证它看到的消息面权重与实盘评审一致，两边不会一个把
    评级下调当头条、一个把它挤到 200 名开外。
    """
    neg, pos = _counted(rows, "neg"), _counted(rows, "pos")
    if not neg and not pos:
        return ""
    lines = ["【消息面判据（系统预判，与实盘评审同口径）】"]
    if neg:
        lines.append(
            "- 利空 %d 条（每条对买入判 -%.0f 分、对卖出判 +%.0f 分，"
            "合计封顶 ∓%.0f）：%s" % (
                len(neg), NEWS_NEG_W, NEWS_NEG_W, NEWS_NEG_CAP, _headline(neg)))
    if pos:
        lines.append(
            "- 利好 %d 条（每条对买入判 +%.0f 分、对卖出判 -%.0f 分，"
            "合计封顶 ±%.0f）：%s" % (
                len(pos), NEWS_POS_W, NEWS_POS_W, NEWS_POS_CAP, _headline(pos)))
    lines.append("（以上为系统对新闻 sentiment 标签的机械统计，评级/目标价"
                 "类新闻已优先选取；请结合具体内容判断轻重，不要只数条数。）")
    return "\n".join(lines)
