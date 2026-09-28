"""微信公众号文章监控（wechat_mp.py）——拉 RSS 源，去重入库，新文推微信。

## 为什么是「拉 RSS」而不是自己爬

微信没有任何官方接口能读别人公众号的文章（官方 API 只管自己号），所以现成方案
都是「一个常驻服务负责爬，本项目只负责订阅它的产物」。本模块刻意只依赖**标准
RSS/Atom/JSON 源**，因此它对上游是谁毫不在意：

| 上游 | 接法 |
|---|---|
| **WeRSS**（`rachelos/we-mp-rss`，推荐，见下） | kind=`werss`，url 填 `http://werss:8001`，feed_id 填 `all` 或某个号 |
| wewe-rss（已归档，仅存量用户） | kind=`rss`，url 填 `http://…:4000/feeds/all.rss` |
| 任何别的 RSS | kind=`rss`，url 填完整订阅地址 |

## WeRSS 选型理由（2026-09-26 调研）

- `rachelos/we-mp-rss`：MIT + 单容器（Python/SQLite）+ 2026-08/09 仍在提交，
  4.7k star。路由实测（读 apis/rss.py）：
  `GET /rss`、`GET /rss/fresh`、`GET /rss/{feed_id}`（`ext=xml|json|md|txt`、
  `limit` ≤100）、`GET /rss/{feed_id}/fresh`、`GET /rss/{feed_id}/api`。
  这几个 RSS 路由上的 `Depends(verify_rss_access)` 都被注释掉了，即**无需鉴权**
  直接拉（源码里留了注释：真要放开请只允许内网）。
- `cooderl/wewe-rss`：2026-05-11 归档只读，且部分接口要经作者的中转服务
  `weread.111965.xyz`，不再作为首选。

## 本模块的表

| 表 | 内容 |
|---|---|
| `sa_mp_sources` | 订阅源（名称/类型/地址/feed_id/开关/上轮状态） |
| `sa_mp_articles` | 文章（guid 按源去重、标题/链接/作者/摘要/公众号名/发布时间/未读） |

本模块不 import app（避免循环依赖）：`get_conn` / `notify_fn` 由 app.py 注入。
"""

import json
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_MP_CONF = {
    "enabled": True,
    "interval_minutes": 30,   # 拉取周期（分钟），0 = 关闭
    "notify_new": True,       # 有新文章推微信
    "max_items": 50,          # 每源每轮最多取多少条
    "keep_days": 120,         # 文章保留天数，超期清理
    "timeout": 25,
    # ---- 远程独立部署用（一次配好，网页添加源时自动带入）----
    # WeRSS 服务的地址，如 http://192.168.1.10:8001 或 https://mp.example.com
    "base_url": "",
    # 远程加了鉴权时填，原样放进请求头：Basic <base64> / Bearer <token> 都支持。
    # 存云库、接口只回掩码（同 X cookie 的处理）。
    "auth": "",
    # 是否走 /rss/{id}/fresh（先让 WeRSS 去上游抓一轮再返回）。
    # 远程共享实例建议 false：走 WeRSS 自己的缓存，不反复催它爬——
    # 上游明确提示「添加订阅频率过高容易被封控」，而本项目默认 30 分钟一轮。
    "use_fresh": False,
}

# 摘要字段：刻意**不含** content/正文——WeRSS 的 content 是完整 HTML 全文，
# 存进库会把每行撑到几十 KB，而页面和推送都只显示前两百来字。
_DESC_KEYS = ("description", "summary", "digest", "abstract", "excerpt", "sub_title")
_TITLE_KEYS = ("title", "name", "subject")
_URL_KEYS = ("url", "link", "origLink", "source_url", "guid", "href", "permalink")
_DATE_KEYS = ("published_at", "pubDate", "published", "pub_ts", "posted_at",
              "date", "created_at", "updated", "dc:date")
_MPNAME_KEYS = ("mp_name", "mpName", "source", "account_name", "author_name", "author")

_state_lock = threading.Lock()
_state = {"fetching": False, "last_run": None, "last_result": None}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/rss+xml, application/atom+xml, application/json;q=0.9, */*"}


# ---------------- 配置 ----------------

def load_mp_conf() -> dict:
    """读 config.yaml 的 mp 段，补齐默认（每轮现读，改配置即生效）。"""
    conf = dict(DEFAULT_MP_CONF)
    try:
        import yaml
        data = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        conf.update({k: v for k, v in (data.get("mp") or {}).items() if v is not None})
    except Exception:
        pass
    return conf


# ---------------- 抓取 ----------------

def _get(url: str, timeout: int = 25, auth: str = "") -> str:
    import requests
    sess = requests.Session()
    sess.trust_env = False      # 与 app.py 的 NO_PROXY=* 同策：绕开 WinINET 代理
    headers = dict(HEADERS)
    if auth:
        headers["Authorization"] = auth      # 远程实例加了反代鉴权时用
    resp = sess.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def build_url(src: dict, conf: dict) -> str:
    """订阅源 → 实际请求地址。"""
    limit = max(1, min(int(conf.get("max_items") or 50), 100))
    if (src.get("kind") or "rss").lower() != "werss":
        return (src.get("url") or "").strip()
    base = (src.get("url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("WeRSS 类型必须填服务地址（可先在 config.yaml 的 mp.base_url 里"
                         "配一次，网页添加源时会自动带入）")
    fid = (src.get("feed_id") or "all").strip() or "all"
    # /fresh 会先让 WeRSS 去上游抓一轮再返回；不加就只能吃它的缓存
    # （缓存默认一天两次）。远程共享实例通常用缓存，见 DEFAULT_MP_CONF.use_fresh。
    fresh = src.get("use_fresh")
    if fresh is None:
        fresh = conf.get("use_fresh", False)
    tail = "/fresh" if fresh else ""
    return f"{base}/rss/{fid}{tail}?limit={limit}"


# ---------------- 解析：JSON / RSS 2.0 / Atom / RSS 1.0 ----------------

def _first(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _nested_str(d: dict, keys) -> str:
    """从 value 是 dict 的候选键里取值（WeRSS 把公众号名塞在 feed/account_meta 里）。"""
    v = _first(d, keys)
    if v:
        return v
    for k in ("feed", "account_meta", "author", "mp"):
        sub = d.get(k)
        if isinstance(sub, dict):
            got = _first(sub, keys)
            if got:
                return got
    return ""


def _mp_name_of(d: dict) -> str:
    """取「公众号名」。顶层 mp_name → feed/account_meta 里的 name/mp_name。

    与 _nested_str 分开是有意的：公众号名在 WeRSS JSON 里叫 `feed.name`，
    而裸 `name` 在很多源里是**别的**含义（作者名、甚至分类名）。所以裸 `name`
    只在 feed/account_meta 这两个确定是「号」的子对象里才认。
    """
    v = _first(d, _MPNAME_KEYS)
    if v:
        return v
    for k in ("feed", "account_meta", "mp"):
        sub = d.get(k)
        if isinstance(sub, dict):
            got = _first(sub, ("mp_name", "mpName", "name", "account_name", "title"))
            if got:
                return got
    return ""


def _parse_dt(raw) -> datetime | None:
    """RSS 的 RFC822 / Atom 的 ISO8601 / 时间戳 / WeRSS 的 ISO 串，统统归一到 aware。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        ts = int(raw)
        if ts > 1e12:      # 毫秒
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(raw).strip()
    try:
        dt = parsedate_to_datetime(s)          # RFC822: 'Sat, 26 Sep 2026 09:00:00 +0800'
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M",
                "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _norm(raw_title: str, raw_desc: str) -> tuple[str, str]:
    """标题/摘要里的 HTML 剥掉、空白压平、长度封顶。"""
    def clean(t: str) -> str:
        t = re.sub(r"<[^>]+>", " ", t or "")
        t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
              .replace("&#39;", "'"))
        return re.sub(r"\s+", " ", t).strip()
    return clean(raw_title)[:300], clean(raw_desc)[:600]


def _collect_json(node, out: list) -> None:
    """深度优先找「一堆含 url+title 的 dict」，顺序即源里的顺序。

    为什么递归而不是按固定键取：wewe-rss 是 `{"data":[…]}`，WeRSS 的 JSON 形态
    随版本变过（`{"data":{"items":[…]}}` / 顶层数组），写死任一种都会在对方升级后
    静默变成「0 条文章」——不报错、页面全空，最难查。
    """
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            if any(_nested_str(x, _URL_KEYS) for x in node):
                out.extend(node)
                return
        for x in node:
            _collect_json(x, out)
    elif isinstance(node, dict):
        for v in node.values():
            _collect_json(v, out)


def _items_from_json(data) -> list[dict]:
    raw: list[dict] = []
    _collect_json(data, raw)
    out = []
    for it in raw:
        url = _nested_str(it, _URL_KEYS)
        title = _nested_str(it, _TITLE_KEYS)
        if not url or not title:
            continue
        desc = _first(it, _DESC_KEYS)
        title, desc = _norm(title, desc)
        out.append({
            "title": title, "url": url, "summary": desc,
            "author": _nested_str(it, ("author", "author_name")) or _mp_name_of(it),
            "mp_name": _mp_name_of(it),
            "published_at": _parse_dt(_first(it, _DATE_KEYS)),
            "guid": _first(it, ("guid", "id", "mid")) or url,
        })
    return out


def _lname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _items_from_xml(root) -> list[dict]:
    """RSS 2.0 (channel/item) / RSS 1.0 (rdf:RDF/item) / Atom (feed/entry) 一次认全。"""
    items: list = []
    for el in root.iter():
        if _lname(el.tag) in ("item", "entry"):
            items.append(el)
    out = []
    for el in items:
        title = desc = link = guid = author = mp_name = ""
        date_raw = ""
        for ch in el.iter():
            name = _lname(ch.tag)
            text = (ch.text or "").strip()
            if name == "title" and not title:
                title = text
            elif name in ("description", "summary", "content", "encoded") and not desc:
                desc = text
            elif name == "link":
                # Atom 的 link 是属性 href；RSS 2.0 的 link 是文本
                cand = (ch.get("href") or text or "").strip()
                rel = (ch.get("rel") or "alternate")
                if cand and rel == "alternate" and not link:
                    link = cand
            elif name == "guid" and not guid:
                guid = text
            elif name in ("author", "dc:creator", "creator") and not author:
                # Atom 的 author 是子元素 <name>，不是文本
                nm = next((g for g in ch.iter() if _lname(g.tag) == "name"), None)
                author = ((nm.text or "").strip() if nm is not None else text)
            elif name in ("pubdate", "published", "updated", "date") and not date_raw:
                date_raw = text
            elif name in ("source", "mp_name") and not mp_name:
                mp_name = text
            elif name in ("author", "dc:creator") and not mp_name:
                mp_name = author
        if not link:
            # 再兜一次：任何 <link> 属性 href
            for ch in el.iter():
                if _lname(ch.tag) == "link" and ch.get("href"):
                    link = ch.get("href").strip()
                    break
        if not link or not title:
            continue
        title, desc = _norm(title, desc)
        out.append({
            "title": title, "url": link, "summary": desc,
            "author": author or mp_name, "mp_name": mp_name,
            "published_at": _parse_dt(date_raw),
            "guid": guid or link,
        })
    return out


def parse_feed(raw: str) -> list[dict]:
    """一段响应文本 → 文章列表。认不出格式返回 []（调用方记进 last_error）。"""
    text = (raw or "").lstrip()
    if not text:
        return []
    if text[0] in "{[":
        try:
            return _items_from_json(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            pass
    blob = text.encode("utf-8", "replace")
    try:
        return _items_from_xml(ET.fromstring(blob))
    except ET.ParseError as exc:
        raise ValueError(f"不是可识别的 RSS/Atom/JSON：{exc}") from exc


# ---------------- 源与文章的读写 ----------------

def _ensure_tables(deps) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_mp_sources (
                id          BIGSERIAL PRIMARY KEY,
                name        VARCHAR(64) NOT NULL UNIQUE,
                kind        VARCHAR(8)  NOT NULL DEFAULT 'rss',   -- werss | rss
                url         TEXT        NOT NULL,
                feed_id     VARCHAR(64) NOT NULL DEFAULT 'all',
                enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
                note        TEXT        NOT NULL DEFAULT '',
                -- NULL = 跟随 config.yaml 的 mp.use_fresh；TRUE/FALSE = 本源覆盖。
                -- 为什么允许逐源覆盖：「大多数源吃缓存、个别急用的源走 fresh」
                -- 在远程共享实例上是合理的搭配。
                use_fresh   BOOLEAN,
                -- 本源的 Authorization 头（远程加反代鉴权时用），NULL = 用全局 mp.auth
                auth        TEXT,
                last_run    TIMESTAMPTZ,
                last_status VARCHAR(16) NOT NULL DEFAULT '',
                last_count  INTEGER     NOT NULL DEFAULT 0,
                last_error  TEXT        NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        # 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）
        cur.execute("ALTER TABLE sa_mp_sources ADD COLUMN IF NOT EXISTS "
                    "use_fresh BOOLEAN")
        cur.execute("ALTER TABLE sa_mp_sources ADD COLUMN IF NOT EXISTS auth TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_mp_articles (
                id           BIGSERIAL PRIMARY KEY,
                source_id    BIGINT      NOT NULL,
                guid         VARCHAR(512) NOT NULL,
                title        TEXT        NOT NULL,
                url          TEXT        NOT NULL,
                author       TEXT        NOT NULL DEFAULT '',
                mp_name      TEXT        NOT NULL DEFAULT '',
                summary      TEXT        NOT NULL DEFAULT '',
                published_at TIMESTAMPTZ,
                fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                is_read      BOOLEAN     NOT NULL DEFAULT FALSE,
                UNIQUE (source_id, guid)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_mp_articles_pub "
                    "ON sa_mp_articles (published_at DESC NULLS LAST, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_mp_articles_src "
                    "ON sa_mp_articles (source_id, id DESC)")


def list_sources(deps) -> list[dict]:
    conf = load_mp_conf()
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, kind, url, feed_id, enabled, note, use_fresh, "
                    "last_run, last_status, last_count, last_error, created_at "
                    "FROM sa_mp_sources ORDER BY id")
        rows = cur.fetchall()
    counts = _unread_map(deps)
    out = []
    for r in rows:
        src_auth = r[8]
        out.append({
            "id": r[0], "name": r[1], "kind": r[2], "url": r[3], "feed_id": r[4],
            "enabled": r[5], "note": r[6],
            "use_fresh": r[7],
            "use_fresh_effective": bool(r[7]) if r[7] is not None
                                    else bool(conf.get("use_fresh")),
            # 只回掩码，不吐原文（与 X cookie 同一套处理）
            "has_auth": bool(src_auth or conf.get("auth")),
            "auth_masked": _mask_auth(src_auth or conf.get("auth") or ""),
            "last_run": r[9].isoformat(timespec="seconds") if r[9] else None,
            "last_status": r[10], "last_count": r[11], "last_error": r[12],
            "created_at": r[13].isoformat(timespec="seconds") if r[13] else None,
            "unread": counts.get(r[0], 0),
        })
    return out


def _mask_auth(auth: str) -> str:
    """Authorization 头打码：只留前缀与末尾 4 字符。"""
    if not auth:
        return ""
    head, _, tail = auth.partition(" ")
    return f"{head} …{tail[-4:]}" if tail else f"{head} …"


def _effective_auth(src: dict, conf: dict) -> str:
    """本源自己的 auth 优先，否则用全局 mp.auth。"""
    return (src.get("auth") or "").strip() or (conf.get("auth") or "").strip()


def _unread_map(deps) -> dict[int, int]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_id, COUNT(*) FROM sa_mp_articles "
                    "WHERE NOT is_read GROUP BY source_id")
        return {r[0]: r[1] for r in cur.fetchall()}


def add_source(deps, name: str, url: str, kind: str = "rss",
               feed_id: str = "all", note: str = "", use_fresh: bool | None = None,
               auth: str | None = None) -> dict:
    name = (name or "").strip()
    url = (url or "").strip()
    kind = (kind or "rss").strip().lower()
    conf = load_mp_conf()
    if not name:
        raise ValueError("请填写订阅源名称")
    if not url:
        # 没填地址就回落到全局 base_url：远程实例地址在 config.yaml 里配一次，
        # 网页添加源时不必重复手敲
        url = (conf.get("base_url") or "").strip()
        if not url:
            raise ValueError("请填写地址（WeRSS 填服务地址如 http://192.168.1.10:8001，"
                             "其他 RSS 填完整订阅地址；也可以在 config.yaml 的 "
                             "mp.base_url 里预设一次）")
    if kind not in ("rss", "werss"):
        raise ValueError("kind 只能是 rss 或 werss")
    if kind == "rss" and not re.match(r"^https?://", url):
        raise ValueError("RSS 地址需以 http:// 或 https:// 开头")
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO sa_mp_sources
                           (name, kind, url, feed_id, note, use_fresh, auth)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (name) DO UPDATE SET
                           kind=EXCLUDED.kind, url=EXCLUDED.url,
                           feed_id=EXCLUDED.feed_id, note=EXCLUDED.note,
                           use_fresh=EXCLUDED.use_fresh, auth=EXCLUDED.auth
                       RETURNING id""",
                    (name, kind, url.rstrip("/"), (feed_id or "all").strip(),
                     note or "", use_fresh, (auth or "").strip() or None))
        sid = cur.fetchone()[0]
    return {"id": sid, "name": name, "kind": kind, "url": url.rstrip("/"),
            "feed_id": feed_id, "use_fresh": use_fresh,
            "has_auth": bool((auth or "").strip() or conf.get("auth"))}


def update_source(deps, sid: int, **fields) -> dict:
    allowed = {"name", "kind", "url", "feed_id", "enabled", "note", "use_fresh", "auth"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"不支持字段 {k}")
        if k == "use_fresh" and v is None:
            sets.append("use_fresh = NULL")   # 交回全局
            continue
        if k == "auth":
            v = (v or "").strip() or None      # 空 = 用全局 mp.auth
        if k == "url":
            v = (v or "").strip().rstrip("/")
        sets.append(f"{k}=%s")
        vals.append(v)
    if not sets:
        raise ValueError("没有要改的字段")
    vals.append(sid)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE sa_mp_sources SET {','.join(sets)} WHERE id=%s "
                    f"RETURNING id, name, kind, url, feed_id, enabled, note, use_fresh",
                    vals)
        row = cur.fetchone()
    if not row:
        raise ValueError(f"订阅源 {sid} 不存在")
    return {"id": row[0], "name": row[1], "kind": row[2], "url": row[3],
            "feed_id": row[4], "enabled": row[5], "note": row[6],
            "use_fresh": row[7]}


def remove_source(deps, sid: int) -> dict:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_mp_articles WHERE source_id = %s", (sid,))
        n = cur.rowcount
        cur.execute("DELETE FROM sa_mp_sources WHERE id = %s RETURNING name", (sid,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"订阅源 {sid} 不存在")
    return {"ok": True, "name": row[0], "removed_articles": n}


def list_articles(deps, source_id: int | None = None, unread_only: bool = False,
                  limit: int = 50) -> list[dict]:
    limit = max(1, min(limit, 200))
    where, params = [], []
    if source_id:
        where.append("a.source_id = %s")
        params.append(source_id)
    if unread_only:
        where.append("NOT a.is_read")
    sql = ("SELECT a.id, a.source_id, a.title, a.url, a.author, a.mp_name, a.summary, "
           "a.published_at, a.fetched_at, a.is_read, s.name "
           "FROM sa_mp_articles a JOIN sa_mp_sources s ON s.id = a.source_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY COALESCE(a.published_at, a.fetched_at) DESC NULLS LAST, a.id DESC "
            "LIMIT %s")
    params.append(limit)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "source_id": r[1], "title": r[2], "url": r[3],
            "author": r[4], "mp_name": r[5], "summary": r[6],
            "published_at": r[7].isoformat(timespec="minutes") if r[7] else None,
            "fetched_at": r[8].isoformat(timespec="seconds") if r[8] else None,
            "is_read": r[9], "source_name": r[10],
        })
    return out


def unread_count(deps) -> int:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sa_mp_articles WHERE NOT is_read")
        return cur.fetchone()[0]


def mark_read(deps, ids: list[int] | None = None, all_: bool = False) -> int:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        if all_:
            cur.execute("UPDATE sa_mp_articles SET is_read = TRUE WHERE NOT is_read")
        elif ids:
            cur.execute("UPDATE sa_mp_articles SET is_read = TRUE "
                        "WHERE id = ANY(%s) AND NOT is_read", (ids,))
        else:
            return 0
        return cur.rowcount


# ---------------- 一轮拉取 ----------------

def _fetch_source(deps, src: dict, conf: dict) -> dict:
    result = {"source_id": src["id"], "name": src["name"], "new": 0,
              "total": 0, "fresh": []}
    try:
        url = build_url(src, conf)
    except ValueError as exc:
        result.update(status="error", error=str(exc))
        return result
    result["url"] = url
    try:
        items = parse_feed(_get(url, int(conf.get("timeout") or 25),
                                _effective_auth(src, conf)))
    except Exception as exc:
        result.update(status="error", error=str(exc)[:500])
        return result
    result["total"] = len(items)
    if not items:
        result.update(status="empty",
                      error="源可达但没解析出文章（确认上游已订阅公众号，"
                            "或地址不是 RSS/Atom/JSON）")
        return result
    now = datetime.now(timezone.utc)
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        for it in items:
            cur.execute(
                """
                INSERT INTO sa_mp_articles
                    (source_id, guid, title, url, author, mp_name, summary, published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_id, guid) DO NOTHING
                """,
                (src["id"], it["guid"][:512], it["title"], it["url"],
                 it.get("author", "") or "", it.get("mp_name", "") or src["name"],
                 it.get("summary", ""), it.get("published_at") or now))
            if cur.rowcount:            # rowcount=1 才是真插入（冲突时为 0）
                result["new"] += 1
                result["fresh"].append(it)
    result["status"] = "ok"
    return result


def run_once(deps) -> dict:
    """一轮：逐个启用的源拉取 → 去重入库 → 新文合并推一条微信。"""
    conf = load_mp_conf()
    with _state_lock:
        if _state["fetching"]:
            return {"skipped": True, "reason": "已有拉取任务在运行"}
        _state["fetching"] = True
    try:
        with deps["get_conn"]() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, kind, url, feed_id, enabled "
                        "FROM sa_mp_sources WHERE enabled ORDER BY id")
            cols = ("id", "name", "kind", "url", "feed_id", "enabled")
            sources = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not sources:
            return {"skipped": True, "reason": "没有启用的订阅源", "items": []}
        items = []
        for src in sources:
            r = _fetch_source(deps, src, conf)
            items.append(r)
            with deps["get_conn"]() as conn, conn.cursor() as cur:
                cur.execute("UPDATE sa_mp_sources SET last_run=now(), last_status=%s, "
                            "last_count=%s, last_error=%s WHERE id=%s",
                            (r.get("status", ""), r.get("total", 0),
                             r.get("error", "")[:900], src["id"]))
        _cleanup(deps, conf)
        fresh = [i for it in items for i in it.get("fresh", [])]
        pushed = 0
        if fresh and conf.get("notify_new", True):
            try:
                deps["notify_fn"](_notify_title(len(fresh)),
                                  _notify_body(fresh))
                pushed = 1
            except Exception as exc:
                print(f"[mp] notify failed: {exc}", flush=True)
        result = {"items": items, "new": sum(i["new"] for i in items),
                  "pushed": pushed, "ts": datetime.now().isoformat(timespec="seconds")}
        _state["last_result"] = result
        return result
    finally:
        _state["fetching"] = False
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")


def _cleanup(deps, conf: dict) -> None:
    keep = int(conf.get("keep_days") or 120)
    if keep <= 0:
        return
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sa_mp_articles "
                    "WHERE fetched_at < now() - make_interval(days => %s)", (keep,))


def _notify_title(n: int) -> str:
    return f"📰 公众号新文章 {n} 篇" if n > 1 else "📰 公众号新文章"


def _notify_body(fresh: list[dict]) -> str:
    by_src: dict[str, list[dict]] = {}
    for f in fresh:
        by_src.setdefault(f.get("mp_name") or "未署名", []).append(f)
    lines = []
    for name, items in by_src.items():
        lines.append(f"【{name}】")
        for it in items[:5]:
            ts = it["published_at"].astimezone().strftime("%m-%d %H:%M") \
                if it.get("published_at") else ""
            lines.append(f"- {it['title']}" + (f"（{ts}）" if ts else ""))
        if len(items) > 5:
            lines.append(f"- …另有 {len(items) - 5} 篇")
    lines.append("\n（打开 stock-advisor「📰 公众号」页看全文列表）")
    return "\n".join(lines)


def get_status(deps) -> dict:
    return {"fetching": _state["fetching"], "last_run": _state["last_run"],
            "last_result": _state["last_result"],
            "sources": sum(1 for s in list_sources(deps) if s["enabled"]),
            "unread": unread_count(deps)}


def digest_lines(deps, hours: int = 36) -> list[str]:
    """近 N 小时新文章的一行摘要，给盘前/盘后报告引用。无数据返回空。"""
    try:
        rows = list_articles(deps, limit=40)
    except Exception:
        return []
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for r in rows:
        ts = r.get("published_at")
        dt = datetime.fromisoformat(ts).astimezone(timezone.utc) if ts else None
        if dt and dt < since:
            continue
        mark = "🔴 " if not r["is_read"] else ""
        out.append(f"- {mark}{r['title']}（{r['source_name']}"
                   + (f"，{dt.astimezone().strftime('%m-%d %H:%M')}" if dt else "") + "）")
        if len(out) >= 25:
            break
    return out
