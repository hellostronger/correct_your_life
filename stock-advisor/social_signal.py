# -*- coding: utf-8 -*-
"""社媒信号（公众号 / B站 / 微博 / X）→ 模拟交易决策上下文。

2026-09-29 加。之前决策上下文只有 sa_news（新闻）+ 解禁增发事件，社媒
（公众号文章、B站动态与评论、微博、X）虽然一直在抓、也在推微信，但**一条
都没进过 LLM 的决策上下文** —— 等于白抓。

## 为什么不能按公司名匹配

实测（36 只自选股）：
  sa_bili_dynamics  118 条   按公司名可关联   0 条 (0.0%)
  sa_mp_articles     12 条   按公司名可关联   0 条 (0.0%)
  sa_bili_comments 4363 条   按公司名可关联  13 条 (0.3%)

原因是社媒讲的是**板块和概念**，不讲公司全名：
  "PCB龙头，直线封涨停"      "算力租赁概念狂飙！南威软件20cm涨停"
  "重大利好！300亿创新药巨头股价飙涨16%"
这些里面一个自选股的公司名都没有。

## 三个信号层（按可信度从高到低）

1. **direct 直呼** — 内容里直接出现公司名或股票代码。高精度、低召回。
2. **concept 概念** — 命中该股配置的 social_keys（概念/板块词）。**只在配了
   才有**，不做任何默认猜测：宁可漏，不可错 —— 假阳性（"创新"把"创新药"匹配成
   蓝色光标、"涨停"把所有含该词的票全拉进来）喂给 LLM 比不给更糟。
3. **hot 全市场热度** — 不限股票，按互动量排的近期热门内容。**这是主力**：
   让 LLM 自己判断「现在什么题材在火，和这只票有没有关系」，比任何静态映射
   都准，而且天然没有假阳性。LLM 擅长的正是这种语义判断。

## 证据等级必须标注

社媒信息**非权威、可失真、可被操纵**。返回结构里每一层都带 basis 字段，
渲染时明确告诉 LLM 它的证据等级低于公告与新闻 —— 否则 LLM 会把 UP 主的
一句话当成事实。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# 各内容源：表名 -> 字段映射
# text_expr  取正文的表达式；ts_expr 时间；eng_expr 互动量（用于热度排序）
#
# 公众号的正文取 content_text（剥标签纯文本），**不再拼 title+summary** ——
# 2026-09-29 实测拼起来同一句话会重复 3 遍（title、summary、content_text 开头
# 高度重叠），注入给 LLM 是在浪费上下文还稀释信息密度。
_SOURCES = [
    {
        "key": "mp", "label": "公众号",
        "table": "sa_mp_articles",
        "text_expr": ("coalesce(nullif(a.content_text,''), a.summary, a.title, '')"
                      " || ' ' || coalesce(a.title,'')"),
        "ts_expr": "coalesce(a.published_at, a.fetched_at)",
        "eng_expr": "0",
        "author_expr": "coalesce(a.mp_name, '')",
        "title_expr": "coalesce(a.title, '')",
        "eng_label": "",
        "min_weight": 3,   # 见下方权重说明
    },
    {
        "key": "bili", "label": "B站动态",
        "table": "sa_bili_dynamics",
        "text_expr": "coalesce(b.title,'') || ' ' || coalesce(b.text,'')",
        "ts_expr": "b.pub_ts",
        "eng_expr": "coalesce((b.stats::jsonb ->> 'like')::bigint, 0)"
                    " + coalesce((b.stats::jsonb ->> 'comments')::bigint, 0)"
                    " + coalesce((b.stats::jsonb ->> 'repost')::bigint, 0)",
        "author_expr": "coalesce(b.author_name, '')",
        "title_expr": "coalesce(b.title, '')",
        "eng_label": "赞/评/转",
        "min_weight": 2,
    },
    {
        "key": "bili_c", "label": "B站评论",
        "table": "sa_bili_comments",
        "text_expr": "coalesce(c.content,'')",
        "ts_expr": "c.pub_ts",
        "eng_expr": "coalesce(c.like_count, 0)",
        "author_expr": "coalesce(c.author, '')",
        "title_expr": "''",
        "eng_label": "赞",
        # 评论是股吧闲聊的重灾区：2026-09-29 实测给智谱配了
        # AI/算力/大模型/芯片 四个概念词，概念层 6 条**全部**是评论，
        # 内容是「美股减持有没有资本利得税」这类，对决策毫无价值。
        # 概念层（精确匹配）排除评论；热门层（按互动量）仍可收 ——
        # 因为那里的评论是全市场最热的，LLM 自己会判断要不要理。
        "skip_concept": True,
    },
    {
        "key": "wb", "label": "微博",
        "table": "sa_wb_posts",
        "text_expr": "coalesce(w.text,'')",
        "ts_expr": "w.pub_ts",
        "eng_expr": "coalesce((w.stats::jsonb ->> 'like')::bigint, 0)"
                    " + coalesce((w.stats::jsonb ->> 'comments')::bigint, 0)"
                    " + coalesce((w.stats::jsonb ->> 'repost')::bigint, 0)",
        "author_expr": "coalesce(w.author_name, '')",
        "title_expr": "''",
        "eng_label": "赞/评/转",
        "min_weight": 2,
    },
    {
        "key": "x", "label": "X",
        "table": "sa_x_tweets",
        "text_expr": "coalesce(t.text,'')",
        "ts_expr": "t.created_at",
        "eng_expr": "coalesce((t.stats::jsonb ->> 'like')::bigint, 0)",
        "author_expr": "coalesce(t.display_name, t.username, '')",
        "title_expr": "''",
        "eng_label": "赞",
        "min_weight": 2,
    },
]

_ALIAS = {
    "sa_mp_articles": "a", "sa_bili_dynamics": "b",
    "sa_bili_comments": "c", "sa_wb_posts": "w", "sa_x_tweets": "t",
}

DEFAULT_CONF = {
    "enabled": True,
    "hours": 48,             # 时间窗：社媒热度衰减快，48 小时足够
    "hot_top": 12,           # 全市场热门条数
    "direct_top": 6,         # 直呼该股的条数
    "concept_top": 6,        # 概念匹配条数
    "snippet": 160,          # 每条正文截断长度
    "hot_min_eng": 0,        # 热门榜最低互动量（0=不限）
    "hot_max_age_h": 24,     # 热门榜只看最近 N 小时（比时间窗更短，只要"当下火"）
}


def load_conf(conf: dict | None = None) -> dict:
    c = dict(DEFAULT_CONF)
    for k, v in (conf or {}).items():
        if k in c and v is not None:
            try:
                c[k] = type(DEFAULT_CONF[k])(v)
            except (TypeError, ValueError):
                pass
    return c


def _clean(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


def _names(code: str, name: str) -> list[str]:
    """公司名别名（与 sa_news 交叉关联同一套思路）。"""
    out = {name} if name else set()
    for suf in ("-W", "-U", "-SW", "-B", "股份", "有限公司", "集团", "控股"):
        if name and name.endswith(suf) and len(name) - len(suf) >= 2:
            out.add(name[: -len(suf)])
    # 去掉「贵州」这类地域前缀的短名（茅台、国电南瑞）
    for pre in ("贵州", "江苏", "浙江", "中国", "北京", "上海", "广东"):
        if name and name.startswith(pre) and len(name) - len(pre) >= 2:
            out.add(name[len(pre):])
    return [x for x in out if len(x) >= 2]


def _split_keys(raw: str) -> list[str]:
    """去重且保序（同一个词配两次不该在输出里出现两遍）。"""
    out, seen = [], set()
    for k in re.split(r"[,，、]", raw or ""):
        k = k.strip()
        if len(k) >= 2 and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _norm_blob(s: str) -> str:
    """去重用：抹掉空白与常见标点，公众号正文与标题才会被判为同一条。"""
    return re.sub(r"[\s\W_]+", "", (s or ""), flags=re.UNICODE).lower()


def _fetch(deps: dict, hours: int = 48, limit: int = 400) -> list[dict]:
    """跨源取数。每源一条 SQL，失败只跳过该源（社媒表可能根本不存在）。"""
    out: list[dict] = []
    for src in _SOURCES:
        al = _ALIAS[src["table"]]
        sql = ("SELECT %s AS text, %s AS title, %s AS author, %s AS ts, "
               "(%s) AS eng FROM %s %s "
               "WHERE %s IS NOT NULL AND %s > now() - interval '%%s hours' "
               "ORDER BY ts DESC LIMIT %%s"
               % (src["text_expr"], src["title_expr"], src["author_expr"],
                  src["ts_expr"], src["eng_expr"], src["table"], al,
                  src["ts_expr"], src["ts_expr"]))
        try:
            with deps["get_conn"]() as conn, conn.cursor() as cur:
                cur.execute(sql, [hours, limit])
                cols = [d[0] for d in cur.description]
                for r in cur.fetchall():
                    d = dict(zip(cols, r))
                    d["_src"] = src["label"]
                    d["_eng_label"] = src["eng_label"]
                    d["_src_key"] = src["key"]
                    # 互动量为 0 的源（公众号没有互动量字段）排在有互动量的后面，
                    # 否则它会被当成"零热度"而被排序挤掉。
                    d["_w"] = int(src.get("min_weight") or 0)
                    d["_skip_concept"] = bool(src.get("skip_concept"))
                    out.append(d)
        except Exception:
            continue
    return out


def _score(it: dict) -> tuple:
    """排序键：互动量、来源权重、时间。没有互动量的源靠 min_weight 兜底。"""
    try:
        eng = int(it.get("eng") or 0)
    except (TypeError, ValueError):
        eng = 0
    ts = it.get("ts")
    return (eng > 0, eng, it.get("_w", 0), ts)


def _fmt(item: dict, conf: dict) -> str:
    body = _clean(item.get("text") or "", conf["snippet"])
    parts = ["[%s" % item["_src"]]
    a = (item.get("author") or "").strip()
    if a:
        parts.append(a)
    try:
        ts = item.get("ts")
        parts.append(ts.strftime("%m-%d %H:%M") if hasattr(ts, "strftime") else "")
    except Exception:
        pass
    eng = item.get("eng") or 0
    if eng and item["_eng_label"]:
        parts.append("%s %d" % (item["_eng_label"], eng))
    head = " ".join(x for x in parts if x)
    return "%s] %s" % (head, body)


def for_stock(deps: dict, code: str, name: str, social_keys: str = "",
              conf: dict | None = None) -> dict:
    """取该股的三层社媒信号。返回 {direct, concept, hot, note}。

    social_keys：该股的概念/板块词（来自 sa_watchlist.social_keys）。
      **没配就只做 direct + hot** —— 不猜、不用默认映射表。理由见模块头：
      假阳性比漏报更有害（"创新"→蓝色光标、"涨停"→全部含该词的票）。
    """
    c = load_conf(conf)
    res = {"direct": [], "concept": [], "hot": [], "note": ""}
    if not c.get("enabled"):
        res["note"] = "社媒信号已关闭（paper.social.enabled=false）"
        return res
    try:
        hours = int(c["hours"])
    except (TypeError, ValueError):
        hours = DEFAULT_CONF["hours"]

    # ---- 一次性取时间窗内全部内容（量级不大：社媒表都是几千到万级） ----
    items = _fetch(deps, hours=hours, limit=400)
    if not items:
        res["note"] = ("社媒无近期内容（公众号/B站/微博/X 都没有近 %d 小时的新内容，"
                       "或采集模块未运行）" % hours)
        return res

    # 跨源去重：公众号的 text 是「正文 + 标题」拼的，和只取标题的源会撞；
    # 同一件事被多个源转发更是常态。抹掉空白标点后比对。
    seen_blob, uniq = set(), []
    for it in items:
        key = _norm_blob(it.get("title") or it.get("text") or "")[:60]
        if not key or key in seen_blob:
            continue
        seen_blob.add(key)
        uniq.append(it)
    items = uniq

    # ---- 层 1：直呼 ----
    names = _names(code, name)
    d_hits = [it for it in items
              if any(nm in (it.get("text") or "") for nm in names)
              or (code and code in (it.get("text") or ""))]
    d_hits.sort(key=_score, reverse=True)
    res["direct"] = [_fmt(x, c) for x in d_hits[: int(c["direct_top"])]]

    # ---- 层 2：概念（仅在配了 social_keys 时才有；排除评论） ----
    keys = _split_keys(social_keys)
    if keys:
        c_hits = []
        for it in items:
            if it.get("_skip_concept"):
                continue
            blob = it.get("text") or ""
            hit = [k for k in keys if k in blob]
            if hit:
                c2 = dict(it)
                c2["_kw"] = "/".join(hit[:3])
                c_hits.append(c2)
        c_hits.sort(key=_score, reverse=True)
        res["concept"] = ["（命中概念：%s）%s" % (x["_kw"], _fmt(x, c))
                          for x in c_hits[: int(c["concept_top"])]]

    # ---- 层 3：全市场热度（主力信号，交给 LLM 自己判断相关性） ----
    h_hits = [x for x in items
              if (int(x.get("eng") or 0) if str(x.get("eng") or "0").lstrip("-").isdigit()
                  else 0) >= int(c["hot_min_eng"])]
    h_hits.sort(key=_score, reverse=True)
    res["hot"] = [_fmt(x, c) for x in h_hits[: int(c["hot_top"])]]

    if not res["direct"] and not res["concept"] and not res["hot"]:
        res["note"] = "社媒无近期内容"
    return res
