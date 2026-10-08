# -*- coding: utf-8 -*-
"""策略库采集编排：列表 -> 入队（防重）-> 详情 -> 源码 -> 落库 -> 触发抽取。

防重复爬取的层次（这是你要求的重点）
--------------------------------------
1) **列表阶段**：每条帖子算 url_hash（sha1），INSERT ... ON CONFLICT DO NOTHING。
   冲突 = 之前见过，**根本不发详情请求**（省掉最贵的那一跳）。
   rank_hint 用 hot 度做排序，让热门先入队。
2) **详情阶段**：再算一次正文 content_hash。与库里已有的一致 -> 跳过抽取
   （内容没变，重新抽只是浪费 LLM）。
3) **人工裁决**：dismissed 的帖子不再入队（用户说过「不想要」就别再来烦）。
4) 失败退避：next_retry_at = now + 2^retry_count 分钟，避免死帖把队列堵死。

注意 crawl_queue 存的是**所有见过的帖子**（包括没抓详情的），
所以「是否已抓过详情」看 sa_strategy_article 有没有对应 post_id，
而不是看队列状态 —— 队列状态只管「要不要再试」。
"""
import json
import re
from datetime import datetime, timedelta

import joinquant_source as JS

SOURCE = "joinquant"

# 每篇抓多少条评论。聚宽热门帖 replyCount 能到 7000+，全抓既慢又没用
# （绝大多数是「谢谢」「学习了」）。抓第一页 50 条，排序交给
# strategy_extract.load_replies 按信息价值做。
REPLY_LIMIT = 50


def _client(deps):
    conf = deps.get("conf") or {}
    return JS.Client(token=conf.get("token", ""),
                     timeout=int(conf.get("timeout", 25)),
                     min_interval=float(conf.get("min_interval", 0.6)))


def _log(cur, url_hash: str, level: str, stage: str, message: str):
    try:
        cur.execute("INSERT INTO sa_crawl_log (url_hash, level, stage, message) "
                    "VALUES (%s,%s,%s,%s)",
                    (url_hash, level, stage, (message or "")[:2000]))
    except Exception:                   # noqa: BLE001
        pass


# ---------------- 阶段 1：列表入队 ----------------

def enqueue(deps: dict, pages: int = 1, limit: int = 20, cate: int = 3,
            type_: str = "isNew") -> dict:
    """抓列表页并入队（防重靠 uniqueKey 的唯一键）。

    **这里踩过一个很隐蔽的坑**：第一版我用站点返回的 `postId` 算 url_hash，
    实测同一页连抓两次 new=22 / dup=0，44 行全是重复 —— 因为聚宽的 postId
    **每次请求都重新签发**（同一篇「聚宽新手指南」两次拿到完全不同的 32 位
    id，连 userId 都是）。换成稳定的 `uniqueKey` 之后 dup 才真的生效。
    """
    cli = _client(deps)
    get_conn = deps["get_conn"]
    seen = new = 0
    total = 0
    no_ukey = 0
    errors = []
    for page in range(1, max(1, int(pages)) + 1):
        try:
            res = cli.list_posts(page=page, limit=limit, cate=cate, type_=type_)
        except Exception as exc:          # noqa: BLE001
            errors.append("page%d: %s" % (page, str(exc)[:100]))
            break
        total = res["total"]
        posts = res["posts"]
        if not posts:
            break
        with get_conn() as conn:
            with conn.cursor() as cur:
                for p in posts:
                    uk = p.get("ukey") or ""
                    if not uk:
                        # 没有 uniqueKey 就没法稳定去重，宁可不入队，
                        # 也不能拿会变的 postId 造出一堆永远重复的行
                        no_ukey += 1
                        continue
                    u = JS.post_url(uk)
                    uh = JS.url_hash(u)
                    # hot 度：回复+点赞+收藏+克隆，粗略代表「有多少人在用/讨论」
                    hot = (p["reply_count"] + p["like_count"]
                           + p["collect_count"] + p["clone_count"] * 5)
                    cur.execute(
                        """INSERT INTO sa_crawl_queue
                           (url_hash, url, site, rank_hint, title_hint, note)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (url_hash) DO NOTHING""",
                        (uh, u, SOURCE, hot, p["title"][:255],
                         "有代码" if "```" in (p["content"] or "") else ""))
                    seen += 1
                    if cur.rowcount:
                        new += 1
                conn.commit()
    return {"ok": not errors, "scanned": seen, "new": new, "dup": seen - new,
            "library_total": total, "no_ukey": no_ukey, "errors": errors}


# ---------------- 阶段 2：抓详情 + 源码 ----------------

def fetch_details(deps: dict, limit: int = 20, only_with_code: bool = False,
                  with_replies: bool = True) -> dict:
    """取详情 + 源码并落库。

    only_with_code=True 时只保存有源码的（其余标 skipped）—— 可以不抓，
    因为实测列表摘要是截断的，60 篇里 0 篇在摘要里带代码，所以**默认 False**
    才对（否则会几乎啥也抓不到）。

    with_replies=True（默认）会连评论区一起抓。**别为了省请求关掉** ——
    抽取质量直接靠它。
    """
    cli = _client(deps)
    get_conn = deps["get_conn"]
    got = skipped = has_code = redacted = 0
    errs = []

    with get_conn() as conn, conn.cursor() as cur:
        # 先对账：队列标了 fetched 但库里没有对应文章的（文章被删过、
        # 上次跑崩了、旧版用错 id 留下的），退回 pending 重抓。
        # 不做这一步，这些行会永远卡在 fetched，新帖永远排在它们后面。
        cur.execute(
            """UPDATE sa_crawl_queue q SET crawl_state='pending',
                   last_error='对账：队列说抓过但文章不在库'
               WHERE q.site=%s AND q.crawl_state='fetched'
                 AND NOT EXISTS (SELECT 1 FROM sa_strategy_article a
                                 WHERE a.url_hash = q.url_hash)""", (SOURCE,))
        requeued = cur.rowcount
        conn.commit()
        if requeued:
            print("[jq] 对账：%d 条队列状态与实际不符，已退回 pending"
                  % requeued, flush=True)

        cur.execute(
            """SELECT url_hash, url, title_hint FROM sa_crawl_queue
               WHERE site=%s
                 AND (crawl_state='pending'
                      OR (crawl_state='failed' AND (next_retry_at IS NULL
                                                   OR next_retry_at <= now())))
               ORDER BY rank_hint DESC LIMIT %s""", (SOURCE, int(limit)))
        queue = cur.fetchall()
        print("[jq] 待抓 %d 条" % len(queue), flush=True)

        for uh, url, hint in queue:
            # url 尾部就是 uniqueKey（稳定 id），直接拿去查详情 ——
            # 实测 detailV2?postId=<uniqueKey> 有效，不依赖列表里的临时 postId
            uk = url.rsplit("=", 1)[-1]
            try:
                d = cli.detail(uk)
            except Exception as exc:      # noqa: BLE001
                skipped += 1
                errs.append("%s: %s" % (uk[:8], str(exc)[:80]))
                _bump_fail(cur, uh, str(exc)[:300])
                continue
            if not d or not d.get("post_id"):
                skipped += 1
                _bump_fail(cur, uh, "详情为空")
                continue

            md = d["content"] or ""
            txt = JS.md_to_text(md)
            ch = JS.content_hash(txt)

            # 评论区什么时候抓？
            # 原来只在「正文没抽到源码」时才抓，省请求。但那是错的：
            # 正文有代码的帖子，评论区同样有高价值内容 —— 别人贴的改进版、
            # 「这策略在XX市况下失效」的实测反馈、作者自己补的参数。
            # 而 LLM 抽取恰恰最需要这些（你要求「原文与评论区里的源码要
            # 特别参考」）。所以默认两个都抓，with_replies=False 才省。
            src = JS.extract_source(md, None)
            replies = []
            want_replies = with_replies or (not src["has_code"] and d.get("reply_count"))
            if want_replies and d.get("reply_count"):
                try:
                    replies = cli.replies(d["post_id"], limit=REPLY_LIMIT, page=1)
                    if not src["has_code"]:
                        src = JS.extract_source(md, replies)
                except Exception:          # noqa: BLE001
                    replies = []
            if src["has_code"]:
                has_code += 1
                if src["source"].get("redacted"):
                    redacted += 1
            if only_with_code and not src["has_code"]:
                skipped += 1
                cur.execute("UPDATE sa_crawl_queue SET crawl_state='skipped', "
                            "fetch_time=now() WHERE url_hash=%s", (uh,))
                continue

            _store_article(cur, d, uh, url, md, txt, ch, src, replies)
            got += 1
            print("[jq] %s %s（%d 字%s%s）" % (
                uk[:8], (d["title"] or "")[:30], len(txt),
                "，源码 %d 行/%s" % (src["source"]["lines"],
                                 "评论区" if src["source"].get("origin") == "reply"
                                 else "正文") if src["has_code"] else "",
                "，**脱敏 %d 处**" % src["source"]["stub_sites"]
                if src["has_code"] and src["source"].get("redacted") else ""),
                flush=True)
        conn.commit()
    return {"ok": not errs, "fetched": got, "skipped": skipped,
            "with_code": has_code, "redacted": redacted,
            "requeued": requeued, "errors": errs[:10]}


def _bump_fail(cur, uh: str, err: str):
    cur.execute(
        """UPDATE sa_crawl_queue
           SET crawl_state='failed', retry_count=retry_count+1,
               fetch_time=now(), last_error=%s,
               -- 指数退避 2/4/8/16…分钟，避免死帖反复重试拖慢队列
               next_retry_at = now() + (interval '1 minute'
                                        * (2 ^ LEAST(retry_count, 8)))
           WHERE url_hash=%s""", (err[:500], uh))
    _log(cur, uh, "error", "detail", err)


def _store_replies(cur, post_id: str, replies: list[dict], author: str = ""):
    """落库评论区。

    为什么必须存：**评论区的信息密度常常比正文高**。实测「聚宽新手指南」
    正文 825 字一个代码块都没有，但下面 7000 多条回复里全是实操问答；
    也有「源码在哪」的答案是作者自己在评论里贴的。第一版我把 replies
    拼成个 list 就扔了（拼完没落库），等于把最该看的那部分丢了。
    """
    if not replies:
        return
    cur.execute("DELETE FROM sa_strategy_reply WHERE article_id=%s", (post_id,))
    rows = []
    for e in replies:
        txt = e.get("content") or ""
        nblk = len(JS.code_blocks(txt))
        who = (e.get("author") or "").strip()
        rows.append((e.get("reply_id") or JS.content_hash(txt),
                     post_id, who, txt, len(txt), nblk > 0, nblk,
                     bool(author) and who == author.strip(),
                     e.get("backtest_id", ""), e.get("backtest_name", ""),
                     e.get("add_time") or None))
    cur.executemany(
        """INSERT INTO sa_strategy_reply
           (reply_id, article_id, author, content, content_len, has_code,
            n_code_blocks, is_author, backtest_id, backtest_name, add_time)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (reply_id) DO NOTHING""", rows)


def _store_article(cur, d: dict, uh: str, url: str, md: str, txt: str,
                   ch: str, src: dict, replies: list[dict]):
    s = src.get("source") or {}
    ev = []
    for e in replies[:20]:
        ev.append({"author": e.get("author"), "date": e.get("add_time"),
                   "has_code": bool(JS.code_blocks(e.get("content") or "")),
                   "text": (e.get("content") or "")[:500]})
    cur.execute(
        """INSERT INTO sa_strategy_article
           (post_id, src_post_id, site, url_hash, url, title, author, author_id,
            content_md, content_text, content_hash, tags,
            view_count, like_count, reply_count, collect_count, clone_count,
            published_at, updated_at_s, last_active_at, fetched_at,
            needs_reextract)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   %s,%s,%s,now(),%s)
           ON CONFLICT (post_id) DO UPDATE SET
             src_post_id=EXCLUDED.src_post_id,
             title=EXCLUDED.title, author=EXCLUDED.author,
             content_md=EXCLUDED.content_md,
             content_text=EXCLUDED.content_text,
             content_hash=EXCLUDED.content_hash, tags=EXCLUDED.tags,
             view_count=EXCLUDED.view_count, like_count=EXCLUDED.like_count,
             reply_count=EXCLUDED.reply_count,
             collect_count=EXCLUDED.collect_count,
             clone_count=EXCLUDED.clone_count,
             updated_at_s=EXCLUDED.updated_at_s, fetched_at=now(),
             -- 内容变了才需要重新抽取；没变就别浪费 LLM
             needs_reextract = (sa_strategy_article.content_hash
                                IS DISTINCT FROM EXCLUDED.content_hash)
        """,
        (d["post_id"], d.get("ephemeral_id", ""), SOURCE, uh, url, d["title"],
         d["author"], d["author_id"],
         md, txt, ch, json.dumps(d["tags"], ensure_ascii=False),
         d["view_count"], d["like_count"], d["reply_count"],
         d["collect_count"], d["clone_count"],
         d["add_time"] or None, d["mod_time"] or None, d["last_active"] or None,
         True))
    # 文章插入后才能插回复（外键约束 sa_strategy_reply_article_id_fkey）
    _store_replies(cur, d["post_id"], replies, author=d.get("author", ""))
    # 抓完就排进 LLM 抽取队列。延迟 import 避免循环依赖
    # （strategy_extract 不 import 本模块，但它 import llm_advisor）
    try:
        import strategy_extract as SX
        SX.enqueue_extract(cur, d["post_id"])
    except Exception as exc:                # noqa: BLE001
        _log(cur, uh, "warn", "enqueue_extract", str(exc)[:200])
    # 源码单独存（内容长，不适合塞 JSONB 字段做检索）
    cur.execute("DELETE FROM sa_strategy_source WHERE post_id=%s", (d["post_id"],))
    if src["has_code"]:
        # blocks 里只存结构（不含 code 正文，正文在 code 字段里，
        # 免得同一份代码在库里存两遍）
        slim = [{k: v for k, v in b.items() if k != "code"} | {"chars": b["chars"]}
                for b in (src.get("blocks") or [])]
        cur.execute(
            """INSERT INTO sa_strategy_source
               (post_id, lang, code, lines, origin, origin_author,
                origin_reply_id, redacted, stub_sites, stub_reasons,
                n_blocks, blocks, other_blocks)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (d["post_id"], s.get("lang", ""), s.get("code", ""),
             int(s.get("lines") or 0), s.get("origin", ""),
             s.get("author", ""), s.get("reply_id", ""),
             bool(s.get("redacted")), int(s.get("stub_sites") or 0),
             json.dumps(s.get("stub_reasons") or [], ensure_ascii=False),
             int(s.get("n_blocks") or 1),
             json.dumps(slim, ensure_ascii=False),
             json.dumps([{k: v for k, v in b.items() if k != "code"}
                         for b in (src.get("other_blocks") or [])],
                        ensure_ascii=False)))
    cur.execute("UPDATE sa_crawl_queue SET crawl_state='fetched', "
                "fetch_time=now(), retry_count=0, last_error='' "
                "WHERE url_hash=%s", (uh,))
    _log(cur, uh, "info", "detail",
         "抓到 %s，%d 字，源码=%s%s" % (
             d["post_id"][:12], len(txt),
             ("%d行/%d块" % (s.get("raw_lines", 0), s.get("n_blocks", 1)))
             if src["has_code"] else "无",
             "（脱敏 %d 处）" % s["stub_sites"] if s.get("redacted") else ""))


# ---------------- 阶段 3：统计 ----------------

def stats(deps: dict) -> dict:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT crawl_state, count(*) FROM sa_crawl_queue "
                    "WHERE site=%s GROUP BY crawl_state", (SOURCE,))
        q = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT count(*) FROM sa_strategy_article")
        arts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sa_strategy_source")
        srcs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sa_strategy_source "
                    "WHERE NOT redacted")
        runnable = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sa_strategy_digest")
        digs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sa_strategy_article "
                    "WHERE needs_reextract")
        need = cur.fetchone()[0]
    return {"queue": q, "articles": arts, "with_source": srcs,
            # 真正能直接跑的（作者没省略核心逻辑）—— 这才是能拿来回测的数量
            "runnable_source": runnable,
            "digested": digs, "need_extract": need,
            "fetch_rate": round((arts / q.get("fetched", 1) * 100), 1)
            if q.get("fetched") else 0.0}
