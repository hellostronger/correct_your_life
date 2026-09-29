# -*- coding: utf-8 -*-
"""挖新股：从现有各内容源 + 名册里找出「还没进自选股、但有信号」的票。

分工（2026-09-29）
------------------
系统的新闻是**按自选股关键词**抓的，所以它天然发现不了新股票 —— 那 10066 行新闻里
出现过的代码 100% 都在自选股里。本模块换一条路：

  1. 名册（stock_roster）：5638 只的 全称->代码 字典 + 上市日期 + 行业
  2. 扫现有内容源（公众号/B站/微博/X + 板块快照）的**正文**，
     用名册里的**公司全称**做最长匹配 -> 解析出代码
  3. 排除已在自选股的、用户已忽略的
  4. 打分排序，分「高置信自动加自选」与「候选池待人工审阅」

为什么按全称匹配而不是概念词
----------------------------
之前社媒概念层踩过：手写静态概念表，「创新药」里的「创新」把蓝色光标也拉进来了。
这里用的是交易所登记的**公司全称**，全市场 5638 只里只有 1 个两字名，歧义天然极低。
命中就用**最长优先**，短名若被更长的名字覆盖则丢弃（「中国平安」优先于「中国」）。

每条证据都留着原文片段 + 来源 + 链接，页面上直接展示 —— 挖错了一眼能看出来，
这是让「自动加自选」敢开的前提。
"""
import json
import re
from datetime import datetime, timedelta

# ---------------- 内容源 ----------------
# 每项：表名、正文列、标题列、时间列、来源标签、URL 列（可空）
# 说明：B站只扫 dynamics 不扫 comments —— 实测概念层命中 6 条全是股吧闲聊，
# 评论区的噪音远大于信息量（这条判断来自 social_signal 的实测结论）。
SOURCES = [
    {"key": "mp", "label": "公众号", "table": "sa_mp_articles", "pk": "id",
     "text": ["title", "content_text"], "title": "title",
     "ts": "published_at", "url": "url", "extra": "mp_name",
     "weight": 1.4, "enabled": True,
     "why": "公众号是有实质内容的渠道（研报转载/行业分析），信噪比最高"},
    {"key": "bili", "label": "B站动态", "table": "sa_bili_dynamics", "pk": "dynamic_id",
     "text": ["title", "text"], "title": "title",
     "ts": "pub_ts", "url": "bvid", "extra": "author_name",
     "weight": 0.8, "enabled": True,
     "why": "动态含实盘讨论，但混着大量表情/闲聊，权重压低"},
    {"key": "wb", "label": "微博", "table": "sa_wb_posts", "pk": "note_id",
     "text": ["text"], "title": None,
     "ts": "pub_ts", "url": None, "extra": "author_name",
     "weight": 0.9, "enabled": True,
     "why": "短线情绪最快，但谣言也多，权重低于公众号"},
    {"key": "x", "label": "X", "table": "sa_x_tweets", "pk": "id",
     "text": ["text"], "title": None,
     "ts": "created_at", "url": "url", "extra": "username",
     "weight": 1.0, "enabled": True,
     "why": "多为海外视角/产业链信息，中文名命中率偏低"},
]

# 名册里的行业 -> 概念词（用于「热门行业里的新面孔」）。刻意短且具体，
# 长尾概念交给社媒正文层去发现，不做穷举。
HOT_INDUSTRY_ALIAS = {
    "计算机、通信和其他电子设备制造业": ["算力", "光模块", "存储", "PCB", "消费电子"],
    "软件和信息技术服务业": ["信创", "AI应用", "SaaS"],
    "医药制造业": ["创新药", "CXO", "疫苗", "医疗器械"],
    "专用设备制造业": ["机器人", "数控机床", "检测设备"],
    "电气机械和器材制造业": ["锂电", "光伏", "储能", "逆变器"],
    "通用设备制造业": ["工业母机", "叉车", "压缩机"],
    "化学原料和化学制品制造业": ["磷化工", "氟化工", "农药"],
    "汽车制造业": ["智能驾驶", "一体化压铸", "出海车"],
    "有色金属冶炼和压延加工业": ["稀土", "铜铝", "钨钼"],
    "半导体": ["半导体", "晶圆", "封测"],
}


def _src_cols(src: dict) -> list[str]:
    """与 _rows 的 SELECT 列顺序严格一致。主键列名各表不同，必须显式配置。"""
    cols = [src["pk"]]
    cols += src["text"] + ([src["title"]] if src["title"] else [])
    return list(dict.fromkeys(cols))


def _rows(cur, src: dict, since) -> tuple[list[tuple], str]:
    """取一个源最近 N 小时的正文。列名全部来自 SOURCES 常量，不接受外部输入。

    返回 (行, 错误说明)。**必须用 SAVEPOINT 隔离单源失败**：PostgreSQL 里
    一条语句报错后整个事务就进入 aborted 状态，后面所有命令都会被拒
    （InFailedSqlTransaction）。原来这里直接 `except: return []`，结果是
    一个源抓不到就把整轮挖掘带崩 —— 而这里本来只是想「跳过这一路」。
    """
    cols = _src_cols(src)
    sql = ("SELECT %s FROM %s WHERE %s >= %%s" % (
        ", ".join(cols), src["table"], src["ts"]))
    sp = "sp_" + src["key"]
    cur.execute("SAVEPOINT %s" % sp)
    try:
        cur.execute(sql, (since,))
        rows = [tuple(r) for r in cur.fetchall()]
        cur.execute("RELEASE SAVEPOINT %s" % sp)
        return rows, ""
    except Exception as exc:      # noqa: BLE001
        cur.execute("ROLLBACK TO SAVEPOINT %s" % sp)
        cur.execute("RELEASE SAVEPOINT %s" % sp)
        return [], "%s（表/列可能还没建）: %s" % (src["label"], str(exc)[:80])


def _norm(s: str) -> str:
    """匹配用：去掉所有非中文非数字非字母的符号，全角转半角。"""
    if not s:
        return ""
    s = str(s)
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:
            ch = " "
        if (0x4E00 <= o <= 0x9FFF or ch.isalnum()):
            out.append(ch)
    return "".join(out)


def _snippet(norm_text: str, name: str, radius: int = 45) -> str:
    """从规范化文本里截一段带上下文的证据片段。"""
    i = norm_text.find(name)
    if i < 0:
        return ""
    lo = max(0, i - radius)
    hi = min(len(norm_text), i + len(name) + radius)
    pre = "…" if lo > 0 else ""
    post = "…" if hi < len(norm_text) else ""
    return pre + norm_text[lo:hi] + post


def _find_names(norm_text: str, index: dict) -> list[str]:
    """在文本里找公司全称，最长优先，短名被包含则丢弃。

    最长优先是必要的：文本里同时有「中国平安」和「平安银行」时，
    两边都应该命中（不同公司）；但若名册里有个 3 字名恰好是另一个 4 字名的
    前缀，只该算长的那个，否则会重复计数。
    """
    if not norm_text:
        return []
    hits = []
    for name in index:
        if len(name) >= 3 and name in norm_text:
            hits.append(name)
    if len(hits) <= 1:
        return hits
    hits.sort(key=len, reverse=True)
    kept: list[str] = []
    for n in hits:
        if not any(n != k and n in k for k in hits):
            kept.append(n)
    return kept


# ---------------- 打分 ----------------
# 权重设计意图：多源交叉 > 单源高频。单源高频往往是一个人的观点，
# 多源交叉说明是共识 —— 这也是「高置信自动加自选」敢开的前提。
W_MULTI_SOURCE = 6.0        # 每多一个独立来源
W_SOURCE_BASE = 2.0
W_HOT_INDUSTRY = 3.0        # 命中热门行业别名
W_NEW_LIST = 5.0            # 上市 <= fresh_days（新股天然高关注）
W_SUB_NEW = 2.0             # 上市 <= sub_new_days（次新股）
W_PRICE_SIGNAL = 0.0        # 由调用方按行情补
AUTO_SCORE = 12.0           # 自动进自选的分数线


def score_of(hit: dict) -> float:
    return (W_SOURCE_BASE * hit["n_sources"]
            + W_MULTI_SOURCE * (hit["n_sources"] - 1)
            + sum(h["weight"] * 0.5 for h in hit["evidence"])
            + (W_NEW_LIST if hit["is_new"] else 0)
            + (W_SUB_NEW if hit["is_sub_new"] and not hit["is_new"] else 0)
            + W_HOT_INDUSTRY * hit["hot_industry_hits"]
            + hit["price_signal"])


def mine(deps: dict, hours: int | None = None, limit_per_source: int = 600) -> dict:
    """跑一轮挖掘。返回统计摘要。"""
    from stock_roster import load_name_index, refresh_roster
    conf = deps.get("conf") or {}
    get_conn = deps["get_conn"]
    hours = int(hours or conf.get("hours", 72))
    fresh_days = int(conf.get("fresh_days", 20))
    sub_new_days = int(conf.get("sub_new_days", 90))
    since = datetime.now() - timedelta(hours=hours)

    rr = refresh_roster(deps)
    if not rr.get("ok"):
        return {"ok": False, "why": "名册刷新失败: %s" % rr.get("meta", {}).get("error"),
                "candidates": 0}
    roster_meta = {"got": rr.get("got"), "skipped": rr.get("skipped", False)}

    hits: dict[str, dict] = {}
    scanned = {}
    src_errors = {}

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SAVEPOINT sp_index")
        try:
            index = load_name_index(cur)
            cur.execute("RELEASE SAVEPOINT sp_index")
        except Exception as exc:      # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT sp_index")
            cur.execute("RELEASE SAVEPOINT sp_index")
            return {"ok": False, "why": "名册读取失败: %s" % str(exc)[:120],
                    "candidates": 0}
        if not index:
            return {"ok": False, "why": "名册为空，先跑一次名册刷新", "candidates": 0}

        cur.execute("SELECT code FROM sa_watchlist")
        watch = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT code FROM sa_discover_candidates "
                    "WHERE status = 'dismissed'")
        dismissed = {r[0] for r in cur.fetchall()}
        # 已加过的（历史候选）不必重复记，但仍更新分数与证据
        cur.execute("SELECT code, name, status FROM sa_discover_candidates")
        prior = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        cur.execute("SELECT DISTINCT industry FROM sa_stock_roster "
                    "WHERE stale = FALSE AND industry <> ''")
        hot_alias = {}
        for (ind,) in cur.fetchall():
            for a in HOT_INDUSTRY_ALIAS.get(ind, []):
                hot_alias.setdefault(a, ind)

        for src in SOURCES:
            if not src.get("enabled", True):
                continue
            if conf.get("only_sources") and src["key"] not in conf["only_sources"]:
                continue
            rows, err = _rows(cur, src, since)
            scanned[src["key"]] = len(rows)
            if err:
                src_errors[src["key"]] = err
            cols_src = _src_cols(src)
            for row in rows[:limit_per_source]:
                row = dict(zip(cols_src, row))
                title = row.get(src["title"]) if src["title"] else None
                blob = " ".join(str(row.get(c) or "") for c in src["text"])
                norm = _norm((title or "") + " " + blob)
                if len(norm) < 20:
                    continue
                for name in _find_names(norm, index):
                    code = index[name]
                    if code in watch or code in dismissed:
                        continue
                    h = hits.setdefault(code, {
                        "code": code, "matched": [], "evidence": [],
                        "sources": {}, "hot_industry_hits": 0,
                        "price_signal": 0.0, "is_new": False,
                        "is_sub_new": False, "list_date": None, "industry": ""})
                    if src["key"] in h["sources"]:
                        continue          # 同一源内重复提及只算一次
                    h["sources"][src["key"]] = src["weight"]
                    h["matched"].append(name)
                    h["evidence"].append({
                        "source": src["label"], "source_key": src["key"],
                        "weight": src["weight"],
                        "name": name,
                        "title": (title or "")[:80],
                        "snippet": _snippet(norm, name),
                        "extra": row.get(src["extra"]) if src["extra"] else None,
                        "ts": str(row.get(src["ts"]))[:19] if src.get("ts") else None,
                    })

        # 名册侧信号：上市天数 + 热门行业别名
        codes = list(hits)
        if codes:
            cur.execute("SELECT code, name, list_date, industry FROM sa_stock_roster "
                        "WHERE code = ANY(%s)", (codes,))
            info = {r[0]: r for r in cur.fetchall()}
        else:
            info = {}
        today = datetime.now().date()
        for code, h in hits.items():
            row = info.get(code)
            if not row:
                continue
            _, name, list_date, industry = row
            h["name"] = name
            h["list_date"] = str(list_date)[:10] if list_date else None
            h["industry"] = industry or ""
            if list_date:
                try:
                    d0 = datetime.strptime(h["list_date"], "%Y-%m-%d").date()
                    days = max(0, (today - d0).days)
                except ValueError:
                    days = None
                if days is not None:
                    h["listed_days"] = days
                    h["is_new"] = days <= fresh_days
                    h["is_sub_new"] = days <= sub_new_days
            # 热门行业：证据正文里出现该行业的概念别名
            for e in h["evidence"]:
                for alias in HOT_INDUSTRY_ALIAS.get(h["industry"], []):
                    if _norm(alias) in _norm(e.get("snippet", "")):
                        h["hot_industry_hits"] += 1
                        break
            h["n_sources"] = len(h["sources"])
            h["score"] = round(score_of(h), 2)

        # 行情加分（涨幅异动）：涨幅越大越可能是「有人在炒的新面孔」
        if codes:
            try:
                q = deps.get("quote_fn") and deps["quote_fn"](codes)
            except Exception:
                q = {}
            for code, h in hits.items():
                pct = (q or {}).get(code, {}).get("pct")
                if isinstance(pct, (int, float)):
                    h["pct"] = pct
                    # 只对「已经因别的原因进了候选」的加分，避免纯涨幅刷屏
                    h["price_signal"] = round(min(4.0, max(0.0, pct) / 5.0), 2)
                    h["score"] = round(score_of(h), 2)

        # 落库
        now = datetime.now()
        for code, h in hits.items():
            ev = json.dumps(h["evidence"][:8], ensure_ascii=False)
            sig = json.dumps(sorted(h["sources"]), ensure_ascii=False)
            cur.execute(
                """INSERT INTO sa_discover_candidates
                   (code, name, industry, list_date, listed_days, score,
                    n_sources, sources, evidence, is_new, is_sub_new, pct,
                    first_seen, last_seen)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (code) DO UPDATE SET
                     name = EXCLUDED.name, industry = EXCLUDED.industry,
                     list_date = EXCLUDED.list_date,
                     listed_days = EXCLUDED.listed_days,
                     score = GREATEST(sa_discover_candidates.score, EXCLUDED.score),
                     n_sources = GREATEST(sa_discover_candidates.n_sources,
                                          EXCLUDED.n_sources),
                     sources = EXCLUDED.sources, evidence = EXCLUDED.evidence,
                     is_new = EXCLUDED.is_new, is_sub_new = EXCLUDED.is_sub_new,
                     pct = EXCLUDED.pct, last_seen = EXCLUDED.last_seen""",
                (code, h.get("name", ""), h.get("industry", ""), h.get("list_date"),
                 h.get("listed_days"), h.get("score", 0), h.get("n_sources", 0),
                 sig, ev, h.get("is_new", False), h.get("is_sub_new", False),
                 h.get("pct"), now, now))
        conn.commit()

    return {"ok": True, "roster": roster_meta, "scanned": scanned,
            "source_errors": src_errors,
            "candidates": len(hits), "hours": hours}


# ---------------- 自动加自选 ----------------

def auto_add(deps: dict, dry_run: bool = False) -> dict:
    """把高置信候选加进自选股。

    门槛（可配 paper.discover.auto_add_score）：
      ① 分数 >= 阈值
      ② **至少 2 个独立来源**都提到 —— 单源高分可能只是某个人的观点，
         多源交叉才说明是共识。这条是硬条件，不能靠分数覆盖。
      ③ 没在自选股、没被忽略
    """
    conf = deps.get("conf") or {}
    get_conn = deps["get_conn"]
    if not conf.get("auto_add", True):
        return {"ok": True, "added": 0, "skipped": True,
                "why": "auto_add 已关闭"}
    threshold = float(conf.get("auto_add_score", AUTO_SCORE))
    min_sources = int(conf.get("auto_add_min_sources", 2))
    note = conf.get("auto_add_note", "自动挖掘")

    added, kept = [], []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT code FROM sa_watchlist")
        watch = {r[0] for r in cur.fetchall()}
        cur.execute(
            """SELECT code, name, score, n_sources, sources, is_new, is_sub_new,
                      listed_days, industry
               FROM sa_discover_candidates
               WHERE status = 'new' AND code <> ALL(%s)
               ORDER BY score DESC""", (list(watch),))
        rows = cur.fetchall()

        for code, name, score, nsrc, sources, is_new, is_sub_new, ldays, ind in rows:
            srcs = _as_list(sources)
            reasons = []
            if float(score or 0) < threshold:
                reasons.append("分数 %.1f < %.0f" % (float(score or 0), threshold))
            if nsrc < min_sources:
                reasons.append("仅 %d 个来源 < %d" % (nsrc, min_sources))
            if code in watch:
                reasons.append("已在自选股")
            if reasons:
                kept.append({"code": code, "name": name, "score": float(score or 0),
                             "n_sources": nsrc, "why": "；".join(reasons)})
                continue
            entry = {"code": code, "name": name, "score": float(score or 0),
                     "n_sources": nsrc, "sources": srcs,
                     "is_new": bool(is_new), "is_sub_new": bool(is_sub_new),
                     "listed_days": ldays, "industry": ind}
            added.append(entry)
            if dry_run:
                continue
            cur.execute(
                """INSERT INTO sa_watchlist (code, name, note)
                   VALUES (%s,%s,%s) ON CONFLICT (code) DO NOTHING""",
                (code, name, note))
            cur.execute(
                """UPDATE sa_discover_candidates
                   SET status='promoted', decided_at=now(), note=%s WHERE code=%s""",
                ("自动加自选：%s" % "、".join(srcs), code))
        if not dry_run:
            conn.commit()
    return {"ok": True, "added": len(added), "items": added,
            "kept_back": len(kept), "keep_examples": kept[:5],
            "dry_run": dry_run, "threshold": threshold,
            "min_sources": min_sources}


def _as_list(v) -> list:
    """JSONB 列 psycopg2 会自动反序列化成 list/dict；但某些驱动配置下仍是 str。

    两种都遇到过，所以这里统一处理 —— 直接 json.loads 会在已是 list 时抛
    TypeError: the JSON object must be str, bytes or bytearray, not list。
    """
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except (ValueError, TypeError):
            return []
    return []


def list_candidates(deps: dict, status: str = "new", limit: int = 200) -> list[dict]:
    conf = deps.get("conf") or {}
    get_conn = deps["get_conn"]
    only_new = conf.get("new_days", 30)
    sql = ("SELECT code, name, industry, list_date, listed_days, score, n_sources, "
           "sources, evidence, is_new, is_sub_new, pct, status, note, "
           "first_seen, last_seen, decided_at "
           "FROM sa_discover_candidates WHERE 1=1")
    args: list = []
    if status and status != "all":
        sql += " AND status = %s"
        args.append(status)
    if status == "new" and only_new > 0:
        sql += " AND last_seen >= %s"
        args.append(datetime.now() - timedelta(days=only_new))
    sql += " ORDER BY score DESC, listed_days ASC NULLS LAST LIMIT %s"
    args.append(int(limit))
    out = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["sources"] = _as_list(d.get("sources"))
            d["evidence"] = _as_list(d.get("evidence"))
            out.append(d)
    return out


def decide(deps: dict, code: str, action: str, note: str = "") -> dict:
    """人工裁决：promote（加自选）/ dismiss（忽略）/ reset（放回候选池）。"""
    get_conn = deps["get_conn"]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM sa_discover_candidates WHERE code=%s", (code,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "why": "候选不存在"}
        if action == "promote":
            cur.execute("INSERT INTO sa_watchlist (code, name, note) "
                        "VALUES (%s,%s,%s) ON CONFLICT (code) DO NOTHING",
                        (code, row[0], note or "人工审阅加入"))
            st = "promoted"
        elif action == "dismiss":
            st = "dismissed"
        elif action == "reset":
            st = "new"
        else:
            return {"ok": False, "why": "action 应为 promote/dismiss/reset"}
        cur.execute("UPDATE sa_discover_candidates SET status=%s, decided_at=now(), "
                    "note=COALESCE(NULLIF(%s,''), note) WHERE code=%s",
                    (st, note, code))
    return {"ok": True, "code": code, "status": st}
