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
from html import unescape as _html_unescape


# 块级标签换行用的（正文转纯文本时保留段落结构）
_BLOCK = re.compile(
    r"</\s*(p|div|section|article|li|tr|h[1-6]|blockquote|br|hr)\s*>|"
    r"<\s*br\s*/?\s*>", re.I)
_SCRIPT = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """HTML 正文 → 纯文本（只删标签，不引第三方库）。

    存两份的理由（2026-09-29）：content_html 保留原文，供存档与将来重新解析；
    content_text 供喂 LLM 与全文搜索 —— 实测单篇 HTML 最高 54,566 字符，
    剥完标签约 5,000 字符，小一个数量级。
    """
    if not html:
        return ""
    s = _SCRIPT.sub(" ", html)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub("", s)
    s = _html_unescape(s)
    s = re.sub(r"[ \t\u00a0\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()
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
    # ---- use_fresh 的限频（2026-09-29 加）----
    # 问题：use_fresh 与 interval_minutes 是耦合的。直接开 use_fresh 的话，
    # 30 分钟 × N 个源 = 每天上百次去微信抓，必然触发风控。
    # 实测 2026-09-29：4 个源里 3 个抓不到正文，微信侧 content:encoded
    # 直接是空的 —— 典型的被拦。
    # 办法：**把两个频率解耦** —— 照常每 interval_minutes 读一次（读的是
    # WeRSS 自己的缓存，几乎零成本），但「催 WeRSS 去微信抓」这个动作
    # 单独限频。两者代价差极大：
    #   读缓存 = 一次 HTTP GET，可以高频
    #   催抓取 = WeRSS 拉起 Chromium 访问微信，风控只按这个计数
    'fresh_interval_minutes': 180,  # 催抓的最小间隔（分钟）
    'fresh_jitter_minutes': 30,    # 抖动上限，避免整点撞车
    # 连续 N 轮没新文章就把间隔翻倍（退避）；一旦有新文章立刻恢复原频率。
    # 「没更新就别去催」—— 这是省配额的关键，不只是限频。
    'fresh_backoff_rounds': 3,
}

# 摘要字段：只取真正的**摘要**类字段。
# 原注记说「刻意不含 content/正文——WeRSS 的 content 是完整 HTML 全文，存进库会把
# 每行撑到几十 KB」，这在「只要标题做监控」的前提下成立。但用户要的是**存档全文**，
# 所以 2026-09-29 改成：正文单独存 content_html / content_text 两列，
# summary 仍只放摘要（页面、推送、喂 LLM 都读它，不碰正文列）。
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


def _should_go_fresh(src: dict, conf: dict, now=None) -> tuple[bool, str]:
    """决定这一轮要不要走 /fresh（催 WeRSS 去微信抓）。返回 (是否 fresh, 说明)。

    这是防风控的核心。直接开 use_fresh 的话，interval_minutes(30) × 源数
    = 每天上百次催抓，微信侧必然拦（2026-09-29 实测 3/4 的源正文为空）。

    三重限流，代价从低到高：
      1) 最小间隔 fresh_interval_minutes —— 硬闸，不足就不催
      2) 退避：连续 fresh_backoff_rounds 轮没有新文章，间隔 ×2^轮数
         （没更新就别去催；有更新嫌疑才值得花配额）
      3) 抖动 —— 在 [0, fresh_jitter_minutes] 内随机，避免固定节奏被识别
    last_fresh_at 落在库里而非内存：否则每次重启都会立刻催一次。
    """
    want = src.get("use_fresh")
    if want is None:
        want = conf.get("use_fresh", False)
    if not want:
        return False, ""

    now = now or datetime.now(timezone.utc)
    base = int(conf.get("fresh_interval_minutes") or 180)
    jit = int(conf.get("fresh_jitter_minutes") or 0)
    idle = int(src.get("idle_rounds") or 0)
    br = int(conf.get("fresh_backoff_rounds") or 0)
    # 退避倍数：连续 idle 轮没新文，每满 br 轮翻一倍，上限 8 倍（≈24 小时一次）
    mult = 1
    if br > 0 and idle >= br:
        mult = min(8, 2 ** ((idle // br)))
    need = base * mult

    last = src.get("last_fresh_at")
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds() / 60.0
        # 抖动取 [-jit, +jit] 的一半区间，保证不会因抖动而「必然够」
        eff = max(1.0, need - jit / 2.0)
        if elapsed < eff:
            left = int(need - elapsed)
            return False, (f"限频：距上次催抓 {int(elapsed)} 分 < "
                           f"需要 {need} 分（{left} 分后再催）"
                           + (f"，已退避 ×{mult}" if mult > 1 else ""))
    return True, ""


def _build_url_ex(src: dict, conf: dict, go_fresh: bool) -> str:
    """地址拼装（fresh 与否由调用方决定）。非 werss 源直接用自己的完整地址。"""
    limit = max(1, min(int(conf.get("max_items") or 50), 100))
    if (src.get("kind") or "rss").lower() != "werss":
        return (src.get("url") or "").strip()
    base = (src.get("url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("WeRSS 类型必须填服务地址（可先在 config.yaml 的 mp.base_url 里"
                         "配一次，网页添加源时会自动带入）")
    fid = (src.get("feed_id") or "all").strip() or "all"
    # /fresh 会先让 WeRSS 去上游抓一轮再返回；不加就只能吃它的缓存
    # （缓存默认一天两次）。是否真的走 /fresh 由 _should_go_fresh 决定 ——
    # 它把「读缓存」和「催抓微信」两个频率解耦，避免高频催抓被风控。
    tail = "/fresh" if go_fresh else ""
    return f"{base}/rss/{fid}{tail}?limit={limit}"


def build_url(src: dict, conf: dict) -> str:
    """订阅源 → 实际请求地址（自行判定要不要走 /fresh）。"""
    go_fresh, _why = _should_go_fresh(src, conf)
    return _build_url_ex(src, conf, go_fresh)


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
        # JSON 源的正文：WeRSS 的 ext=json 会把全文放在 content/content_html/
        # content_encoded/body 等字段。优先级从高到低。
        body = _nested_str(it, ("content_html", "content_encoded", "content",
                                "body", "html", "raw_content"))
        title, desc = _norm(title, desc)
        if not desc and body:
            desc = _norm(title, _html_to_text(body)[:400])[1]
        out.append({
            "title": title, "url": url, "summary": desc,
            "content_html": body,
            "content_text": _html_to_text(body) if body else "",
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
        body = ""          # content:encoded —— 全文原文（HTML）
        date_raw = ""
        for ch in el.iter():
            name = _lname(ch.tag)
            text = (ch.text or "").strip()
            if name == "title" and not title:
                title = text
            elif name in ("content", "encoded") and not body:
                # 2026-09-29 拆开：原来 content:encoded 和 description 抢同一个
                # desc 槽位，谁先到谁占。content:encoded 是**全文**（实测单篇
                # 最高 54,566 字符），description 是摘要；混在一起再被 _norm
                # 截断，等于把全文扔了。现在各存各的。
                body = text
            elif name in ("description", "summary") and not desc:
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
        # 摘要优先用 description；没有 description 时用全文截一段（很多源只给全文）
        if not desc and body:
            desc = _norm(title, _html_to_text(body)[:400])[1]
        out.append({
            "title": title, "url": link, "summary": desc,
            "content_html": body,
            "content_text": _html_to_text(body) if body else "",
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
        # last_fresh_at：上次**真的催过 WeRSS 去微信抓**的时间。
        # 必须落库而不是放内存 —— 放内存的话每次重启都会「上次从未 fresh 过」
        # 而立刻再催一次，反复重启就是持续的风控压力。
        cur.execute("ALTER TABLE sa_mp_sources ADD COLUMN IF NOT EXISTS "
                    "last_fresh_at TIMESTAMPTZ")
        # idle_rounds：连续多少轮没有新文章。用来退避（没更新就别去催）。
        cur.execute("ALTER TABLE sa_mp_sources ADD COLUMN IF NOT EXISTS "
                    "idle_rounds INTEGER NOT NULL DEFAULT 0")
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
        # content_html：公众号文章**全文原文**（WeRSS 的 content:encoded，HTML）。
        # 2026-09-29 加。原设计只留 summary，理由是「全文单条几十 KB，存库会撑爆」——
        # 这个顾虑在「只要标题做监控」时成立，但用户要的是存档全文。
        # 实测单篇最大 54,566 字符；按日均 30 篇/源 × 10 源估算约 15~20 MB/年，
        # PostgreSQL TOAST 会自动压缩外存，这个量级完全不是问题。
        # 摘要仍单独留一份，页面/推送/喂 LLM 都只读 summary，不碰这列。
        cur.execute("ALTER TABLE sa_mp_articles "
                    "ADD COLUMN IF NOT EXISTS content_html TEXT NOT NULL DEFAULT ''")
        # 纯文本版（剥掉 HTML 标签）：喂 LLM / 全文搜索用，比 HTML 小约 10 倍。
        # ext=md 出口实测单篇 ~5 KB，content:encoded HTML ~50 KB。
        cur.execute("ALTER TABLE sa_mp_articles "
                    "ADD COLUMN IF NOT EXISTS content_text TEXT NOT NULL DEFAULT ''")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_mp_articles_pub "
                    "ON sa_mp_articles (published_at DESC NULLS LAST, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_mp_articles_src "
                    "ON sa_mp_articles (source_id, id DESC)")
        # 全文检索用 GIN 触发器太重，这里只建一个表达式索引辅助 LIKE '关键词%' 场景，
        # 真正的正文检索交给 ILIKE 全表扫（存档量级下够用）。
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_mp_articles_title "
                    "ON sa_mp_articles (md5(title))")


def list_sources(deps) -> list[dict]:
    conf = load_mp_conf()
    import psycopg2.extras  # 延迟导入：本模块整体不依赖 psycopg2，只有
    with deps["get_conn"]() as conn, conn.cursor(          # 走 DB 的函数才需要
            cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 2026-09-29 改成按列名取值。原来是位置索引 r[0]~r[13]，而 SELECT 里
        # 漏了后加的 auth 列（它在 sa_mp_sources 里是第 14 位）—— 于是 r[8]
        # 拿到的是 last_run（datetime）而不是 auth，_mask_auth 里
        # auth.partition(" ") 抛 AttributeError，/api/mp/sources 与
        # /api/mp/status 一起 500。
        # 位置索引只要加一列就错位；按列名取则永远对得上。
        cur.execute("SELECT * FROM sa_mp_sources ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
    counts = _unread_map(deps)
    out = []
    for r in rows:
        src_auth = r.get("auth") or ""
        uf = r.get("use_fresh")
        lr, ca = r.get("last_run"), r.get("created_at")
        out.append({
            "id": r["id"], "name": r["name"], "kind": r["kind"], "url": r["url"],
            "feed_id": r.get("feed_id"), "enabled": r.get("enabled"),
            "note": r.get("note") or "",
            "use_fresh": uf,
            "use_fresh_effective": bool(uf) if uf is not None
                                    else bool(conf.get("use_fresh")),
            # 只回掩码，不吐原文（与 X cookie 同一套处理）
            "has_auth": bool(src_auth or conf.get("auth")),
            "auth_masked": _mask_auth(src_auth or conf.get("auth") or ""),
            "last_run": lr.isoformat(timespec="seconds") if lr else None,
            "last_status": r.get("last_status"),
            "last_count": r.get("last_count"),
            "last_error": r.get("last_error") or "",
            "created_at": ca.isoformat(timespec="seconds") if ca else None,
            "unread": counts.get(r["id"], 0),
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

def _fetch_source(deps, src: dict, conf: dict,
                  force_fresh: bool | None = None) -> dict:
    """拉一个源。force_fresh 由调用方（run_once）用 _should_go_fresh 判好后传入
    —— 避免在 build_url 内部再判一次：两处各判一次会因秒级时间差与抖动导致
    「判了要 fresh 却没走 /fresh」这种不一致。"""
    result = {"source_id": src["id"], "name": src["name"], "new": 0,
              "total": 0, "fresh": [], "backfilled": 0, "backfilled_titles": []}
    try:
        if force_fresh is None:
            url = build_url(src, conf)
        else:
            # 复用 build_url 的地址拼装，但用调用方已定的 force_fresh
            url = _build_url_ex(src, conf, bool(force_fresh))
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
            body = it.get("content_html", "") or ""
            body_txt = it.get("content_text", "") or ""
            cur.execute(
                """
                INSERT INTO sa_mp_articles
                    (source_id, guid, title, url, author, mp_name, summary,
                     published_at, content_html, content_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_id, guid) DO NOTHING
                """,
                (src["id"], it["guid"][:512], it["title"], it["url"],
                 it.get("author", "") or "", it.get("mp_name", "") or src["name"],
                 it.get("summary", ""), it.get("published_at") or now,
                 body, body_txt))
            if cur.rowcount:            # rowcount=1 才是真插入（冲突时为 0）
                result["new"] += 1
                result["fresh"].append(it)
                continue
            # ---- 冲突：已存在。补正文（2026-09-29 加）----
            # 实测踩到的坑：文章首次被抓到时 WeRSS 往往只有标题+短摘要，正文
            # 是空的（「百亿龙头昨天涨停…」库里 HTML=0，而几分钟后 WeRSS 那边
            # 已经有 24,950 字符的正文）。ON CONFLICT DO NOTHING 让这个状态
            # 永久化 —— 正文永远不会补上。
            # 所以这里做一次**单向补齐**：只在「库里为空、这次有」时写。
            # 绝不用新值覆盖已有值 —— 正文被上游改写（微信排版修正、删图）
            # 时保留先到的原文更符合「存档」语义。
            if body:
                cur.execute(
                    """UPDATE sa_mp_articles
                       SET content_html = %s,
                           content_text = COALESCE(NULLIF(content_text,''), %s)
                       WHERE source_id = %s AND guid = %s
                         AND COALESCE(content_html,'') = ''""",
                    (body, body_txt, src["id"], it["guid"][:512]))
                if cur.rowcount:
                    result["backfilled"] += 1
                    result["backfilled_titles"].append(it["title"][:60])
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
            cur.execute("SELECT id, name, kind, url, feed_id, enabled, "
                        "use_fresh, last_fresh_at, idle_rounds "
                        "FROM sa_mp_sources WHERE enabled ORDER BY id")
            cols = ("id", "name", "kind", "url", "feed_id", "enabled",
                    "use_fresh", "last_fresh_at", "idle_rounds")
            sources = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not sources:
            return {"skipped": True, "reason": "没有启用的订阅源", "items": []}
        items = []
        for src in sources:
            # 先判要不要催抓，并把判定结果带进 result 供回写
            go_fresh, why = _should_go_fresh(src, conf)
            r = _fetch_source(deps, src, conf, force_fresh=go_fresh)
            r["fresh_attempted"] = go_fresh
            r["fresh_deferred"] = why
            items.append(r)
            with deps["get_conn"]() as conn, conn.cursor() as cur:
                # idle_rounds：连续多少轮没新文章。用于退避 ——
                # 没更新就别去催微信，有更新嫌疑才值得花风控配额。
                # 「有新文章」也包括**补到正文**（backfilled>0）：那说明
                # WeRSS 那边确实在产出，值得继续按原频率催。
                # 上限 999 防止无限增长（退避倍数另有 8 倍封顶）。
                cur.execute("""UPDATE sa_mp_sources
                               SET last_run=now(), last_status=%s,
                                   last_count=%s, last_error=%s,
                                   last_fresh_at = CASE WHEN %s THEN now()
                                                        ELSE last_fresh_at END,
                                   idle_rounds = CASE WHEN %s > 0 THEN 0
                                                ELSE LEAST(COALESCE(idle_rounds,0) + 1, 999)
                                           END
                               WHERE id=%s""",
                            (r.get("status", ""), r.get("total", 0),
                             r.get("error", "")[:900], go_fresh,
                             r.get("new", 0) + r.get("backfilled", 0),
                             src["id"]))
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
