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

import concurrent.futures
import io
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from html import escape as _html_escape
from html import unescape as _html_unescape
from html.parser import HTMLParser

import requests


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
    "notify_new": True,       # 有新文章时走通知（发不发、发哪些渠道见 notify_events 矩阵）
    "max_items": 50,          # 每源每轮最多取多少条
    "keep_days": 120,         # 文章保留天数，超期清理
    "timeout": 25,
    # ---- 真实发布时间（2026-10-11 加）----
    # 新文章入库后并发抓一次微信文章页取 var ct，失败就退 published_at=抓取时刻。
    # 关掉 = 只信 WeRSS 给的值（实测那是它的入库时刻，中位偏 18 分钟、最大偏 32 小时）。
    "resolve_true_time": True,
    "true_time_workers": 4,   # 别调高：对 mp.weixin.qq.com 并发越高越容易被风控
    # 把公众号文章写进 sa_news，让它进模拟盘/日报的决策上下文。
    # 关掉 = 只在「📰 公众号」页看，不参与决策。
    "publish_to_news": True,
    # ---- LLM 相关性过滤（2026-10-12 用户要「公众号必须参与决策」后加）----
    # 公司名匹配抓不到「题材级」关联：实测 715 篇里 406 篇一篇都没关联上，
    # 而它们恰恰是券商策略/行业观点这类**决策最有用**的内容。这一层让 LLM 在
    # 「自选+持仓」范围内做语义判断（同一公司/主营/上下游/该题材主要标的）。
    # 默认关的理由是**它改决策口径**（§0 的授权边界），**不是省钱** ——
    # 见 AGENTS.md「别为省 LLM 调用而牺牲效果」：额度要按效果放开。
    "relevance_llm": {
        "enabled": False,
        # 只给「还没被公司名关联上」的文章做；传 false = 每篇都问一遍，
        # 能捞到「点了 A 公司但主题其实是 B 行业」的额外关联（线上已按效果放开）。
        "only_unlinked": True,
        "batch": 6,            # 每次 LLM 调用放几篇
        "max_per_round": 12,   # 每轮最多处理几篇（防单轮跑太久；线上可放开）
        "snippet": 300,        # 每篇给模型看的开头字数
        "max_relations": 4,    # 每篇最多关联几只（宁缺毋滥）
        "dry_run": False,      # true = 只判不写库
    },
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
    # ---- 导出（2026-10-20 加：批量导出 MD / 打印成 PDF）----
    # 正文为空的文章先回微信原文重抓；重试仍失败就跳过并列进导出说明。
    'export': {
        'recrawl': True,
        'retries': 3,      # 单篇最多尝试几次（含首次）
        'workers': 4,      # 并发抓取数；别调高，微信会风控
    },
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
    """读 config.yaml 的 mp 段，补齐默认（每轮现读，改配置即生效）。

    ⚠️ 嵌套 dict 必须**深合并**（2026-10-12 加）：`conf.update(section)` 是浅合并，
    config.yaml 里只要写了 `relevance_llm:` 一个键（哪怕只想改 enabled），
    整个默认子字典就被顶掉，batch/上限全变 0 —— 与 10-11 那次「保存调度设置抹掉
    mp.auth」是同一类事故（默认值冒充用户配置）。
    """
    conf = dict(DEFAULT_MP_CONF)
    try:
        import yaml
        data = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        section = data.get("mp") or {}
        for k, v in section.items():
            if v is None:
                continue
            base = conf.get(k)
            if isinstance(base, dict) and isinstance(v, dict):
                conf[k] = {**base, **{kk: vv for kk, vv in v.items()
                                      if vv is not None}}
            else:
                conf[k] = v
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


def _normalize_mp_url(url: str) -> str:
    """微信 `/s/` 短链里的 `~` → `_`（2026-10-02 实测修复）。

    现象：183 篇文章里 55 篇抓不到正文，按 url 与 WeRSS 对齐后发现 ——
    **有正文的 128 篇 URL 含 `~` 次数为 0，无正文的 55 篇 100% 含 `~`**。
    微信 `/s/` 短链是 base64url、恒定 22 字符，字母表里根本没有 `~`。

    实测（3 种 UA × 跟随/不跟随重定向）：
      - `~` 版 → 恒 200 +「参数错误」页，页面里**没有**文章标题
      - `_` 版 → 微信 UA 下 302 到 `<同一 _ token>?nwr_flag=1`（只是加风控参数，
        **不会跳回 `~`**），跟随重定向后 200 + 标题命中
      - 把 `~` 换成 64 个 base64url 字符逐一试，**只有 `_` 不报参数错误且标题命中**

    概率自洽：22 字符里含至少一个 `_` 的概率 = 1−(63/64)²² ≈ 29.3%，
    183×29.3% ≈ 53.6 篇，实测失败 55 篇；失败组平均 `_` 数 1.25（理论 1.19）。

    WeRSS 自带的 `scripts/fix_weread_mp_urls.py` 注释声称相反结论
    （「微信把 `_` 302 回 `~`」），与上面实测矛盾，且它代码里的
    `"~" in new_token` 条件在当前微信行为下永不成立 —— 注释是陈旧的。

    url 和 guid 都要改 —— guid 就是 `ON CONFLICT (source_id, guid)` 的去重键。
    （2026-10-02 踩过：当时只改 url、刻意保留原始 guid 想「不动去重键」，结果
    远端 WeRSS 的 URL 修好之后开始下发 `_` 形式，同一篇文章被判成新文章，
    **插出 34 组重复行**。所以必须两侧同口径，且库里的历史 guid 也一并规范化。）
    """
    if not url or "~" not in url:
        return url
    head = "https://mp.weixin.qq.com/s/"
    if url.startswith(head):
        # 只换 token 段，query（?nwr_flag=1 等）保持原样
        token, sep, qs = url[len(head):].partition("?")
        return head + token.replace("~", "_") + (sep + qs if sep else "")
    return url


def _items_from_json(data) -> list[dict]:
    raw: list[dict] = []
    _collect_json(data, raw)
    out = []
    for it in raw:
        url_raw = _nested_str(it, _URL_KEYS)
        url = _normalize_mp_url(url_raw)
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
            # guid 也要规范化，见 _normalize_mp_url 注释（不同口径会造出重复行）
            "guid": _normalize_mp_url(_first(it, ("guid", "id", "mid")) or url_raw),
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
            "title": title, "url": _normalize_mp_url(link), "summary": desc,
            "content_html": body,
            "content_text": _html_to_text(body) if body else "",
            "author": author or mp_name, "mp_name": mp_name,
            "published_at": _parse_dt(date_raw),
            # guid 也要规范化，见 _normalize_mp_url 注释（不同口径会造出重复行）
            "guid": _normalize_mp_url(guid or link),
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
        # ---- 真实发布时间（2026-10-11 加，见下方 resolve_true_time 的注释）----
        # published_at_src 的三个取值：
        #   'wx'      = 从微信文章页 var ct 抽到的真值（唯一可信的）
        #   'fetched' = 没抽到，退回**我们自己的**抓取时刻（宁可粗糙也不造假）
        #   'rss'     = WeRSS 给的值，**已知是入库时刻不是发布时间**，只留档
        # rss_published_at 永远保留上游原值做审计对照 —— 这次事故的教训就是
        # 「看起来有值、实际是错的」，没有对照列就发现不了上游哪天又变了。
        cur.execute("ALTER TABLE sa_mp_articles "
                    "ADD COLUMN IF NOT EXISTS published_at_src TEXT")
        cur.execute("ALTER TABLE sa_mp_articles "
                    "ADD COLUMN IF NOT EXISTS rss_published_at TIMESTAMPTZ")
        cur.execute("UPDATE sa_mp_articles SET published_at_src = 'rss' "
                    "WHERE published_at_src IS NULL")
        cur.execute("UPDATE sa_mp_articles SET rss_published_at = published_at "
                    "WHERE rss_published_at IS NULL AND published_at IS NOT NULL")


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
    # 2026-10-02 加 content_text：原来列表只给 title + summary，而 summary
    # 有一个坑 —— **无正文的 66 篇 summary 100% 等于 title**，页面上是标题
    # 重复两遍、正文区一片空白（库里明明有 117 篇全文却一条都看不见）。
    # 带上 content_text 后前端既能给真摘要预览，也能不发请求就地展开纯文本。
    # 体积实测：60 篇 38 KB、200 篇 128 KB（content_html 才是大的，60 篇 784 KB，
    # 所以正文 HTML 走单独的 /body 接口按需取）。
    sql = ("SELECT a.id, a.source_id, a.title, a.url, a.author, a.mp_name, a.summary, "
           "a.published_at, a.fetched_at, a.is_read, s.name, a.content_text, "
           "a.published_at_src "
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
            # 时间来源标注（2026-10-11）。必带：WeRSS 给的 published_at 是**它自己
            # 的入库时刻**，页面不给标注的话，看的人会以为那就是发布时间。
            #   wx      = 微信文章页 var ct 抽到的真值
            #   fetched = 没抽到，用的是我们自己的抓取时刻
            #   rss     = WeRSS 原值（已知不准，保留只为可见）
            "published_at_src": r[12] or "rss",
            "is_read": r[9], "source_name": r[10],
            "content_text": r[11] or "",
            # 有没有可展开的正文（决定标题要不要显示展开箭头）
            "has_body": bool(r[11]),
        })
    return out


# ---------------- 单篇正文（含图片转 base64）----------------

_IMG_TAG = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*([\"'])(https?://[^\"']+)(\1)[^>]*>", re.I)
_IMG_NOSRC = re.compile(r"<img\b(?![^>]*\bsrc\s*=)[^>]*>", re.I)
_DATA_SRC = re.compile(r"(\bdata-src\s*=\s*)([\"'])(https?://[^\"']+)(\2)", re.I)
_MAX_IMG_BYTES = 3_000_000        # 单张上限，防被超大图拖垮
_INLINE_CACHE_MAX = 40            # 内联结果缓存条数（进程内）
_inline_cache: dict = {}
_inline_lock = threading.Lock()


def _img_to_data_url(url: str, sess) -> str | None:
    """下载一张图并转成 data URL；失败返回 None（调用方保留原始外链）。"""
    try:
        r = sess.get(url, timeout=(5, 12), stream=True)
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return None
        buf = bytearray()
        for chunk in r.iter_content(16384):
            buf.extend(chunk)
            if len(buf) > _MAX_IMG_BYTES:
                return None
        if len(buf) < 200:          # 小于 200 字节的多半是占位/错误页
            return None
        import base64
        return f"data:{ctype};base64," + base64.b64encode(bytes(buf)).decode()
    except Exception:
        return None


def inline_images(html: str) -> str:
    """把 HTML 里的远程图片（mmbiz.qpic.cn 等）下载转成 data URL 内嵌。

    为什么要内嵌（2026-10-02 实测）：公众号图床有防盗链且策略会随时变，
    直接引用外链 = 正文里的图表时好时坏。实测裸连 12/12 通，但**每篇都要
    现场确认**，所以改成落库后按需内嵌。

    为什么不预生成：230 张图 × 中位 111 KB → base64 后 36.5 MB，
    塞进列表接口不可接受（列表 60 篇纯文本才 38 KB）。单篇平均 2 张 ≈ 324 KB，
    按需内嵌刚好。下载失败保留原外链，不因为图挂了就整篇不显示。
    """
    if not html:
        return ""
    import requests

    urls = [m.group(2) for m in _IMG_TAG.finditer(html)]
    urls += [m.group(3) for m in _DATA_SRC.finditer(html)]
    if not urls:
        return html

    sess = requests.Session()
    sess.trust_env = False      # 与 _get 同策：绕开本机代理（NO_PROXY=*）
    sess.headers.update(HEADERS)
    done: dict = {}
    for u in dict.fromkeys(urls):        # 去重且保序
        key = _html_unescape(u)
        if key not in done:
            du = _img_to_data_url(key, sess)
            if du:
                done[key] = du
    if not done:
        return html

    def _sub(m):
        raw = _html_unescape(m.group(2))
        return m.group(0).replace(m.group(2), done.get(raw, m.group(2)))

    html = _IMG_TAG.sub(_sub, html)

    # 懒加载图（无 src，只有 data-src）：补上 src 内联后的值
    def _sub_nosrc(m):
        tag = m.group(0)
        dm = _DATA_SRC.search(tag)
        if not dm:
            return tag
        raw = _html_unescape(dm.group(3))
        du = done.get(raw)
        if not du:
            return tag
        return tag.replace(dm.group(0), f"{dm.group(1)}{dm.group(2)}{raw}{dm.group(2)}"
                                       f" src={dm.group(2)}{du}{dm.group(2)}")
    return _IMG_NOSRC.sub(_sub_nosrc, html)


def get_article_body(deps, aid: int, embed_images: bool = True) -> dict:
    """取单篇正文。带图片的 HTML 走这里，纯文本直接用列表里的 content_text。

    返回 {id, title, url, content_html, content_text, has_images}；
    content_html 里的图片已转 base64 内嵌（embed_images=True 时）。
    """
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, url, content_html, content_text "
            "FROM sa_mp_articles WHERE id = %s", (aid,))
        row = cur.fetchone()
    if not row:
        raise ValueError("文章不存在")
    aid2, title, url, html, txt = row[0], row[1], row[2], row[3] or "", row[4] or ""
    has_images = bool(_IMG_TAG.search(html) or _DATA_SRC.search(html))
    if not embed_images or not has_images:
        return {"id": aid2, "title": title, "url": url,
                "content_html": html, "content_text": txt,
                "has_images": False}

    ck = (aid2, len(html))
    with _inline_lock:
        hit = _inline_cache.get(ck)
    if hit is not None:
        return hit

    out = {"id": aid2, "title": title, "url": url,
           "content_html": inline_images(html), "content_text": txt,
           "has_images": True}
    with _inline_lock:
        if len(_inline_cache) >= _INLINE_CACHE_MAX:
            _inline_cache.pop(next(iter(_inline_cache)), None)
        _inline_cache[ck] = out
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
              "total": 0, "fresh": [], "backfilled": 0, "backfilled_titles": [],
              # 本轮真插入的行（带库内 id），供 run_once 回填真时间 + 推 sa_news
              "new_rows": []}
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
    # ⚠️ 这里**必须显式 commit**，不能只靠 `with conn` 的上下文退出。
    #
    # 实测踩到的事故（2026-10-01）：这个块以前只有 `with conn, cur:`，
    # psycopg2 的 with 只在**正常退出**时提交；一旦进程在块内被杀/断连，
    # 服务器上就留下一条 `idle in transaction` 的连接，**持有
    # sa_mp_articles 的表锁和行锁**（pg_locks 里 granted=True）。
    # 后果不是"慢"，是**全服务起不来**：app.py 启动时跑的那些幂等 DDL
    # （ALTER TABLE ... ADD COLUMN IF NOT EXISTS）全部排在这个锁后面，
    # 实测 4 个 ALTER 排队等了 20 多分钟，import app 直接卡死、
    # 8686 端口不监听，而任何一条日志里都没有报错 ——
    # 看起来像"启动了但没监听"，实际是卡在第一条 ALTER 上。
    #
    # 所以：每写完一个源就 commit（这里是一个源一个连接，粒度合适），
    # 不给任何异常路径留下持锁的窗口。
    conn = deps["get_conn"]()
    try:
        with conn.cursor() as cur:
            for it in items:
                body = it.get("content_html", "") or ""
                body_txt = it.get("content_text", "") or ""
                # published_at **不写 WeRSS 的值**（2026-10-11）。
                # 那个值是它的入库时刻：实测 40 篇对微信真值中位偏 +18 分钟、
                # 最大偏 32 小时（详见 resolve_true_time 的注释）。先落我们自己的
                # fetched 时刻并标 src='fetched'，本函数末尾再回填真值；
                # 真值拿不到就保持 'fetched' —— 宁可粗糙，也不让假时间冒充发布时间。
                cur.execute(
                    """
                    INSERT INTO sa_mp_articles
                        (source_id, guid, title, url, author, mp_name, summary,
                         published_at, content_html, content_text,
                         published_at_src, rss_published_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'fetched',%s)
                    ON CONFLICT (source_id, guid) DO NOTHING
                    """,
                    (src["id"], it["guid"][:512], it["title"], it["url"],
                     it.get("author", "") or "", it.get("mp_name", "") or src["name"],
                     it.get("summary", ""), now, body, body_txt,
                     it.get("published_at") or None))
                if cur.rowcount:            # rowcount=1 才是真插入（冲突时为 0）
                    result["new"] += 1
                    result["fresh"].append(it)
                    cur.execute("SELECT id FROM sa_mp_articles "
                                "WHERE source_id=%s AND guid=%s",
                                (src["id"], it["guid"][:512]))
                    r2 = cur.fetchone()
                    if r2:
                        result["new_rows"].append({
                            "id": int(r2[0]), "url": it["url"],
                            "title": it["title"],
                            "mp_name": it.get("mp_name", "") or src["name"],
                            "summary": it.get("summary", "") or "",
                        })
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
        conn.commit()          # ← 见上方注释：必须显式提交，不给异常路径留持锁窗口
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
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
        # ---- 新文章的后处理：回填真实发布时间 → 推入 sa_news ----
        # 两步都只作用于**本轮真插入**的行，且都是「失败不阻断主流程」：
        # 抓时间失败只是时间粗糙一点，推 news 失败只是不进决策上下文，
        # 都不该让这一轮的抓取结果作废。
        new_rows = [r for it in items for r in (it.get("new_rows") or [])]
        tstat = {"n": 0, "ok": 0, "fail": 0, "skipped": 0}
        nstat = {"n": 0, "inserted": 0, "skipped_no_time": 0,
                 "skipped_old": 0, "linked": 0, "unlinked": 0}
        # ---- LLM 相关性过滤（题材级关联；公司名匹配抓不到的那部分）----
        # 排在 publish_to_news 之后：它只处理「本轮新插入」的文章，且只补关系，
        # 不重复建 news 行。默认关（要花钱），失败不阻断主流程。
        rconf = conf.get("relevance_llm") or {}
        rstat = {"n": 0, "enabled": bool(rconf.get("enabled"))}
        if new_rows and conf.get("resolve_true_time", True):
            try:
                tstat = fill_true_times(deps, new_rows)
            except Exception as exc:
                print("[mp] 回填真实发布时间失败：%s" % str(exc)[:150], flush=True)
        if new_rows and conf.get("publish_to_news", True):
            try:
                nstat = publish_to_news(deps, new_rows)
            except Exception as exc:
                print("[mp] 推入 sa_news 失败：%s" % str(exc)[:150], flush=True)
        if new_rows and rconf.get("enabled"):
            try:
                rstat = llm_relate_to_holdings(deps, new_rows, rconf)
            except Exception as exc:
                rstat = {"error": str(exc)[:200]}
                print("[mp] LLM 相关性过滤失败：%s" % str(exc)[:150], flush=True)
        fresh = [i for it in items for i in it.get("fresh", [])]
        pushed = 0
        # 两道门：本段 notify_new（粗粒度，用户可能整段关掉）与
        # notifier 内部的事件矩阵（细粒度，notify_events.mp_article）。
        # 邮箱那一条由矩阵关掉 —— 见 notify_events.py 顶部说明。
        if fresh and conf.get("notify_new", True) and _pushable():
            try:
                res = deps["notify_fn"](_notify_title(len(fresh)),
                                        _notify_body(fresh),
                                        event="mp_article")
                pushed = 1 if (res or {}).get("sent") else 0
            except Exception as exc:
                print(f"[mp] notify failed: {exc}", flush=True)
        result = {"items": items, "new": sum(i["new"] for i in items),
                  "pushed": pushed, "true_time": tstat, "news": nstat,
                  "relevance": rstat,
                  "ts": datetime.now().isoformat(timespec="seconds")}
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


def _pushable() -> bool:
    """本次新文章还有没有可用渠道（矩阵全关时不必组装正文，省一次拼接）。

    notifier 延迟 import：本模块刻意不 import app（避免循环依赖），
    notifier 自己也只在被调用时才 import app 的 get_conn。
    """
    import notifier
    return notifier.event_enabled("mp_article")


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


# ---------------- 真实发布时间（2026-10-11 加）----------------
#
# 为什么要自己抓
# --------------
# WeRSS 的 `articles` 表里确实有 4 个时间列（publish_time / create_time /
# updated_at / 记录 created_at），看起来直接拿就行 —— **但它们全是同一个值**。
# 实测（把 101 上67MB 的 db.db 拉下来逐条对）：
#
#   id             publish_time    create_time     updated_at     记录 created_at
#   2397491920-…   10-11 22:00:03  22:00:03        22:00:03        2026-10-11 22:00:03
#   3964712822-…   10-11 21:00:13  21:00:13        21:00:13        2026-10-11 21:00:13
#
# 四列逐条相同，publish_info 是空 {}，fetch_started_at 是 NULL；
# 30 条样本的 create_time − publish_time 的 min/中位/max **全是 0**。
# 结论：fork 走 /web/mp/articles 列表接口时**根本没取微信侧的 update_time**，
# 四个列全被盖成了入库时刻。
#
# 那个值有多不准（基准 = 微信原文页的 var ct，40 篇样本）：
#   中位 +18 分钟，p25 +10、p75 +43；>60 分钟的 9/39；最大 +1933 分钟（32 小时）；
#   偏早的 0 条。（秒数连号 20:49:04/:13/:14 就是「插入循环里的 time.time()」的指纹）
#
# 真值在哪
# --------
# 微信文章页底部有个 JS 变量 `var ct = "1791721889"`（10 位 Unix 秒）——
# 页面顶上那个肉眼可见的 `id="publish_time"` 元素**是空的**，靠 JS 填，
# 所以抓 HTML 元素拿不到，只能读这个变量。
#
# 成本（实测）
#   单篇线传输 0.8MB（gzip；解压后 3.3MB）、1.4s
#   并发 4 抓 12 篇 = 5.5s；并发 6 = 4.1s；12/12 全部抽到，没被风控
#   → 每轮 21 篇新文章 ≈ 17MB / 约 10 秒，完全可接受

WX_ARTICLE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                 "MicroMessenger/8.0")
_WX_CT = re.compile(rb'var ct\s*=\s*"(\d{9,11})"')
# 兜底：约 18% 的页面（实测 1158 篇里 23 篇首抓失败、另有十几篇页面完整但无 var ct）
# 没有 var ct，却带 `create_time: <unix秒>`。
# **这个等价性是实测出来的，不是推的**：在 25 个「var ct 存在」的页面上交叉比对，
# create_time == var ct 是 25/25；而同页的 update_time == var ct 是 **0/25** ——
# 所以只能用 create_time，用 update_time 会拿到另一个值。
_WX_CREATE = re.compile(rb'create_time[^0-9]{0,20}(\d{9,11})')
# 合理性边界：早于 2000 年（不是真实文章）或晚于「现在 +1 小时」
# （时钟错/预发布）的一律拒绝 —— 宁可当成抽不到，也不能写进去一个假时间。
_CT_MIN = 946684800          # 2000-01-01T00:00:00Z


def fetch_true_publish_time(url: str, timeout: int = 25):
    """抓一篇公众号文章的真实发布时间。抽不到返回 None。

    来源优先级：`var ct` → `create_time`（见 _WX_CREATE 的交叉验证注释）。
    页面上的 `#publish_time` 元素是空的（JS 填），JSON-LD / og:published_time /
    <time datetime> 在 mp.weixin.qq.com 上实测全部不存在 —— 不写那些永远走不到的分支。
    """
    if not url or not url.startswith("http"):
        return None
    try:
        r = requests.get(url, headers={"User-Agent": WX_ARTICLE_UA}, timeout=timeout)
        if r.status_code != 200:
            return None
        body = r.content
        m = _WX_CT.search(body) or _WX_CREATE.search(body)
        if not m:
            return None
        ts = int(m.group(1))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if ts < _CT_MIN or dt > datetime.now(timezone.utc) + timedelta(hours=1):
            return None
        return dt
    except Exception:
        return None


def resolve_true_times(urls, workers: int = 4, timeout: int = 25) -> dict:
    """批量取真实发布时间。返回 {url: aware_datetime}，抽不到的url不在结果里。

    workers 默认 4：实测 12 篇并发 4 要 5.5s、并发 6 要 4.1s，再往上收益不大，
    而对 mp.weixin.qq.com 的并发越高越容易被风控，**宁可慢也别被拦**。
    """
    urls = [u for u in dict.fromkeys(urls or []) if u]
    if not urls:
        return {}
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max(1, int(workers))) as ex:
        futs = {ex.submit(fetch_true_publish_time, u, timeout): u for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            u = futs[fut]
            try:
                dt = fut.result()
            except Exception:
                dt = None
            if dt:
                out[u] = dt
    return out


def fill_true_times(deps, rows: list[dict]) -> dict:
    """给本轮新插入的文章回填真实发布时间。返回统计。

    rows: [{'id': int, 'url': str, ...}]（_fetch_source 攒的 new_rows）
    抽不到的**保持 src='fetched' 不动** —— published_at 已经是我们的抓取时刻，
    比上游那个假时间诚实。丢弃/失败必须打印，否则「时间对不上」会变成无人知晓的静默。
    """
    rows = [r for r in rows if r.get("id") and r.get("url")]
    stat = {"n": len(rows), "ok": 0, "fail": 0, "skipped": 0}
    if not rows:
        return stat
    try:
        found = resolve_true_times([r["url"] for r in rows])
    except Exception as exc:                      # 网络层整体失败
        print("[mp] 真实发布时间批量抓取失败：%s（%d 篇保持抓取时刻）"
              % (str(exc)[:120], len(rows)), flush=True)
        stat["fail"] = len(rows)
        return stat
    conn = deps["get_conn"]()
    try:
        with conn.cursor() as cur:
            for r in rows:
                dt = found.get(r["url"])
                if not dt:
                    stat["fail"] += 1
                    continue
                cur.execute(
                    "UPDATE sa_mp_articles SET published_at=%s, published_at_src='wx' "
                    "WHERE id=%s", (dt, r["id"]))
                stat["ok"] += cur.rowcount or 0
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    if stat["fail"]:
        print("[mp] 真实发布时间：%d 篇取到 var ct，%d 篇没取到（published_at "
              "保持抓取时刻 src='fetched'）" % (stat["ok"], stat["fail"]), flush=True)
    return stat


# --------------
# 导出：Markdown / 打印成 PDF（2026-10-20）
# --------------
# 用户诉求：批量导出公众号文章；**正文为空的先回微信原文重抓**，重试仍失败就跳过。
#
# 两个关键取舍：
#   ① **不引 PDF 库**。本机没有 weasyprint/reportlab/fpdf，装一个属于「加新依赖」
#      （AGENTS.md §0 要先问）。改成生成 A4 打印页，由浏览器「打印 → 另存为 PDF」
#      出 PDF —— 中文字体、分页、图片都交给浏览器，比手写 PDF 稳得多。
#   ② 重抓只补「仍为空」的行（`COALESCE(content_html,'')='' AND ...` 兜底），
#      **绝不用重抓结果覆盖已有正文** —— 这是补漏，不是刷新。
#
# 「空」的定义：content_html 与 content_text 都是空白。只有图片没有文字的文章
# html 非空 → 不算空，照常导出（图片本身就是正文）。

EXPORT_MAX_IDS = 200
try:
    from zoneinfo import ZoneInfo
    _BJ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:                    # Windows 缺 tzdata 时退回固定 +8
    _BJ_TZ = timezone(timedelta(hours=8))


class _JsContentFinder(HTMLParser):
    """定位微信文章页 `<div id="js_content">…</div>` 的**原始正文区间**。

    为什么不用正则直接抓：正文里嵌套大量 div，`<div ...>(.*?)</div>` 会在第一个
    内层闭合处截断。这里用 HTMLParser 的 getpos() 换算绝对偏移，再按 div 深度
    配平找闭合位置 —— 不用自己写 tokenizer，也不会被嵌套 div 骗到。
    """

    def __init__(self, text: str):
        super().__init__(convert_charrefs=False)
        # HTMLParser 只按 "\n" 计行（updatepos 里数的是 \n），这里必须同口径；
        # 用 splitlines() 会把单独的 \r 也当换行，偏移会整体错位。
        self._line_starts = [0]
        pos = text.find("\n")
        while pos != -1:
            self._line_starts.append(pos + 1)
            pos = text.find("\n", pos + 1)
        self.start: int | None = None
        self.end: int | None = None
        self._depth = 0
        self._done = False

    def _abs(self) -> int:
        line, col = self.getpos()
        return self._line_starts[max(0, min(line - 1, len(self._line_starts) - 1))] + col

    def handle_starttag(self, tag, attrs):
        if self._done or tag.lower() != "div":
            return
        if self.start is None:
            if any((k or "").lower() == "id" and (v or "").strip() == "js_content"
                   for k, v in attrs):
                raw = self.get_starttag_text() or ""
                self.start = self._abs() + len(raw)
                self._depth = 1
        else:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._done or tag.lower() != "div" or self.start is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            self.end = self._abs()
            self._done = True

    def handle_startendtag(self, tag, attrs):
        # 自闭合 <div/> 不能让深度 +1；默认实现会 start+end 各调一次，这里显式忽略。
        pass


_JS_CONTENT_RE = re.compile(
    r"<\s*div\b[^>]*\bid\s*=\s*(['\"])js_content\1[^>]*>", re.I)
_DIV_TAG_RE = re.compile(r"<\s*(/?)\s*div\b[^>]*>", re.I)


def _extract_js_content(page: str) -> str:
    """从微信文章页取出正文 HTML（js_content 区块内层）。取不到返回 ""。"""
    if not page:
        return ""
    finder = _JsContentFinder(page)
    try:
        finder.feed(page)
        finder.close()
    except Exception:
        pass
    if finder.start is not None and finder.end is not None and finder.end > finder.start:
        return page[finder.start:finder.end]
    # 兜底：页面结构不规范、HTMLParser 中途抛异常时按 div 深度配平再扫一次。
    m = _JS_CONTENT_RE.search(page)
    if not m:
        return ""
    depth = 1
    for tag in _DIV_TAG_RE.finditer(page, m.end()):
        raw = tag.group(0)
        if tag.group(1):
            depth -= 1
        elif not raw.rstrip().endswith("/>"):
            depth += 1
        if depth <= 0:
            return page[m.end():tag.start()]
    return ""


_IMG_TAG_RE = re.compile(r"<\s*img\b([^>]*)>", re.I)


def _attr_value(attrs: str, name: str) -> str:
    m = re.search(
        r"""(?:^|\s)%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""" % re.escape(name),
        attrs, re.I)
    if not m:
        return ""
    return _html_unescape(m.group(1) or m.group(2) or m.group(3) or "")


def _abs_url(url: str) -> str:
    u = (url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    return u


def _normalize_content_html(html: str) -> str:
    """微信正文里的图片统一成真实地址。

    正文图片是懒加载：`src` 是占位 SVG，真地址在 `data-src`。不换过来的话
    导出后图片全是空白占位。顺手丢掉宽度/样式属性 —— 打印页靠 CSS 自适应。
    """
    if not html:
        return ""

    def repl(m):
        attrs = m.group(1)
        src = _abs_url(_attr_value(attrs, "data-src")
                       or _attr_value(attrs, "data-original")
                       or _attr_value(attrs, "src"))
        if not src:
            return ""
        alt = _attr_value(attrs, "alt")
        return '<img src="%s" alt="%s">' % (
            _html_escape(src, quote=True), _html_escape(alt, quote=True))

    return _IMG_TAG_RE.sub(repl, html)


_DROP_BLOCK_RE = re.compile(
    r"<\s*(script|style|iframe|object|embed|form|link|meta|base|svg|"
    r"video|audio|input|button|textarea|select)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.I | re.S)
_DROP_TAG_RE = re.compile(
    r"<\s*/?\s*(script|style|iframe|object|embed|form|link|meta|base|svg|"
    r"video|audio|input|button|textarea|select)\b[^>]*>", re.I)
_ON_ATTR_RE = re.compile(
    r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_JS_URL_RE = re.compile(
    r"""(\s(?:href|src)\s*=\s*)(["']?)\s*javascript:[^"'>\s]*\2""", re.I)


def _sanitize_html(html: str) -> str:
    """导出页要嵌进浏览器，必须挡掉脚本与事件属性（与前端 sanitizeMpHtml 同口径）。"""
    if not html:
        return ""
    s = _DROP_BLOCK_RE.sub("", html)
    s = _DROP_TAG_RE.sub("", s)
    s = _ON_ATTR_RE.sub("", s)
    s = _JS_URL_RE.sub(lambda m: m.group(1) + m.group(2) + "#" + m.group(2), s)
    return s


def _safe_href(url: str) -> str:
    u = _abs_url(url)
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:", "#")):
        return u
    return ""


def _safe_img_src(url: str) -> str:
    u = _abs_url(url)
    low = u.lower()
    if low.startswith(("http://", "https://", "data:image/")):
        return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return ""


class _HtmlToMarkdown(HTMLParser):
    """微信正文 HTML → Markdown（只用标准库，不引第三方转换器）。

    覆盖公众号正文实际出现的结构：标题、段落/section、换行、列表、引用、
    代码块、粗斜体、链接、图片、表格、分隔线。未识别的标签按内联处理。
    """

    _BLOCK = {"p", "div", "section", "article", "header", "footer", "main",
              "figure", "figcaption", "aside", "center", "dl", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._quote_stack: list[list[str]] = []
        self._lists: list[dict] = []
        self._link: list[str] = []
        self._pre = 0
        self._cell: list[str] | None = None
        self._table: list[list[str]] | None = None

    # ---- 输出缓冲 ----
    def _trim(self):
        while self.parts:
            last = self.parts[-1]
            if not last:
                self.parts.pop()
                continue
            stripped = last.rstrip(" \t")
            if stripped != last:
                if stripped:
                    self.parts[-1] = stripped
                else:
                    self.parts.pop()
                    continue
            break

    def _newlines(self, n: int = 2):
        if self._cell is not None:
            return
        if self._pre:
            self.parts.append("\n")
            return
        self._trim()
        have = 0
        for p in reversed(self.parts):
            if not p:
                continue
            core = p.rstrip("\n")
            have += len(p) - len(core)
            if core.strip():
                break
        if have < n:
            self.parts.append("\n" * (n - have))

    def _append(self, text: str):
        if not text:
            return
        if self._cell is not None:
            self._cell.append(text)
        else:
            self.parts.append(text)

    # ---- 事件 ----
    def handle_data(self, data):
        if not data:
            return
        if self._pre:
            self._append(data.replace("\r\n", "\n").replace("\r", "\n"))
            return
        text = re.sub(r"[ \t\r\n\f\v\u00a0]+", " ", data)
        if not text:
            return
        if self._cell is not None:
            self._cell.append(text)
            return
        if not self.parts or self.parts[-1].endswith("\n"):
            text = text.lstrip(" ")
        if text:
            self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {str(k).lower(): (v or "") for k, v in attrs}
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newlines(2)
            self._append("#" * int(tag[1]) + " ")
        elif tag in self._BLOCK:
            self._newlines(2)
        elif tag == "br":
            self._append("  \n")
        elif tag == "hr":
            self._newlines(2)
            self._append("---")
            self._newlines(2)
        elif tag == "blockquote":
            self._quote_stack.append(self.parts)
            self.parts = []
        elif tag in ("ul", "ol"):
            self._newlines(2 if not self._lists else 1)
            self._lists.append({"ordered": tag == "ol", "n": 0})
        elif tag == "li":
            self._newlines(1)
            depth = max(0, len(self._lists) - 1)
            marker = "- "
            if self._lists:
                lst = self._lists[-1]
                if lst["ordered"]:
                    lst["n"] += 1
                    marker = "%d. " % lst["n"]
            self._append("  " * depth + marker)
        elif tag in ("strong", "b"):
            self._append("**")
        elif tag in ("em", "i"):
            self._append("*")
        elif tag == "code" and not self._pre:
            self._append("`")
        elif tag == "pre":
            self._newlines(2)
            self._append("```\n")
            self._pre += 1
        elif tag == "a":
            href = _safe_href(a.get("href", ""))
            self._link.append(href)
            if href:
                self._append("[")
        elif tag == "img":
            src = _safe_img_src(a.get("data-src") or a.get("data-original")
                                or a.get("src") or "")
            if src:
                alt = (a.get("alt") or "").strip().replace("]", "\\]")
                self._append("![%s](%s)" % (alt, src))
        elif tag == "table":
            self._table = []
            self._cell = None
            self._newlines(2)
        elif tag == "tr":
            if self._table is not None:
                self._table.append([])
        elif tag in ("td", "th"):
            if self._table is not None:
                self._cell = []
        elif tag == "sup":
            self._append("^")
        elif tag == "sub":
            self._append("~")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newlines(2)
        elif tag in self._BLOCK:
            self._newlines(2)
        elif tag == "blockquote":
            self._end_quote()
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._newlines(2)
        elif tag == "li":
            self._newlines(1)
        elif tag in ("strong", "b"):
            self._append("**")
        elif tag in ("em", "i"):
            self._append("*")
        elif tag == "code" and not self._pre:
            self._append("`")
        elif tag == "pre":
            self._newlines(1)
            self._append("```")
            self._pre = max(0, self._pre - 1)
            self._newlines(2)
        elif tag == "a":
            href = self._link.pop() if self._link else ""
            if href:
                self._append("](%s)" % href)
        elif tag in ("td", "th"):
            if self._table is not None:
                cell = "".join(self._cell or [])
                cell = re.sub(r"\s+", " ", cell).strip().replace("|", "\\|")
                if not self._table:
                    self._table.append([])
                self._table[-1].append(cell)
                self._cell = None
        elif tag == "table":
            self._render_table()
        elif tag == "sup":
            self._append("^")
        elif tag == "sub":
            self._append("~")

    def _end_quote(self):
        if not self._quote_stack:
            return
        inner = "".join(self.parts)
        self.parts = self._quote_stack.pop()
        inner = re.sub(r"\n{3,}", "\n\n", inner).strip()
        if not inner:
            return
        quoted = "\n".join(("> " + ln) if ln.strip() else ">"
                           for ln in inner.split("\n"))
        self._newlines(2)
        self.parts.append(quoted)
        self._newlines(2)

    def _render_table(self):
        rows = [r for r in (self._table or []) if r]
        self._table = None
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |",
               "| " + " | ".join(["---"] * width) + " |"]
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        self._newlines(2)
        self._append("\n".join(out))
        self._newlines(2)

    def result(self) -> str:
        while self._quote_stack:          # 容错：未闭合的引用
            self._end_quote()
        s = "".join(self.parts)
        s = re.sub(r"[ \t]+\n(?=\s*\n)", "\n", s)   # 段末空格（保留 <br> 的硬换行）
        # 不能把「换行后的缩进」一律删掉：嵌套列表就是靠 "  - " 的缩进表达层级。
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()


def html_to_markdown(html: str) -> str:
    """HTML → Markdown；转换器自身出错时退回纯文本（绝不抛给导出流程）。"""
    if not html:
        return ""
    conv = _HtmlToMarkdown()
    try:
        conv.feed(html)
        conv.close()
        return conv.result()
    except Exception as exc:
        print("[mp] HTML→Markdown 失败，退回纯文本：%s" % str(exc)[:120], flush=True)
        return _html_to_text(html)


_RISK_MARKERS = ("环境异常", "访问过于频繁", "去验证", "请在微信客户端打开",
                 "该内容已被发布者删除", "此内容因违规无法查看", "参数错误",
                 "该公众号已迁移")


def fetch_article_content(url: str, timeout: int = 20, attempts: int = 3) -> dict:
    """回微信原文抓正文。返回 {"html", "text", "error"}。

    重试 attempts 次（含首次），间隔 1.2s / 2.4s… 递增；**不做无限重试**：
    对 mp.weixin.qq.com 的密集请求会被风控，失败就交给调用方跳过并说明。
    """
    if not url or not url.startswith("http"):
        return {"html": "", "text": "", "error": "无效的原文链接"}
    attempts = max(1, min(8, int(attempts or 1)))
    last_err = ""
    for i in range(attempts):
        try:
            r = requests.get(url, headers={"User-Agent": WX_ARTICLE_UA},
                             timeout=timeout)
            if r.status_code != 200:
                last_err = "HTTP %d" % r.status_code
            else:
                page = r.content.decode("utf-8", "replace")
                inner = _extract_js_content(page)
                if inner:
                    html = _normalize_content_html(inner)
                    text = _html_to_text(html)
                    if html.strip() or text.strip():
                        return {"html": html, "text": text, "error": ""}
                    last_err = "原文正文为空"
                else:
                    hit = next((k for k in _RISK_MARKERS if k in page[:6000]), "")
                    last_err = ("原文页被拦截/失效（%s）" % hit) if hit else \
                        "原文页里没有正文区块"
        except Exception as exc:
            last_err = str(exc)[:160] or exc.__class__.__name__
        if i + 1 < attempts:
            time.sleep(1.2 * (i + 1))
    return {"html": "", "text": "", "error": last_err or "未知错误"}


def _is_empty_article(a: dict) -> bool:
    return not (a.get("content_html") or "").strip() and \
        not (a.get("content_text") or "").strip()


def recrawl_empty_articles(deps, articles: list[dict], conf: dict | None = None) -> dict:
    """把正文为空的文章回原文重抓；只更新仍为空的行，失败就记进 skipped。

    返回 {"empty", "recovered", "skipped", "skipped_items":[{id,title,url,reason}]}。
    """
    conf = conf or load_mp_conf()
    ec = conf.get("export") or {}
    enabled = bool(ec.get("recrawl", True))
    tries = max(1, min(8, int(ec.get("retries") or 3)))
    workers = max(1, min(8, int(ec.get("workers") or 4)))
    empty = [a for a in articles if _is_empty_article(a)]
    stat = {"empty": len(empty), "recovered": 0, "skipped": 0, "skipped_items": []}
    if not empty:
        return stat
    if not enabled:
        stat["skipped"] = len(empty)
        stat["skipped_items"] = [
            {"id": a.get("id"), "title": a.get("title") or "",
             "url": a.get("url") or "",
             "reason": "未开启正文补抓（mp.export.recrawl=false）"}
            for a in empty]
        return stat

    results: dict = {}
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_article_content, a.get("url") or "", 20, tries): a
                for a in empty}
        for fut in concurrent.futures.as_completed(futs):
            a = futs[fut]
            try:
                results[a.get("id")] = fut.result()
            except Exception as exc:
                results[a.get("id")] = {"html": "", "text": "",
                                        "error": str(exc)[:160] or "抓取异常"}

    conn = None
    try:
        conn = deps["get_conn"]()
    except Exception as exc:
        print("[mp] 导出补抓：拿不到数据库连接，结果只在本次导出内存里生效（%s）"
              % str(exc)[:120], flush=True)

    updates: list[tuple] = []
    for a in empty:
        res = results.get(a.get("id")) or {}
        html = res.get("html") or ""
        text = res.get("text") or ""
        if not html.strip() and not text.strip():
            stat["skipped"] += 1
            stat["skipped_items"].append({
                "id": a.get("id"), "title": a.get("title") or "",
                "url": a.get("url") or "",
                "reason": res.get("error") or "原文正文为空"})
            continue
        a["content_html"] = html
        a["content_text"] = text
        stat["recovered"] += 1
        updates.append((a.get("id"), html, text))
    if updates and conn is not None:
        try:
            with conn.cursor() as cur:
                # 单向：只写「读完这篇时仍然为空」的行。抓取期间若别的进程
                # 已补上正文，这里绝不覆盖。
                # 一次 UNNEST 批量写：远程 PG 每往返约 300ms（AGENTS.md 记过
                # 逐行 INSERT 249 行要 81.8 秒），逐篇 UPDATE 会把导出拖成分钟级。
                cur.execute(
                    "UPDATE sa_mp_articles a SET content_html=v.html, "
                    "content_text=v.text "
                    "FROM (SELECT * FROM UNNEST(%s::bigint[], %s::text[], "
                    "%s::text[]) AS t(id, html, text)) v "
                    "WHERE a.id = v.id AND COALESCE(a.content_html,'')='' "
                    "AND COALESCE(a.content_text,'')=''",
                    ([u[0] for u in updates], [u[1] for u in updates],
                     [u[2] for u in updates]))
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print("[mp] 导出补抓写库失败（%d 篇）：%s（本次导出仍用抓到的新内容）"
                  % (len(updates), str(exc)[:120]), flush=True)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    if stat["empty"]:
        print("[mp] 导出补抓：空正文 %d 篇，补到 %d 篇，跳过 %d 篇"
              % (stat["empty"], stat["recovered"], stat["skipped"]), flush=True)
    return stat


def _load_export_articles(deps, ids) -> list[dict]:
    clean: list[int] = []
    for v in ids or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0 and n not in clean:
            clean.append(n)
    clean = clean[:EXPORT_MAX_IDS]
    if not clean:
        return []
    conn = deps["get_conn"]()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.id, a.title, a.url, a.author, a.mp_name, a.summary, "
                "a.published_at, a.fetched_at, a.published_at_src, "
                "COALESCE(a.content_html,'') AS content_html, "
                "COALESCE(a.content_text,'') AS content_text, "
                "COALESCE(s.name,'') AS source_name "
                "FROM sa_mp_articles a "
                "LEFT JOIN sa_mp_sources s ON s.id = a.source_id "
                "WHERE a.id = ANY(%s) "
                "ORDER BY COALESCE(a.published_at, a.fetched_at) DESC NULLS LAST, "
                "a.id DESC", (clean,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r) if isinstance(r, dict) else dict(zip(cols, r))
        out.append({
            "id": d.get("id"),
            "title": d.get("title") or "（无标题）",
            "url": d.get("url") or "",
            "author": d.get("author") or "",
            "mp_name": d.get("mp_name") or "",
            "summary": d.get("summary") or "",
            "published_at": d.get("published_at"),
            "fetched_at": d.get("fetched_at"),
            "published_at_src": d.get("published_at_src") or "",
            "content_html": d.get("content_html") or "",
            "content_text": d.get("content_text") or "",
            "source_name": d.get("source_name") or "",
        })
    return out


def _fmt_dt(dt) -> str:
    if not dt:
        return ""
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_BJ_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)[:16]


def _src_note(src: str) -> str:
    return "" if (src or "") == "wx" else "（近似：抓取/RSS 时刻）"


_BAD_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_filename(name: str, fallback: str = "article", max_len: int = 70) -> str:
    s = _BAD_FN.sub(" ", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = fallback
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or fallback


def _article_body_md(a: dict) -> str:
    html = a.get("content_html") or ""
    if html.strip():
        return html_to_markdown(_normalize_content_html(html))
    return (a.get("content_text") or "").strip()


def _md_doc(a: dict, body: str, index: int | None = None) -> str:
    title = a.get("title") or "（无标题）"
    head = ("%d. %s" % (index, title)) if index else title
    lines = ["# " + head, ""]
    meta = []
    mp_name = a.get("mp_name") or a.get("source_name")
    if mp_name:
        meta.append("公众号：%s" % mp_name)
    if a.get("author"):
        meta.append("作者：%s" % a.get("author"))
    when = _fmt_dt(a.get("published_at") or a.get("fetched_at"))
    if when:
        meta.append("发布时间：%s%s" % (when, _src_note(a.get("published_at_src"))))
    if a.get("url"):
        meta.append("原文：%s" % a.get("url"))
    for m in meta:
        lines.append("- " + m)
    lines += ["", "---", "", body.strip() or "_（原文正文为空）_", ""]
    return "\n".join(lines)


def _skip_doc(items: list[dict], total: int, ok: int) -> str:
    lines = ["# 导出说明", "",
             "- 共请求：%d 篇" % total,
             "- 成功导出：%d 篇" % ok,
             "- 未能导出：%d 篇" % len(items), "",
             "## 未能导出的文章", "",
             "以下文章正文为空，已回微信原文重试仍未取到，已跳过：", ""]
    for it in items:
        lines.append("- %s（id=%s）" % (it.get("title") or "（无标题）", it.get("id")))
        lines.append("  - 原因：%s" % (it.get("reason") or "正文为空"))
        if it.get("url"):
            lines.append("  - 原文：%s" % it.get("url"))
    return "\n".join(lines) + "\n"


def _skip_note(items: list[dict]) -> str:
    lines = ["> ⚠️ 另有 %d 篇正文为空、重新抓取失败，**未包含在本次导出中**：" % len(items)]
    for it in items:
        lines.append("> - %s（id=%s）：%s"
                     % (it.get("title") or "（无标题）", it.get("id"),
                        it.get("reason") or "正文为空"))
    return "\n".join(lines)


def _zip_bytes(files: list[tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files:
            base = name
            n = 2
            while name in used:
                stem, dot, ext = base.rpartition(".")
                name = (stem or base) + "-%d" % n + (dot + ext if dot else "")
                n += 1
            used.add(name)
            zf.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


def export_markdown(deps, ids, conf: dict | None = None) -> dict:
    """批量导出 Markdown。单篇返回 .md，多篇返回 .zip（含导出说明）。"""
    conf = conf or load_mp_conf()
    arts = _load_export_articles(deps, ids)
    if not arts:
        raise ValueError("没有找到可导出的文章（可能已被清理或 id 无效）")
    rc = recrawl_empty_articles(deps, arts, conf)
    ok = [a for a in arts if not _is_empty_article(a)]
    skipped = rc.get("skipped_items") or []
    if not ok:
        detail = "；".join("%s（%s）" % (s.get("title") or s.get("id"),
                                       s.get("reason") or "正文为空")
                          for s in skipped[:5])
        raise ValueError("所选文章正文均为空，重新抓取也失败" +
                         ("：" + detail if detail else ""))
    stamp = datetime.now(timezone.utc).astimezone(_BJ_TZ).strftime("%Y%m%d-%H%M")
    if len(ok) == 1:
        a = ok[0]
        md = _md_doc(a, _article_body_md(a))
        if skipped:
            md = _skip_note(skipped) + "\n\n" + md
        return {
            "data": md.encode("utf-8"),
            "filename": _safe_filename(a.get("title"), "文章%d" % a.get("id")) + ".md",
            "media_type": "text/markdown; charset=utf-8",
            "exported": 1, "skipped": len(skipped),
            "skipped_items": skipped, "mode": "single",
        }
    files: list[tuple[str, str]] = []
    if skipped:
        files.append(("00_导出说明.md", _skip_doc(skipped, len(arts), len(ok))))
    for i, a in enumerate(ok, 1):
        name = "%02d-%s.md" % (i, _safe_filename(a.get("title"),
                                                "文章%d" % a.get("id"), 50))
        files.append((name, _md_doc(a, _article_body_md(a))))
    return {
        "data": _zip_bytes(files),
        "filename": "公众号文章_%s_%d篇.zip" % (stamp, len(ok)),
        "media_type": "application/zip",
        "exported": len(ok), "skipped": len(skipped),
        "skipped_items": skipped, "mode": "zip",
    }


_PRINT_CSS = """
@page { size: A4; margin: 14mm 13mm; }
* { box-sizing: border-box; }
body { margin: 0; padding: 22px 26px 40px; color: #222;
  font: 14px/1.75 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  background: #fff; }
.mp-toolbar { position: sticky; top: 0; z-index: 9; display: flex; gap: 10px;
  align-items: center; flex-wrap: wrap; padding: 10px 14px; margin-bottom: 18px;
  background: #f5f7fa; border: 1px solid #e3e8ef; border-radius: 10px; }
.mp-toolbar button { font-size: 14px; padding: 6px 16px; border-radius: 8px;
  border: 1px solid #1a73e8; background: #1a73e8; color: #fff; cursor: pointer; }
.mp-toolbar .tip { color: #667085; font-size: 12px; }
.notice { border: 1px solid #f0c36d; background: #fff8e6; color: #7a5b00;
  border-radius: 8px; padding: 10px 14px; margin: 14px 0; font-size: 13px; }
.notice ul { margin: 6px 0 0; padding-left: 20px; }
.cover { page-break-after: always; }
.cover h1 { font-size: 25px; margin: 8px 0 4px; }
.cover .sub { color: #667085; margin-bottom: 18px; }
.toc { border-top: 1px solid #e3e8ef; padding-top: 12px; }
.toc h2 { font-size: 17px; }
.toc li { margin: 4px 0; }
.mp-art { page-break-after: always; }
.mp-art:last-of-type { page-break-after: auto; }
.mp-art h1 { font-size: 22px; line-height: 1.45; margin: 0 0 10px; }
.mp-meta { color: #667085; font-size: 12px; border-bottom: 1px solid #e3e8ef;
  padding-bottom: 10px; margin-bottom: 18px; word-break: break-all; }
.mp-meta a { color: #1a73e8; text-decoration: none; }
.mp-body img { max-width: 100% !important; height: auto !important; }
.mp-body table { border-collapse: collapse; max-width: 100%; }
.mp-body td, .mp-body th { border: 1px solid #d0d5dd; padding: 4px 8px; }
.mp-body pre { white-space: pre-wrap; word-break: break-word; background: #f8fafc;
  border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; }
.mp-body a { color: #1a73e8; }
.mp-text { white-space: pre-wrap; word-break: break-word; }
@media print {
  body { padding: 0; }
  .mp-toolbar { display: none !important; }
}
"""

_PRINT_JS = """
(function () {
  var printed = false;
  function go() {
    if (printed) return;
    printed = true;
    setTimeout(function () { window.print(); }, 150);
  }
  var imgs = Array.prototype.slice.call(document.images || []);
  var left = imgs.filter(function (i) { return !i.complete; }).length;
  if (!left) { go(); return; }
  var timer = setTimeout(go, 6000);
  imgs.forEach(function (img) {
    if (img.complete) return;
    function done() {
      img.removeEventListener('load', done);
      img.removeEventListener('error', done);
      if (--left <= 0) { clearTimeout(timer); go(); }
    }
    img.addEventListener('load', done);
    img.addEventListener('error', done);
  });
})();
"""


def _print_body_html(a: dict) -> str:
    html = a.get("content_html") or ""
    if html.strip():
        return _sanitize_html(_normalize_content_html(html))
    text = a.get("content_text") or ""
    if not text.strip():
        return ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n".join("<p>%s</p>" % _html_escape(p).replace("\n", "<br>")
                     for p in paras)


def export_print_html(deps, ids, conf: dict | None = None) -> dict:
    """生成可打印的 A4 页面（浏览器「打印 → 另存为 PDF」即得 PDF）。"""
    conf = conf or load_mp_conf()
    arts = _load_export_articles(deps, ids)
    if not arts:
        raise ValueError("没有找到可导出的文章（可能已被清理或 id 无效）")
    rc = recrawl_empty_articles(deps, arts, conf)
    ok = [a for a in arts if not _is_empty_article(a)]
    skipped = rc.get("skipped_items") or []
    if not ok:
        detail = "；".join("%s（%s）" % (s.get("title") or s.get("id"),
                                       s.get("reason") or "正文为空")
                          for s in skipped[:5])
        raise ValueError("所选文章正文均为空，重新抓取也失败" +
                         ("：" + detail if detail else ""))

    now = datetime.now(timezone.utc).astimezone(_BJ_TZ).strftime("%Y-%m-%d %H:%M")
    file_title = "公众号文章导出 %s（%d 篇）" % (now, len(ok))
    h = []
    h.append("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    h.append("<meta name=\"referrer\" content=\"no-referrer\">")
    h.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    h.append("<title>%s</title><style>%s</style></head><body>"
             % (_html_escape(file_title), _PRINT_CSS))
    h.append("<div class=\"mp-toolbar\"><button onclick=\"window.print()\">"
             "🖨 打印 / 保存为 PDF</button>"
             "<span class=\"tip\">在弹出的打印对话框里选择「另存为 PDF」；"
             "勾选「背景图形」可保留文章底色。图片加载完成后会自动弹出打印框。</span>"
             "</div>")
    h.append("<section class=\"cover\"><h1>%s</h1>"
             "<div class=\"sub\">导出时间：%s · 共 %d 篇</div>"
             % (_html_escape("公众号文章导出"), _html_escape(now), len(ok)))
    if skipped:
        h.append("<div class=\"notice\">⚠️ 有 %d 篇正文为空，回原文重试仍未取到，"
                 "已跳过：<ul>" % len(skipped))
        for s in skipped:
            label = _html_escape(s.get("title") or "（无标题）")
            if s.get("url"):
                label = "<a href=\"%s\">%s</a>" % (
                    _html_escape(s.get("url"), quote=True), label)
            h.append("<li>%s —— %s</li>"
                     % (label, _html_escape(s.get("reason") or "正文为空")))
        h.append("</ul></div>")
    h.append("<div class=\"toc\"><h2>目录</h2><ol>")
    for a in ok:
        h.append("<li>%s</li>" % _html_escape(a.get("title") or "（无标题）"))
    h.append("</ol></div></section>")

    for i, a in enumerate(ok, 1):
        meta = []
        mp_name = a.get("mp_name") or a.get("source_name")
        if mp_name:
            meta.append("公众号：%s" % _html_escape(mp_name))
        if a.get("author"):
            meta.append("作者：%s" % _html_escape(a.get("author")))
        when = _fmt_dt(a.get("published_at") or a.get("fetched_at"))
        if when:
            meta.append("发布时间：%s%s"
                        % (_html_escape(when), _html_escape(_src_note(a.get("published_at_src")))))
        if a.get("url"):
            meta.append("原文：<a href=\"%s\">%s</a>" % (
                _html_escape(a.get("url"), quote=True),
                _html_escape(a.get("url"))))
        body = _print_body_html(a)
        h.append("<section class=\"mp-art\" id=\"art-%s\">" % _html_escape(str(a.get("id"))))
        h.append("<h1>%d. %s</h1>" % (i, _html_escape(a.get("title") or "（无标题）")))
        h.append("<div class=\"mp-meta\">%s</div>" % " · ".join(meta))
        h.append("<div class=\"mp-body\">%s</div></section>"
                 % (body or "<p>（原文正文为空）</p>"))
    h.append("<script>%s</script></body></html>" % _PRINT_JS)
    return {
        "html": "".join(h),
        "exported": len(ok), "skipped": len(skipped),
        "skipped_items": skipped,
        "filename": _safe_filename(file_title, "公众号文章导出") + ".html",
    }


def _link_index(cur) -> dict:
    """名册索引 + 自选股名/代码别名 → code。

    为什么不能只用具册（2026-10-12 实测）：`load_name_index` 只读 sa_stock_roster
    （**A股 5640 只**），而自选/持仓里 **14/43 根本不在名册里** —— 8 只港股
    （01024 快手-W、00100 MINIMAX-W、02513、06658、01368、01888、00136、001979）
    和 6 只 ETF（159321、159329、512710、513980、588000、510310）。名册里没有它们的
    名字，`_find_names` 就不可能命中 —— 也就是说**这类持仓的公众号文章永远
    关联不到自己**，实测「【MiniMax-W 暴涨…】」这种标题里直接点名（位置 23）都
    连不上。

    这里补三层，全部**通用规则**，没有任何逐票手写映射：
      ① 自选股名（港股去掉 -W/-U/-B 等后缀；A 股去掉 股份/有限公司/集团/控股）
      ② 同上再去掉「贵州/中国/北京…」地域前缀（「中国海洋石油」→「海洋石油」）
      ③ 股票代码本身（财经标题习惯直接写 6 位/5 位代码，如「【600031…】」）
    长度门槛沿用 `_find_names` 的 >= 3：2 字短名（茅台、快手）一律不收 ——
    假阳性比漏报有害。

    ⚠️ `load_name_index` 只吃 **tuple cursor**；传 dict cursor 会静默产出垃圾
    （dict 迭代出的是列名，我在验证时就被这个坑过一次）。
    """
    idx: dict[str, str] = {}
    try:
        from stock_roster import load_name_index
        idx.update(load_name_index(cur) or {})
    except Exception as exc:
        print("[mp] 股票名册索引取不到（%s），只按自选股名匹配" % str(exc)[:80],
              flush=True)
    try:
        cur.execute("SELECT code, name FROM sa_watchlist")
        rows = cur.fetchall()
    except Exception:
        rows = []
    sfx = ("-W", "-U", "-SW", "-B", "股份", "有限公司", "集团", "控股")
    pre = ("贵州", "江苏", "浙江", "中国", "北京", "上海", "广东")
    for row in rows:
        code = (row[0] or "").strip()
        name = (row[1] or "").strip()
        if not code:
            continue
        cands = {name, code}
        for s in sfx:
            if name.endswith(s) and len(name) - len(s) >= 3:
                cands.add(name[: -len(s)])
        for p_ in pre:
            if name.startswith(p_) and len(name) - len(p_) >= 3:
                cands.add(name[len(p_):])
        for c in cands:
            c = c.strip()
            if len(c) >= 3 and c not in idx:
                idx[c] = code
    return idx


def publish_to_news(deps, rows: list[dict], max_age_days: int = 7) -> dict:
    """把公众号文章写进 sa_news，让它参与模拟盘/日报的决策上下文。

    口径与 news_fetcher.save_to_db 完全一致（2026-10-08 用户定的规矩）：
      · **没有真实发布时间就不写** —— 只处理 published_at_src='wx' 的行。
        这里没有「抽不到就退 fetched_at」的兜底：那是「抓取时间冒充发布时间」，
        正是 6 月旧闻被当成当日新闻的根因。
      · 7 天以外的旧文对决策没有价值，直接丢。
    主 code 列写 ''（还不知道是哪只票），命中到的公司全称走 sa_news_related
    多对多 —— 和视频号（wx_channels）同一套路，也和 _paper_news_rows 的
    `n.code = %s OR r.code = %s` 查询对得上。
    """
    rows = [r for r in rows if r.get("id") and r.get("url")]
    stat = {"n": len(rows), "inserted": 0, "skipped_no_time": 0,
            "skipped_old": 0, "linked": 0, "unlinked": 0}
    if not rows:
        return stat
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    conn = deps["get_conn"]()
    try:
        with conn.cursor() as cur:
            index = _link_index(cur)
            for r in rows:
                cur.execute("SELECT title, published_at, published_at_src, "
                            "COALESCE(content_text,'') "
                            "FROM sa_mp_articles WHERE id=%s", (r["id"],))
                got = cur.fetchone()
                if not got:
                    continue
                title, pub, src, body_txt = got
                if src != "wx" or not pub:
                    stat["skipped_no_time"] += 1
                    continue
                if pub < cutoff:
                    stat["skipped_old"] += 1
                    continue
                senti = ""
                try:
                    import news_fetcher
                    senti = news_fetcher.classify_sentiment(title or "") or ""
                except Exception:
                    senti = ""
                cur.execute(
                    "INSERT INTO sa_news (code, title, url, source, media, "
                    "publish_time, sentiment) "
                    "VALUES ('',%s,%s,'mp',%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                    (title, r["url"], (r.get("mp_name") or "")[:120], pub, senti))
                new_row = bool(cur.rowcount)
                stat["inserted"] += int(new_row)
                codes = []
                if index:
                    import stock_discovery as sd
                    # ⚠️ 扫**全文**，不截断（2026-10-12 实测修的召回 bug）：
                    # 原来只扫 body[:6000]，而券商策略/研报类长文正文 8k~20k，
                    # 实测 8 篇「明明提到了持仓股」的公众号文章里 **7 篇**的持仓股
                    # 出现在第 7725~20438 字符处 —— 全被切掉了，于是那篇文章
                    # 关联到了别的股票、唯独没关联到真正提到的那只（招商银行
                    # naive 命中 7 篇、实际只关联上 2 篇）。这里没有任何成本，
                    # 只是漏召回。
                    norm = sd._norm((title or "") + " " + (body_txt or ""))
                    for name in sd._find_names(norm, index):
                        codes.append(index[name])
                for code in codes:
                    cur.execute("INSERT INTO sa_news_related (url, code) "
                                "VALUES (%s,%s) ON CONFLICT (url, code) DO NOTHING",
                                (r["url"], code))
                if codes:
                    stat["linked"] += 1
                else:
                    stat["unlinked"] += 1
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    print("[mp] 推入 sa_news：新增 %d 条，关联到股票的文章 %d 篇"
          "（丢弃 无真值时间 %d / 超%d天 %d）"
          % (stat["inserted"], stat["linked"], stat["skipped_no_time"],
             max_age_days, stat["skipped_old"]), flush=True)
    return stat


# ---------------- LLM 相关性过滤（题材级关联，公司名匹配抓不到的那部分） ----------------

RELEVANCE_SYSTEM = (
    "你是 A 股 / 港股研究助理。给你若干条财经公众号文章的标题和开头，以及一份"
    "「关注股票清单」。逐条判断这篇文章**与清单里哪些股票直接相关**。\n"
    "算相关的口径：\n"
    "  (1) 文章直接点名该公司；\n"
    "  (2) 文章讲该公司主营业务/产品本身（业绩、订单、产品发布、产能）；\n"
    "  (3) 文章讲该公司所处产业链的上游或下游环节，且该环节的变动对它是实质影响；\n"
    "  (4) 文章讲某个行业或题材，而清单里该股是这个题材/赛道的主要标的。\n"
    "不算相关的口径：\n"
    "  · 大盘点评、宏观政策解读、纯资金流/情绪统计，与清单里任何一只都没实质关系；\n"
    "  · 只因为「都属于科技/消费/医药」这种宽泛行业就算相关（太松，宁可漏）；\n"
    "  · 提到公司但只是顺带一句、无实质信息。\n"
    "**宁缺毋滥**：拿不准就不输出。\n"
    "输出格式（严格照抄，不要 markdown、不要解释、不要写别的字）：\n"
    "  <文章序号>:<逗号分隔的股票代码>\n"
    "没有任何相关股票的那篇输出 <文章序号>:NONE\n"
    "每篇最多 %d 个代码。"
)


def _candidate_universe(cur, limit: int = 80) -> list[tuple[str, str]]:
    """候选池 = 自选 + 持仓（**只在这份清单里判关联**）。

    为什么不给全市场 5640 只：prompt 会长到没法判，模型也会开始幻觉——
    而决策只关心自选/持仓。名册里查不到名字的（港股/ETF）就只给代码。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        cur.execute("SELECT code, name FROM sa_watchlist ORDER BY code")
        for code, name in cur.fetchall():
            if code and code not in seen:
                seen.add(code)
                out.append((code, (name or "").strip()))
    except Exception:
        pass
    try:
        cur.execute("SELECT code, sum(CASE WHEN side='buy' THEN shares "
                    "ELSE -shares END) s FROM sa_paper_trades "
                    "WHERE status <> 'skipped' GROUP BY code")
        held = [r[0] for r in cur.fetchall() if r[1] and r[1] > 0]
    except Exception:
        held = []
    missing = [c for c in held if c not in seen]
    if missing:
        try:
            cur.execute("SELECT code, name FROM sa_stock_roster "
                        "WHERE code = ANY(%s)", (missing,))
            for code, name in cur.fetchall():
                seen.add(code)
                out.append((code, (name or "").strip()))
        except Exception:
            pass
        for c in missing:
            if c not in seen:
                seen.add(c)
                out.append((c, ""))
    return out[:limit]


def _parse_relevance(raw: str, n_items: int, allowed: set[str],
                     max_relations: int) -> dict[int, list[str]]:
    """解析 `<序号>:<代码,...>`，**代码必须命中候选白名单**否则丢弃。

    不用 JSON：模型不守格式时（少逗号、多个换行、混进解释文字）行式解析的
    容错高得多，而白名单兜住了「幻觉出不存在的代码」。
    """
    import re
    out: dict[int, list[str]] = {}
    for line in (raw or "").splitlines():
        m = re.match(r"\s*\[?(\d{1,3})\]?\s*[:：、]\s*(.+)$", line)
        if not m:
            continue
        idx = int(m.group(1))
        if not (0 <= idx < n_items):
            continue
        hits = []
        for code in re.findall(r"\d{5,6}", m.group(2)):
            if code in allowed and code not in hits:
                hits.append(code)
            if len(hits) >= max_relations:
                break
        if hits:
            out.setdefault(idx, []).extend(c for c in hits if c not in out[idx])
    return out


def llm_relate_to_holdings(deps, rows: list[dict], conf: dict | None = None,
                           max_age_days: int = 7) -> dict:
    """让 LLM 判断公众号文章与「自选+持仓」的相关性，结果写进 sa_news_related。

    为什么需要它：`_link_index` 只能匹配**公司名/代码**，而公众号大量文章讲的是
    行业与题材（「存储涨价」「白酒动销」「算力资本开支」）。实测 715 篇里 406 篇
    一条都没关联上，而这批恰恰是决策价值最高的券商策略/行业观点。

    失败**绝不阻断**主流程（同 publish_to_news）：LLM 挂了只是这批文章没关联上，
    抓取结果不该作废。写入用 ON CONFLICT DO NOTHING，重复跑幂等。
    """
    conf = conf or {}
    stat = {"n": 0, "calls": 0, "fail": 0, "articles_linked": 0,
            "relations": 0, "skipped_linked": 0, "dry_run": bool(conf.get("dry_run"))}
    rows = [r for r in rows if r.get("id") and r.get("url")]
    if not rows:
        return stat
    try:
        import llm_advisor as llm
    except Exception as exc:
        print("[mp] LLM 不可用，跳过相关性过滤：%s" % str(exc)[:120], flush=True)
        return stat
    llm_conf = llm.load_llm_conf()
    if not llm_conf.get("enabled") or not llm_conf.get("api_key"):
        print("[mp] LLM 未启用/无 key，跳过相关性过滤", flush=True)
        return stat
    try:
        batch = max(1, int(conf.get("batch") or 6))
        cap = max(1, int(conf.get("max_per_round") or 12))
        snippet = max(80, int(conf.get("snippet") or 300))
        max_rel = max(1, int(conf.get("max_relations") or 4))
        only_unlinked = bool(conf.get("only_unlinked", True))
    except (TypeError, ValueError):
        batch, cap, snippet, max_rel, only_unlinked = 6, 12, 300, 4, True

    conn = deps["get_conn"]()
    inserted: list[tuple[str, str]] = []
    try:
        with conn.cursor() as cur:
            cand = _candidate_universe(cur)
            if not cand:
                print("[mp] 候选池为空（自选与持仓都没读到），跳过相关性过滤", flush=True)
                return stat
            allowed = {c for c, _ in cand}
            cur.execute(
                "SELECT a.id, a.url, a.title, a.mp_name, "
                "       left(coalesce(nullif(a.content_text,''), a.summary, ''), %s) snip, "
                "       a.published_at, a.published_at_src, "
                "       EXISTS (SELECT 1 FROM sa_news_related r "
                "               WHERE r.url = a.url) AS linked "
                "  FROM sa_mp_articles a WHERE a.id = ANY(%s)", (snippet, [r["id"] for r in rows]))
            cols = [d[0] for d in cur.description]
            arts = [dict(zip(cols, r)) for r in cur.fetchall()]
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        pool = []
        for a in arts:
            if a.get("published_at_src") != "wx" or not a.get("published_at"):
                continue
            if a["published_at"] < cutoff:
                continue
            if only_unlinked and a.get("linked"):
                stat["skipped_linked"] += 1
                continue
            pool.append(a)
        stat["n"] = len(pool)
        pool = pool[:cap]
        stat["n"] = len(pool)
        if not pool:
            return stat
        cand_txt = "、".join(f"{c} {n}" if n else c for c, n in cand)
        base_msg = RELEVANCE_SYSTEM % max_rel
        # ⚠️ 按**文章 id** 累积，不能按「批内下标」：批内下标每批都从 0 开始，
        # 用它做 key 会让后一批的 0 覆盖前一批的 0 —— 实测 batch=1 跑 4 篇时
        # 只剩最后 1 篇的关联，前 3 篇全丢（离线测试抓到的）。
        found: dict[int, tuple[str, list[str]]] = {}
        for start in range(0, len(pool), batch):
            chunk = pool[start:start + batch]
            lines = []
            for i, a in enumerate(chunk):
                title = (a.get("title") or "").replace("\n", " ").strip()
                snip = (a.get("snip") or "").replace("\n", " ").strip()[:snippet]
                lines.append(f"{i}. 标题：{title}\n   开头：{snip}")
            user = (f"关注股票清单（共 {len(cand)} 只）：\n{cand_txt}\n\n"
                    f"文章（共 {len(chunk)} 篇，序号从 0 开始）：\n" + "\n".join(lines))
            stat["calls"] += 1
            try:
                raw = llm.ask(base_msg, user, conf=llm_conf, thinking=False,
                                 task="standard")
            except Exception as exc:
                stat["fail"] += 1
                print("[mp] 相关性判断第 %d 批失败：%s" % (start // batch + 1,
                                                     str(exc)[:150]), flush=True)
                continue
            for idx, codes in _parse_relevance(raw, len(chunk), allowed,
                                               max_rel).items():
                row = chunk[idx]
                prev = found.get(row["id"], (row["url"], []))[1]
                for c in codes:
                    if c not in prev:
                        prev.append(c)
                found[row["id"]] = (row["url"], prev)
        for aid in sorted(found):
            url, codes = found[aid]
            for code in codes:
                inserted.append((url, code))
            stat["articles_linked"] += 1
        stat["relations"] = len(inserted)
        if not stat["dry_run"] and inserted:
            with conn.cursor() as cur:
                for url, code in inserted:
                    cur.execute("INSERT INTO sa_news_related (url, code) "
                                "VALUES (%s,%s) ON CONFLICT (url, code) DO NOTHING",
                                (url, code))
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    print("[mp] LLM 相关性：处理 %d 篇（已关联的跳过 %d 篇），调用 %d 次失败 %d 次"
          " → 关联出 %d 篇 / %d 条关系%s"
          % (stat["n"], stat["skipped_linked"], stat["calls"], stat["fail"],
             stat["articles_linked"], stat["relations"],
             "（dry-run，未写库）" if stat["dry_run"] else ""), flush=True)
    return stat
