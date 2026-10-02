"""多渠道免费新闻抓取模块（全部免 key，零成本）。

渠道（2026-10-01 逐个实测的可用性，**别凭印象改这个表**）：

| 渠道 | 实测 | 说明 |
|---|---|---|
| ``eastmoney`` | ✅ 可用 | 东财公告接口 + 资讯搜索 JSONP，纯 JSON，0.2s。主力 |
| ``ak_em`` | ✅ 可用 | akshare ``stock_news_em``，个股新闻**带真实发布时间**，0.3s |
| ``baidu`` | ✅ 需移动 UA | 桌面 UA 会被 WAF 302 到 ``wappass.baidu.com`` 图形验证码（详见 fetch_baidu） |
| ``sina`` | ✅ 可用 | 新浪财经滚动 JSON，按股票名过滤 |
| ``bing`` | ⚠️ 价值低 | 网页 RSS 可用，但中文财经查询多是行情页/官网，非新闻；``/news/search`` 返回 0 条 |
| ``searxng`` | ✅ 可用 | 101 上自建 SearXNG，实测能出真新闻（360search/sogou/yandex）。url 留空即关闭 |
| ``duckduckgo`` | ❌ 已死 | 自 2026-09-23 起本机与 101 均 TCP 443 超时。**默认关闭**，保留仅为将来解锁 |

DDG 为什么被替换（三个渠道的实测对比，见 docs）：
- DDG：本机不通，且超时 30s × 36 只 = 一轮白等 18 分钟。
- 必应：活着，但 ``/search?format=rss`` 对「贵州茅台」返回的是东财行情页和茅台官网；
  ``/news/search?format=rss`` 直接返回 0 条（HTML 也是 15KB 空壳）。当兜底可以，当主力不行。
- SearXNG：实测 24~31 条/查询，且出的是网易/新浪/搜狐的真新闻。

用法：
    python news_fetcher.py                      # 抓全部自选股
    python news_fetcher.py 600519 贵州茅台      # 单股单跑（验证用）
    python news_fetcher.py --health             # 看各渠道健康度/冷却状态
"""

import io
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}

# 百度专用 UA：必须是**移动端**。
# 实测 2026-10-01（桌面 UA vs iPhone UA，同一 URL 同一参数）：
#   桌面 UA -> HTTP 302 跳 wappass.baidu.com 图形验证码页（1488 字节，0 结果）
#   iPhone UA -> 582250 字节，10 个 h3，含 28 处时间字样，无验证码
# 所以百度这一路不能用 UA 里的桌面 UA —— 见 fetch_baidu 里的反爬判据注释。
BAIDU_UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                          "Mobile/15E148 Safari/604.1",
            "Accept-Language": "zh-CN,zh;q=0.9"}

# ---------------- 配置加载（无 pyyaml 时回退到简易解析） ----------------

DEFAULT_CONF = {
    "channels": {
        "eastmoney": {"enabled": True, "min_interval": 1.0},
        "ak_em": {"enabled": True, "min_interval": 1.5},
        "baidu": {"enabled": True, "min_interval": 5.0},
        "sina": {"enabled": True, "min_interval": 2.0},
        # bing 默认关闭：实测入库的 230 条全是官网/行情页噪声，见 fetch_bing 的 docstring
        "bing": {"enabled": False, "min_interval": 3.0},
        # url 留空 = 关闭。填 SearXNG 的根地址（末尾不要带 /search）：
        #   本机装：        http://127.0.0.1:8888
        #   101（需放行安全组 8888）：http://101.43.25.101:8888
        "searxng": {"enabled": False, "min_interval": 2.0, "timeout": 25, "url": ""},
        # DDG 已死（见文件头），默认关闭；留着开关是为了哪天网络通了能直接开回来
        "duckduckgo": {"enabled": False, "min_interval": 5.0, "timeout": 8},
    },
    "fetch_interval_minutes": 60,
    "items_per_query": 10,
    "keywords_extra": [],
    # 渠道健康度：连续失败多少次后进入冷却，冷却多少分钟
    "health_fail_threshold": 3,
    "health_cooldown_minutes": 30,
}


def load_config() -> dict:
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    if not text:
        return DEFAULT_CONF
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        news = data.get("news") or {}
    except ImportError:
        news = _parse_simple(text)
    conf = json.loads(json.dumps(DEFAULT_CONF))  # deep copy
    for key in ("fetch_interval_minutes", "items_per_query",
                "health_fail_threshold", "health_cooldown_minutes"):
        if news.get(key) is not None:
            conf[key] = int(news[key])
    if news.get("keywords_extra") is not None:
        conf["keywords_extra"] = list(news["keywords_extra"])
    # channels 逐个**合并**而不是整体 update：
    # DEFAULT_CONF 里的新渠道（bing / searxng / ak_em）即使用户 config.yaml 里
    # 没写，也要拿到自己的默认参数 —— 否则 searxng 会拿到 {}，连 url 键都不存在。
    merged = json.loads(json.dumps(DEFAULT_CONF["channels"]))
    for name, ch in (news.get("channels") or {}).items():
        if isinstance(ch, dict):
            merged.setdefault(name, {}).update(ch)
        else:                       # 老写法：只写了 true/false
            merged.setdefault(name, {})["enabled"] = bool(ch)
    conf["channels"] = merged
    return conf


def _parse_simple(text: str) -> dict:
    """无 pyyaml 时的兜底：只认 2 空格缩进的 news: 块内扁平 key=value。"""
    news, in_block = {}, False
    for line in text.splitlines():
        if line.startswith("news:"):
            in_block = True
            continue
        if in_block and line and not line.startswith("  "):
            in_block = False
        if in_block and ":" in line and "#" not in line.split(":")[0]:
            key, _, value = line.strip().partition(":")
            value = value.strip()
            if value in ("true", "false"):
                news[key] = value == "true"
            elif value.isdigit():
                news[key] = int(value)
    return {k: v for k, v in news.items() if not isinstance(v, bool)} or {}


# ---------------- 渠道限速 ----------------

_last_request: dict[str, float] = {}
_speed_lock = threading.Lock()


def _rate_limit(channel: str, min_interval: float) -> None:
    with _speed_lock:
        last = _last_request.get(channel, 0.0)
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_request[channel] = time.monotonic()


# ---------------- 渠道健康度：连续失败自动冷却 ----------------
#
# 为什么需要（2026-10-01 排查 DDG 时暴露的问题）
# ----------------------------------------------------
# DDG 自 2026-09-23 起本机 TCP 443 全超时，但它是 `enabled: true`，
# 于是每一轮、每只股票、每个关键词都在白等 `timeout=30` 秒。
# 36 只票 × 2 轮 = 一轮光等就 30 分钟以上，而抓取周期才 30 分钟 ——
# 结果是**永远追不上，且没有任何报错**（fetch_duckduckgo 里的 `except: return []`
# 把异常吞了，调用方只看到「这个渠道 0 条」，分不清是「没搜到」还是「根本不通」）。
#
# 修法：记住每个渠道的连续失败数，达到阈值就在冷却期内**直接跳过**，
# 同时把「为什么跳过、错是什么」暴露到 /api/news/status 和 --health 输出里。
# 「HTTP 200 但 0 条」不算失败（那是正常的空结果），只有明确的
# 连接异常 / 反爬拦截 / 解析失败才计数 —— 这一点很重要，否则
# 「今天恰好没新闻」会让渠道被误伤进冷却。

_health: dict[str, dict] = {}
_health_lock = threading.Lock()


def _health_get(channel: str) -> dict:
    with _health_lock:
        return dict(_health.get(channel) or {"fail": 0, "ok": 0,
                                             "until": 0.0, "last_err": "",
                                             "last_ms": 0})


def _mark_ok(channel: str, ms: int = 0) -> None:
    with _health_lock:
        h = _health.setdefault(channel, {"fail": 0, "ok": 0, "until": 0.0,
                                         "last_err": "", "last_ms": 0})
        h["ok"] += 1
        h["fail"] = 0
        h["until"] = 0.0
        h["last_err"] = ""
        if ms:
            h["last_ms"] = ms


def _mark_fail(channel: str, err: str, ms: int = 0,
               threshold: int = 3, cooldown_minutes: int = 30) -> None:
    """记录一次**明确的失败**（连接异常 / 反爬 / 解析失败）。

    连续失败到阈值就进入冷却。注意：调用方要区分「0 条结果」和「抓不到」，
    前者走 _mark_ok。
    """
    with _health_lock:
        h = _health.setdefault(channel, {"fail": 0, "ok": 0, "until": 0.0,
                                         "last_err": "", "last_ms": 0})
        h["fail"] += 1
        h["last_err"] = (err or "")[:160]
        h["last_ms"] = ms
        if h["fail"] >= threshold:
            h["until"] = time.monotonic() + max(1, cooldown_minutes) * 60
            h["fail"] = 0


def _channel_ready(channel: str, conf: dict) -> tuple[bool, str]:
    """渠道现在该不该跑。返回 (可跑, 不可跑的原因)。"""
    h = _health_get(channel)
    if h["until"] and time.monotonic() < h["until"]:
        left = int(h["until"] - time.monotonic())
        return False, f"冷却中（剩 {left // 60 + 1} 分钟，上次错：{h['last_err'][:60]}）"
    return True, ""


def health_snapshot(conf: dict | None = None) -> dict:
    """各渠道健康度，供 /api/news/status 与 `--health` 用。"""
    conf = conf or load_config()
    out = {}
    for name in FETCHERS:
        ch = conf["channels"].get(name) or {}
        h = _health_get(name)
        out[name] = {
            "enabled": bool(ch.get("enabled")),
            "ok": h["ok"], "fail": h["fail"],
            "cooling": bool(h["until"] and time.monotonic() < h["until"]),
            "cooldown_left_sec": (max(0, int(h["until"] - time.monotonic()))
                                  if h["until"] else 0),
            "last_err": h["last_err"],
            "last_ms": h["last_ms"],
        }
    return out


# ---------------- 抓取进度（抓 30 分钟，只有「抓取中」三个字太没用）----------------
#
# 一轮要 36 只票 × (1 + 关键词轮) × 6 个渠道 = 几百次请求，实测 4~10 分钟。
# 期间前端只有一句「抓取中…」，看不出是在干活还是卡死 —— 曾经就是因为
# DDG 每只白等 30 秒，日志里全是 timeout，但界面上完全看不出来。
#
# 现在暴露：已抓 N/总数、当前标的、当前渠道、单渠道累计耗时。
# 抓取器不需要知道自己「正在被观察」，每个 fetch_* 内部自己记一下即可。

_PROGRESS: dict = {}
_progress_lock = threading.Lock()


def get_progress() -> dict:
    with _progress_lock:
        return dict(_PROGRESS)


def reset_progress() -> None:
    """清空进度。/api/news/fetch 刚触发时调，好让前端立刻看到「0/0 启动中」，
    而不是继续显示上一轮跑完的进度（那会让人以为已经抓完了）。"""
    with _progress_lock:
        _PROGRESS.clear()


def _prog_start(total: int, label: str) -> None:
    with _progress_lock:
        _PROGRESS.clear()
        _PROGRESS.update({
            "running": True, "label": label, "done": 0, "total": total,
            "current": "", "channel": "", "started_at": time.time(),
            "elapsed": 0, "got": 0, "channel_ms": {}, "skipped": {},
        })


def _prog_step(current: str = "", channel: str = "", done: int | None = None,
               total: int | None = None, got: int | None = None,
               ms: int = 0, skip: str = "") -> None:
    """更新进度。字段含义：

    - done/total   : 已完成 / 总数（个股轮是股票数，主题轮是主题词数）
    - current      : 当前处理的标的（股票名(代码) 或 [分组] 主题词）
    - channel      : 正在跑的渠道
    - got          : **本轮当前标的**已抓到的条数（不是累计）——
                     fetch_for_stock 每只股票新建 results 列表，
                     所以这里是单股计数；累计数看入库后的 total_new
    - channel_ms   : 渠道 -> 累计毫秒，用来定位「到底哪个渠道最慢/卡住」
    - skipped      : 跳过原因 -> 次数，最常见是健康度冷却
    """
    with _progress_lock:
        if current:
            _PROGRESS["current"] = current
        if channel:
            _PROGRESS["channel"] = channel
        if done is not None:
            _PROGRESS["done"] = done
        if total is not None:
            _PROGRESS["total"] = total
        if got is not None:
            _PROGRESS["got"] = got
        if ms:
            cms = _PROGRESS.setdefault("channel_ms", {})
            cms[channel] = cms.get(channel, 0) + ms
        if skip:
            sk = _PROGRESS.setdefault("skipped", {})
            sk[skip] = sk.get(skip, 0) + 1
        if _PROGRESS.get("started_at"):
            _PROGRESS["elapsed"] = int(time.time() - _PROGRESS["started_at"])


def _prog_finish() -> None:
    with _progress_lock:
        _PROGRESS["running"] = False
        _PROGRESS["current"] = ""
        _PROGRESS["channel"] = ""
        if _PROGRESS.get("started_at"):
            _PROGRESS["elapsed"] = int(time.time() - _PROGRESS["started_at"])


# ---------------- 各渠道实现（返回统一结构的 list） ----------------

SOURCE_LABEL = {
    "eastmoney": "东方财富", "baidu": "百度新闻", "sina": "新浪财经",
    "duckduckgo": "DuckDuckGo", "bing": "必应", "searxng": "SearXNG",
    "ak_em": "东财个股新闻",
}

# 搜索结果里「明显不是新闻」的链接（行情页/官网首页/百科）。
# 搜索引擎对「贵州茅台」这类词会大量返回这些，混进 sa_news 就是纯噪声。
_NOT_NEWS_HOSTS = (
    "quote.eastmoney.com", "push2.eastmoney.com", "data.eastmoney.com",
    "baike.baidu.com", "xueqiu.com", "10jqka.com.cn/quote",
    "moutaichina.com", "moutai.com.cn", "guba.eastmoney.com",
)
_NOT_NEWS_TITLE = ("_最新价格_", "行情_走势图", "百度百科", "官网", "股吧")


# akshare 是重包，**首次 import 要 8~9 秒**。原来在 fetch_ak_em 里 import，
# 于是第一次调用会报出「这个渠道 9.2s」，而 API 本身只要 0.15s ——
# 看起来像渠道慢，其实是一次性 import 开销，排查时白白绕了半天。
# 所以这里显式缓存成单例，代价只付一次。
_AK = None


def _akshare():
    global _AK
    if _AK is None:
        import akshare
        _AK = akshare
    return _AK


# ---------------- 数据库连接（**不要**改成 `from app import get_conn`）----------------
#
# AGENTS.md 记的教训：`from app import get_conn` 会 import 整个 app.py，
# 连带启动所有 daemon（iLink 会话、holiday 日历构建…），实测光这一步 46.8 秒。
# 而且 news_fetcher 是被 app.py 导入的，循环导入还会让两边各起一套连接。
# 所以这里照 ipo_quota._load_db_cfg 的模式：自己读仓库根 .env 建连接。
#
# 注意路径：本文件在 stock-advisor/ 下，往上一级才是仓库根（.env 在那）。

def _load_db_cfg() -> dict:
    cfg: dict[str, str] = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, ".env")
    if not os.path.exists(p):
        return cfg
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def _get_conn():
    """新建一个 psycopg2 连接（带重试）。

    重试是抄 app.get_conn 的：云库同实例还跑着 dify，负载波动会偶发建连超时。
    """
    import psycopg2
    cfg = _load_db_cfg()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return psycopg2.connect(
                host=cfg.get("DB_HOST"), port=int(cfg.get("DB_PORT", 5432)),
                user=cfg.get("DB_USERNAME"), password=cfg.get("DB_PASSWORD"),
                dbname=cfg.get("DB_DATABASE"), connect_timeout=45)
        except psycopg2.OperationalError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise last_exc


@contextmanager
def _db():
    """数据库连接 contextmanager，**保证 commit + close**。

    为什么需要它：psycopg2 的连接对象自己当 context manager 用时，
    退出只 commit/rollback，**不 close**。原来 `with get_conn() as conn`
    的写法每轮入库都泄漏一个连接，长跑会耗尽连接数。

    ⚠️ **必须 commit**：psycopg2 默认 autocommit=False，INSERT 执行后
    如果不 commit，连接 close 时会 rollback。实测踩过 —— `save_to_db`
    返回 5（rowcount=5）但数据库里 0 条，因为事务被 rollback 了。
    """
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _norm(code: str, title: str, url: str, source: str,
          media: str = "", publish_time=None) -> dict:
    return {
        "code": code,
        "title": re.sub(r"<[^>]+>", "", title or "").strip(),
        "url": url,
        "source": source,
        "media": media or SOURCE_LABEL.get(source, source),
        "publish_time": publish_time,   # ISO 字符串或 None
    }


# ---------------- 相对时间 → 绝对 ISO（百度新闻只给「6小时前」这种） ----------------
#
# 百度新闻搜索的时间是**相对表述**（`6小时前` / `昨天16:23` / `2026年9月30日`），
# 而 sa_news.publish_time 存的是 timestamp，直接塞相对字符串没法排序、
# 也没法算「近 24 小时的新消息」。所以必须在这里换算成绝对时间。
#
# 换算不了的**一律返回 None**，不猜、不填默认值 —— 错的比没有更糟：
# 下游 paper_trading 会拿发布时间筛时效，填错时间等于凭空造出时效。

_REL_PATTERNS = [
    (re.compile(r"^(\d+)\s*分钟前$"), lambda m, now: now - timedelta(minutes=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*小时前$"), lambda m, now: now - timedelta(hours=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*天前$"), lambda m, now: now - timedelta(days=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*分钟前"), lambda m, now: now - timedelta(minutes=int(m.group(1)))),
]


def _cn_relative_to_iso(text: str, now: datetime | None = None) -> str | None:
    """中文/相对时间表述 → ISO 字符串；认不出来返回 None。

    支持：`刚刚`、`6小时前`、`8天前`、`今天HH:MM`、`昨天HH:MM`、`前天HH:MM`、
    `HH:MM`（今天）、`YYYY年M月D日`、`YYYY年M月D日HH:MM`、`YYYY-MM-DD HH:MM[:SS]`。
    """
    if not text:
        return None
    now = now or datetime.now()
    s = re.sub(r"\s+", " ", str(text)).strip()
    # 去掉百度偶尔带的前缀，如「发布于：」
    s = re.sub(r"^发布于[:：]\s*", "", s)

    if s in ("刚刚", "刚才", "just now"):
        return now.isoformat(timespec="seconds")
    for pat, fn in _REL_PATTERNS:
        m = pat.match(s)
        if m:
            return fn(m, now).isoformat(timespec="seconds")

    m = re.match(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$", s)
    if m:
        days = {"今天": 0, "昨天": 1, "前天": 2}[m.group(1)]
        base = (now - timedelta(days=days)).replace(
            hour=int(m.group(2)), minute=int(m.group(3)),
            second=0, microsecond=0)
        # 「今天26:30」不可能，说明是明天或数据有误，宁可不给
        return None if base > now + timedelta(hours=1) else base.isoformat(timespec="seconds")

    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        try:
            return datetime(y, mo, d, hh, mm).isoformat(timespec="seconds")
        except ValueError:
            return None

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T]?(\d{1,2})?:?(\d{2})?:?(\d{2})?$", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4) or 0), int(m.group(5) or 0),
                            int(m.group(6) or 0)).isoformat(timespec="seconds")
        except ValueError:
            return None

    # 裸 HH:MM（百度结果里出现过），按今天算；跨天的给不出日期就不给
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                           second=0, microsecond=0).isoformat(timespec="seconds")
    return None


def _looks_like_news(url: str, title: str) -> bool:
    """粗筛：排除行情页/官网/百科这类「搜词命中但不是新闻」的链接。"""
    low = (url or "").lower()
    if any(h in low for h in _NOT_NEWS_HOSTS):
        return False
    t = title or ""
    if any(k in t for k in _NOT_NEWS_TITLE):
        return False
    # yandex 兜底结果常把裸域名当标题（实测 "dq0mkyuy01u5o.cloudfront.net"）
    if re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", t.strip().lower()):
        return False
    # yandex 另一种兜底：标题是 URL 本身（实测 "post.smzzm.com/p/a03dod70"、
    # "finance.sina.com.cn/"）。特征是「以域名开头 + 含路径分隔符」。
    # 真新闻标题不会长这样（顶多含 "A/B" 这种短斜杠，不会以域名开头）。
    if re.match(r"^[a-z0-9.\-]+\.[a-z]{2,}/", t.strip().lower()):
        return False
    return True


def fetch_eastmoney(code: str, name: str, limit: int, conf: dict,
                    keyword: str = "") -> list[dict]:
    """东财公告 + 资讯搜索，两个接口合一渠道。港股(5位)无 A 股公告接口，走资讯搜索。

    keyword 非空时为"自定义搜索词"模式（竞品动态等）：只跑资讯搜索，不跑公告。

    公告接口失败**不计入健康度**（它对港股/次新股本来就 400，是结构性缺失
    不是源坏了）；只有资讯搜索这一路失败才算渠道故障。
    """
    items: list[dict] = []
    ch_conf = conf["channels"]["eastmoney"]
    is_hk = re.fullmatch(r"\d{5}", code) is not None
    query = keyword or name
    t0 = time.time()
    if not is_hk and not keyword:  # 公告接口仅支持 A 股，且只按股票名跑
        _rate_limit("eastmoney", ch_conf.get("min_interval", 1.0))
        try:
            r = requests.get(
                "https://np-anotice-stock.eastmoney.com/api/security/ann",
                params={"sr": -1, "page_size": limit, "page_index": 1,
                        "ann_type": "A", "stock_list": code},
                headers=UA, timeout=10)
            r.raise_for_status()
            for a in (r.json().get("data") or {}).get("list") or []:
                art = a.get("art_code", "")
                items.append(_norm(
                    code, a.get("title", ""),
                    f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
                    if art else a.get("url", ""),
                    "eastmoney",
                    a.get("columns") and a["columns"][0].get("name") or "公告",
                    a.get("notice_date")))
        except Exception:
            pass    # 结构性缺失，不计健康度（见 docstring）
    _rate_limit("eastmoney", ch_conf.get("min_interval", 1.0))
    try:  # 资讯搜索（JSONP）
        param = {"uid": "", "keyword": query, "type": ["cmsArticleWebOld"],
                 "client": "web", "clientVersion": "curr", "clientType": "web",
                 "param": {"cmsArticleWebOld": {"searchScope": "default",
                                                "sort": "time", "pageIndex": 1,
                                                "pageSize": limit,
                                                "preTag": "", "postTag": ""}}}
        r = requests.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "jQuery_1", "param": json.dumps(param, ensure_ascii=False)},
            headers=UA, timeout=10)
        r.raise_for_status()
        payload = json.loads(re.sub(r"^jQuery_\d*\(|\)$", "", r.text))
        for art in ((payload.get("result") or {}).get("cmsArticleWebOld") or []):
            items.append(_norm(code, art.get("title", ""), art.get("url", ""),
                               "eastmoney", art.get("mediaName", ""),
                               art.get("date")))
        _mark_ok("eastmoney", ms=int((time.time() - t0) * 1000))
    except Exception as exc:
        _mark_fail("eastmoney", f"资讯搜索 {type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
    return items


def fetch_baidu(code: str, name: str, limit: int, conf: dict,
                keyword: str = "") -> list[dict]:
    """百度新闻垂直搜索（tn=news&rtt=4 按时间排序），**带发布时间**。

    两个必须知道的坑（都是 2026-10-01 实测出来的，改代码前先读）：

    1. **必须用移动端 UA**。同一 URL 同一参数：
       桌面 Chrome UA -> 302 跳 wappass.baidu.com 图形验证码（1488 字节 0 结果）；
       iPhone UA      -> 582250 字节、10 个 h3、28 处时间字样、无验证码。

    2. **反爬判据不能用「页面里有没有『百度安全验证』这五个字」**。
       百度跳验证码页时 requests 默认跟随跳转，最后拿到的就是那个页面；
       但 requests 对无 charset 的 text/html 会猜 ISO-8859-1，中文全成乱码，
       所以 `"百度安全验证" in r.text` **恒为 False**（实测 r.encoding=ISO-8859-1 → False，
       按 utf-8 解 → True）。原实现因此把反爬当成了「今天没新闻」，
       静默返回 []，排查了两天才发现百度其实早就被拦了。
       现在用**两个不依赖中文文本**的判据：最终 host 是不是 wappass.baidu.com、
       以及按 utf-8 解后再查文本。

    3. **百度会分阶段封禁**。实测一轮 36 只票 × 1 次请求（间隔 3s，约 108s 连续请求）
       后开始跳验证码；被拦期间再密集探测会让封禁延长。所以 min_interval 提到 5s，
       且健康度冷却会在拦到后立刻跳过整轮 —— 不是「继续捶到恢复」，
       和 push2ex 那个「每 5 分钟捶 12 次导致永久续期」的坑是同一类错误。
    """
    ch_conf = conf["channels"]["baidu"]
    ready, why = _channel_ready("baidu", conf)
    if not ready:
        return []
    _rate_limit("baidu", ch_conf.get("min_interval", 3.0))
    t0 = time.time()
    # 时间范围：只搜最近一周。百度的 gpc 参数格式是 stf=<start>,<end>（Unix 时间戳）。
    # 不加的话返回的旧新闻会占满结果（实测「贵州茅台」返回 2024 年的旧文）。
    now = int(time.time())
    week_ago = now - 7 * 24 * 3600
    try:
        r = requests.get(
            "https://www.baidu.com/s",
            params={"wd": keyword or name, "tn": "news", "rtt": 4,
                    "gpc": f"stf={week_ago},{now}"},
            headers=BAIDU_UA, timeout=10)
        r.raise_for_status()
        # 编码：尊重服务端声明，只在**它什么都没给**时才猜 utf-8。
        #
        # 为什么不能无脑 `r.encoding = "utf-8"`：
        #   - 验证码页没有 charset，requests 猜 ISO-8859-1 → 中文乱码 →
        #     `"百度安全验证" in r.text` 恒 False（这个必须靠 utf-8 才判得出）
        #   - 但**正常结果页**里百度会按客户端/UA 声明不同 charset，
        #     强行按 utf-8 解会把部分 `发布于：8月4日` 之类的值解成乱码，
        #     于是发布时间换算 4/10 失败（实测症状）。
        # 两个诉求方向相反，所以只能按「声明优先、缺失才猜」处理。
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = "utf-8"
        if "wappass.baidu.com" in urlparse(r.url).netloc or "百度安全验证" in r.text:
            _mark_fail("baidu", "反爬：跳 wappass.baidu.com 图形验证码",
                       ms=int((time.time() - t0) * 1000),
                       threshold=conf.get("health_fail_threshold", 3),
                       cooldown_minutes=conf.get("health_cooldown_minutes", 30))
            return []
        html = r.text
        items: list[dict] = []
        # 结果块以 <h3 class="news-title_XXXX"> 开始（class 后缀是每次随机生成的 hash，
        # 所以只匹配前缀 news-title）。每块的结构（实测 dump）：
        #   h3 前：<!-- {...,"sourceName":"考古学家","dispTime":"6小时前"} -->  ← 结构化数据
        #   h3 内：<a href="真链接"><em>高亮词</em>标题</a>
        #   h3 后：<span class="c-color-gray2 ..." aria-label="发布于：6小时前">6小时前</span>
        # 用 split 而不是 findall：块之间有嵌套 div，任何非贪婪正则都会截断或吞掉相邻块。
        chunks = re.split(r'<h3[^>]*class="news-title', html)[1:]
        for chunk in chunks:
            am = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
            if not am:
                continue
            url = unquote(am.group(1).replace("&amp;", "&"))
            title = re.sub(r"<[^>]+>", "", am.group(2)).strip()
            if not (title and url.startswith("http")):
                continue
            if not _looks_like_news(url, title):
                continue
            # 发布时间：优先 aria-label（百度自己标注的），退到注释 JSON 的 dispTime
            tm = re.search(r'aria-label="发布于：([^"]+)"', chunk)
            when = _cn_relative_to_iso(tm.group(1)) if tm else None
            if not when:
                dm = re.search(r'"dispTime"\s*:\s*"([^"]+)"', chunk)
                when = _cn_relative_to_iso(dm.group(1)) if dm else None
            # 媒体名同样优先用 aria-label，注释 JSON 的 sourceName 兜底
            media = ""
            sm = re.search(r'aria-label="新闻来源：([^"]+)"', chunk)
            if sm:
                media = sm.group(1).strip()
            if not media:
                jm = re.search(r'"sourceName"\s*:\s*"([^"]*)"', chunk)
                media = (jm.group(1).strip() if jm else "")
            items.append(_norm(code, title, url, "baidu", media, when))
            if len(items) >= limit:
                break
        _mark_ok("baidu", ms=int((time.time() - t0) * 1000))
        return items
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("baidu", f"{type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []


def fetch_sina(code: str, name: str, limit: int, conf: dict,
               keyword: str = "") -> list[dict]:
    """新浪财经滚动新闻：拉多页财经流，按股票名/简称过滤标题。

    自定义搜索词模式（keyword 非空）同样只做标题过滤——滚动流是全市场混排，
    竞品关键词（如"OpenAI"）命中率不高，但零成本顺带扫一遍。
    """
    ch_conf = conf["channels"]["sina"]
    ready, _why = _channel_ready("sina", conf)
    if not ready:
        return []
    _rate_limit("sina", ch_conf.get("min_interval", 2.0))
    # 新浪滚动流是全市场混排，"贵州茅台"全名命中太苛刻；改用短简称集合匹配
    short_names = {name, name.replace("贵州", "").replace("股份", "")}
    if name.startswith(("ST", "*")):
        short_names.add(name.lstrip("*ST"))
    if keyword:
        short_names = {keyword}
    t0 = time.time()
    try:
        # 滚动流是全市场混排，单页 50 条命中率低；拉 3 页提高命中
        items = []
        for page in (1, 2, 3):
            r = requests.get(
                "https://feed.mix.sina.com.cn/api/roll/get",
                params={"pageid": 153, "lid": 2509, "k": "", "num": 50, "page": page},
                headers=UA, timeout=10)
            r.raise_for_status()
            for art in (r.json().get("result") or {}).get("data") or []:
                title = art.get("title", "")
                if not any(kw in title for kw in short_names if kw):
                    continue
                ts = art.get("ctime") or art.get("intime")
                publish = (datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
                           if ts else None)
                if art.get("url") and title not in {i["title"] for i in items}:
                    items.append(_norm(code, title, art.get("url", ""), "sina",
                                       art.get("media_name", ""), publish))
                if len(items) >= limit:
                    _mark_ok("sina", ms=int((time.time() - t0) * 1000))
                    return items
            _rate_limit("sina", ch_conf.get("min_interval", 2.0))
        _mark_ok("sina", ms=int((time.time() - t0) * 1000))
        return items
    except Exception as exc:
        _mark_fail("sina", f"{type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []


def fetch_ak_em(code: str, name: str, limit: int, conf: dict,
                keyword: str = "") -> list[dict]:
    """akshare ``stock_news_em``：东财个股新闻，**带真实发布时间**。

    为什么加这个渠道（2026-10-01 实测对比）：
    其它渠道都给不了可靠的发布时间 ——
      - 东财资讯搜索：``art.get("date")`` 实测常为空
      - 百度：只有「6小时前」这种相对表述，要自己换算
      - 必应 RSS：``<pubDate>`` 是**索引/抓取时间**，不是发稿时间
      - DDG：整个渠道已死
    而 ``stock_news_em`` 直接给 ``发布时间`` 列（``2026-09-30 16:23:00``），
    还带 ``文章来源``（媒体名）和 ``新闻链接``，0.3 秒一条请求，纯 JSON 不用解析 HTML。

    它只认**股票代码**（keyword 模式无意义），所以关键词轮直接跳过。
    """
    ch_conf = conf["channels"]["ak_em"]
    if not code or keyword:
        return []
    ready, _why = _channel_ready("ak_em", conf)
    if not ready:
        return []
    _rate_limit("ak_em", ch_conf.get("min_interval", 1.5))
    t0 = time.time()
    try:
        df = _akshare().stock_news_em(symbol=str(code))
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("ak_em", f"{type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []
    items: list[dict] = []
    try:
        for _, row in df.iterrows():
            title = re.sub(r"\s+", " ", str(row.get("新闻标题") or "")).strip()
            url = str(row.get("新闻链接") or "").strip()
            if not (title and url.startswith("http")):
                continue
            if not _looks_like_news(url, title):
                continue
            items.append(_norm(
                code, title, url, "ak_em",
                str(row.get("文章来源") or "").strip(),
                _cn_relative_to_iso(str(row.get("发布时间") or "").strip())))
            if len(items) >= limit:
                break
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("ak_em", f"解析失败 {type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []
    _mark_ok("ak_em", ms=int((time.time() - t0) * 1000))
    return items


def fetch_bing(code: str, name: str, limit: int, conf: dict,
               keyword: str = "") -> list[dict]:
    """必应网页 RSS（``/search?format=rss``）—— **实测是噪声源，默认关闭**。

    2026-10-01 实测结论（这是替换 DDG 的尝试，结论是「必应也不行」，如实记录）：
      - ``cn.bing.com/search?format=rss`` → 请求成功、10 条、**有** ``<pubDate>``
      - ``cn.bing.com/news/search?format=rss`` → **0 条**（HTML 也只有 15KB 空壳）
      - RSS 认 query（``贵州茅台`` → 茅台行情页，``月球基地`` → 月球基地），
        但中文财经词它只认「公司」不认「新闻」
      - **开着的后果**：一轮抓取入库 230 条，抽样全是智谱AI官网 / chatglm /
        莲花控股行情页 / 公司概况页，**没有一条是新闻**
    所以 config 默认 ``enabled: false``。实现保留下来是因为网络环境或
    引擎行为变化后可以随时开回来验证，但**在重新验证前不要开**。

    ``<pubDate>`` **不写进 publish_time**：它是搜索引擎的索引/抓取时间，不是发稿
    时间（实测 ``quote.eastmoney.com`` 行情页的 pubDate 就是页面刷新时间）。
    填进去等于凭空造出时效，比留 None 更坏。
    """
    ch_conf = conf["channels"]["bing"]
    ready, _why = _channel_ready("bing", conf)
    if not ready:
        return []
    _rate_limit("bing", ch_conf.get("min_interval", 3.0))
    t0 = time.time()
    query = keyword or name
    # 时间范围：只搜最近一周。必应的 filters 参数：
    #   ex1:"ez5_19869_19870" = 最近一周
    #   ex1:"ez5_19869_19871" = 最近 24 小时
    # 不加的话返回的旧新闻会占满结果。
    try:
        r = requests.get("https://cn.bing.com/search",
                         params={"q": query, "format": "rss",
                                 "count": max(10, limit),
                                 "filters": 'ex1:"ez5_19869_19870"'},
                         headers=UA, timeout=ch_conf.get("timeout", 12))
        r.raise_for_status()
        r.encoding = "utf-8"
        items: list[dict] = []
        for blk in re.findall(r"<item>(.*?)</item>", r.text, re.S):
            tm = re.search(r"<title>(.*?)</title>", blk, re.S)
            lm = re.search(r"<link>(.*?)</link>", blk, re.S)
            title = re.sub(r"\s+", " ", re.sub(
                r"<[^>]+>", "", tm.group(1) if tm else "")).strip()
            url = (lm.group(1) if lm else "").strip()
            if not (title and url.startswith("http")):
                continue
            if not _looks_like_news(url, title):
                continue
            # publish_time 留 None：见上面关于 pubDate 的说明
            items.append(_norm(code, title, url, "bing", ""))
            if len(items) >= limit:
                break
        _mark_ok("bing", ms=int((time.time() - t0) * 1000))
        return items
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("bing", f"{type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []


def fetch_searxng(code: str, name: str, limit: int, conf: dict,
                  keyword: str = "") -> list[dict]:
    """自建 SearXNG（101 上 ``/opt/searxng``）—— 目前唯一能出**真新闻**的搜索兜底。

    实测（2026-10-01，101）：中文查询出 24~31 条，来自 360search / sogou / yandex，
    内容是网易订阅 / 新浪财经 / 搜狐的涨停与个股新闻 —— 比必应（行情页为主）有用得多。
    bing / baidu 引擎在 SearXNG 里实测 0 条，已在 settings.yml 里禁用。

    ``channels.searxng.url`` 留空即关闭。注意 SearXNG 自身无鉴权，
    部署时只绑 127.0.0.1，别暴露公网（否则等于给别人开搜索代理）。
    """
    ch_conf = conf["channels"]["searxng"]
    base = (ch_conf.get("url") or "").strip().rstrip("/")
    if not base:
        return []
    ready, _why = _channel_ready("searxng", conf)
    if not ready:
        return []
    _rate_limit("searxng", ch_conf.get("min_interval", 2.0))
    t0 = time.time()
    query = f"{keyword} 新闻" if keyword else f"{name} 新闻"
    # 时间范围：只搜最近一周。SearXNG 的 time_range 参数：
    #   day=24h / week=7d / month=30d / year=365d
    # 不加的话返回的多是旧新闻（实测「贵州茅台」返回 2024 年的旧文）。
    time_range = ch_conf.get("time_range", "week")
    try:
        r = requests.get(base + "/search",
                         params={"q": query, "format": "json", "language": "zh-CN",
                                 "time_range": time_range},
                         headers=UA, timeout=ch_conf.get("timeout", 25))
        r.raise_for_status()
        data = r.json()
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("searxng", f"{type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []
    items: list[dict] = []
    try:
        for it in (data.get("results") or []):
            title = re.sub(r"<[^>]+>", "", str(it.get("title") or "")).strip()
            url = str(it.get("url") or "").strip()
            if not (title and url.startswith("http")):
                continue
            if not _looks_like_news(url, title):
                continue
            # SearXNG 的 publishedDate 常为 None（yandex 尤其），换算不出就 None
            when = _cn_relative_to_iso(str(it.get("publishedDate") or ""))
            engine = str(it.get("engine") or "").strip()
            items.append(_norm(code, title, url, "searxng",
                               f"SearXNG·{engine}" if engine else "", when))
            if len(items) >= limit:
                break
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("searxng", f"解析失败 {type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []
    _mark_ok("searxng", ms=int((time.time() - t0) * 1000))
    return items


def fetch_duckduckgo(code: str, name: str, limit: int, conf: dict,
                     keyword: str = "") -> list[dict]:
    """DDG Lite HTML 搜索 —— **已死**（2026-09-23 起本机与 101 均 TCP 443 超时）。

    保留实现只为将来网络环境变化时能直接开回来（config 里 enabled 默认 false）。
    超时从 30s 降到 8s：死源最贵的不是失败，而是每只票都白等满超时
    （36 只 × 2 轮 × 30s ≈ 一轮半小时，比抓取周期还长）。
    再加上健康度冷却后，连续失败 3 次就跳过整轮，误开也不会拖垮整体。
    """
    ch_conf = conf["channels"]["duckduckgo"]
    ready, _why = _channel_ready("duckduckgo", conf)
    if not ready:
        return []
    _rate_limit("duckduckgo", ch_conf.get("min_interval", 5.0))
    t0 = time.time()
    query = f"{keyword} 新闻" if keyword else f"{name} {code} 新闻"
    try:
        r = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers=UA,
            timeout=ch_conf.get("timeout", 8))
        r.raise_for_status()
        items = []
        seen_urls = set()
        # 只保留明确的"新闻列表/新闻文章"页（investing/雪球新闻等），其余行情页全部丢弃
        keep = ("-news", "news-", "/news", "moutai-news", "资讯", "新闻")
        noise = ("quote.eastmoney", "/realstock/", "xueqiu.com/S",
                 "vCB_AllMemordDetail", "MarketHistory", "vMS_MarketHistory")
        for m in re.finditer(r'<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
            url, raw_title = m.group(1), m.group(2)
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            if not title or len(title) < 8:
                continue
            if any(p in url for p in noise):
                continue
            if not any(p in url.lower() or p in title for p in keep):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            items.append(_norm(code, title, url, "duckduckgo"))
            if len(items) >= limit:
                break
        _mark_ok("duckduckgo", ms=int((time.time() - t0) * 1000))
        return items
    except Exception as exc:                                   # noqa: BLE001
        _mark_fail("duckduckgo", f"{type(exc).__name__}: {exc}",
                   ms=int((time.time() - t0) * 1000),
                   threshold=conf.get("health_fail_threshold", 3),
                   cooldown_minutes=conf.get("health_cooldown_minutes", 30))
        return []


FETCHERS = {
    "eastmoney": fetch_eastmoney,
    "ak_em": fetch_ak_em,
    "baidu": fetch_baidu,
    "sina": fetch_sina,
    "bing": fetch_bing,
    "searxng": fetch_searxng,
    "duckduckgo": fetch_duckduckgo,
}

# 关键词轮（中性/利好/利空词）不跑的渠道。
# 搜索类渠道单次 8~25s，关键词轮一轮可能有 5~10 个词 × 3 类，
# 跑全了整轮会到小时级 —— 比抓取周期还长，等于永远追不上。
# 个股轮照跑（那里才需要多一个厂商的独立视角）。
_KEYWORD_SKIP_CHANNELS = {"duckduckgo", "bing", "searxng"}


# ---------------- 聚合入口 ----------------

def fetch_for_stock(code: str, name: str, conf: dict | None = None,
                    keywords: list[str] | None = None,
                    keywords_pos: list[str] | None = None,
                    keywords_neg: list[str] | None = None) -> dict:
    """对一只股票跑所有启用渠道，返回 {code, name, results, errors}。

    keywords / keywords_pos / keywords_neg: 中性 / 利好 / 利空搜索词
    （逗号分隔存 sa_watchlist，中性=竞品动态等，利好/利空按方向打情绪标记）。
    每个关键词对每个渠道额外跑一轮（东财跳过公告、只搜资讯），命中即关联到该股。
    情绪标记规则：同一 URL 优先保留利空（利空值得先看到），仅标记入库不影响抓取。
    """
    conf = conf or load_config()
    limit = conf.get("items_per_query", 10)
    kw_limit = max(3, limit // 2)   # 关键词轮次取条数减半，控制总量
    results, errors = [], {}
    queries = [(name, "", "")]
    queries += [(kw, kw, "") for kw in (keywords or [])]
    queries += [(kw, kw, "pos") for kw in (keywords_pos or [])]
    queries += [(kw, kw, "neg") for kw in (keywords_neg or [])]
    for stock_name, keyword, senti in queries:
        for channel, fetcher in FETCHERS.items():
            ch_conf = conf["channels"].get(channel) or {}
            if not ch_conf.get("enabled", False):
                continue
            # 关键词轮跳过两个搜索渠道：它们要 8-25s 且多为聚合页，
            # 而关键词轮一多（中性+利好+利空）整轮时长远超抓取周期。
            # 竞品/情绪词靠东财资讯 + 百度 + ak_em 已经够。
            if keyword and channel in _KEYWORD_SKIP_CHANNELS:
                continue
            ready, why = _channel_ready(channel, conf)
            if not ready:
                _prog_step(skip=f"{channel}: {why}")
                continue
            _prog_step(channel=channel)
            t_ch = time.time()
            try:
                got = fetcher(code, stock_name, kw_limit if keyword else limit,
                              conf, keyword=keyword)
                if keyword:  # 标记来源关键词 + 情绪（去重后统计/入库用）
                    for it in got:
                        it["_kw"] = keyword
                        it["_senti"] = senti
                results.extend(got)
            except Exception as exc:
                errors[channel] = str(exc)
            _prog_step(channel=channel, got=len(results),
                        ms=int((time.time() - t_ch) * 1000))
    # 跨渠道按 URL 去重；情绪冲突时利空优先（利空新闻错过代价更高）
    seen, uniq = {}, []
    for item in results:
        url = item["url"]
        if not url:
            continue
        if url in seen:
            if item.get("_senti") == "neg":
                seen[url]["_senti"] = "neg"
        else:
            seen[url] = item
            uniq.append(item)
    return {"code": code, "name": name, "results": uniq, "errors": errors}


# ---------------- 情绪判定（2026-09-29）----------------
# 为什么加这个：原来 sentiment 只由「用哪个关键词搜到的」决定 —— 只有用
# keywords_pos 搜出来的才算利好、用 keywords_neg 搜出来的才算利空。而
# sa_watchlist 35 只票的 keywords_pos/keywords_neg **一个都没配**，于是
# 关键词轮压根不生成，入库 9852 条的 sentiment 全是空串，
# paper_trading._build_context 的 {'pos':'利好','neg':'利空'}.get(s or '', '中性')
# 于是 100% 兜底成「中性」——LLM 看到的是「新闻中性，未见明确利好利空」，
# 2724 条新闻的语义信息被整体丢弃，只剩标题。
#
# 现在改成**按标题文本判定**，与「用哪个 query 搜到的」解耦：
#   - 用词搜只影响抓取排序（利空词轮的结果照样入库）
#   - 打标看标题本身，9852 条历史数据可以一次性回填
# 词表偏利空是有依据的：2025 全年龙虎榜统计里，净卖出信号次日胜率 >50% 的
# 有 95/100 家，净买入只有 24/100 —— 利空的信息价值显著高于利好。
DEFAULT_SENTIMENT_NEG = [
    # 减持/解禁/质押（中金负面信号清单，统计上下行风险偏大）
    "减持", "解禁", "质押", "冻结", "清仓", "套现", "司法划转", "划转",
    # 监管（中金清单 + 四类函：关注函/问询函/警示函/监管函）
    "立案", "问询函", "关注函", "警示函", "监管函", "处罚", "违规", "违法",
    "调查", "问询", "警示", "责令", "整改", "谴责", "公开谴责", "异常波动",
    # 业绩暴雷
    "预亏", "亏损", "业绩下滑", "业绩预降", "商誉减值", "计提", "减记",
    "下修", "业绩变脸", "爆雷", "退市", "ST", "暂停上市", "财务造假",
    "由盈转亏", "净利下滑", "营收下滑", "转亏", "减产", "停产",
    # 经营/交易层面的负面
    "终止", "失败", "中止", "撤回", "诉讼", "仲裁", "停牌", "跌停", "暴跌",
    "下滑", "萎缩", "承压", "风险提示", "延期", "裁员", "欠薪", "失效",
    "辞职", "离任", "被动减持", "下调", "不及预期", "流拍",
    "大跌", "重挫", "闪崩", "跳水",
]
DEFAULT_SENTIMENT_POS = [
    "回购", "增持", "中标", "预增", "扭亏", "超预期", "获批", "批复",
    "订单", "中标", "合作", "签约", "战略合作", "投产", "量产", "突破",
    "创新高", "业绩增长", "净利润增长", "分红", "派息", "收购", "注入",
    "预升", "新高", "大利好", "利好", "摘牌",
    "权益分派", "战略融资", "授信", "补助", "补贴", "税收优惠",
    # 实测漏判后补的词组（务必整个词匹配，别拆成单字）
    # 刻意不收「入选」「开业」「揭牌」这类泛词 —— 它们在「入选首批…名单」
    # 「新店开业」里也会命中，污染率高于信号价值。
    "融资融券", "两融", "配售", "增发", "可转债", "重组", "要约",
    "控制权变更", "易主", "举牌", "入主", "专精特新", "单项冠军",
]


def classify_sentiment(title: str, pos_words: list[str] | None = None,
                       neg_words: list[str] | None = None) -> str:
    """按标题文本判定情绪，返回 'pos' / 'neg' / ''（中性或无法判定）。

    - 利空优先：同时命中时判 neg。理由见 DEFAULT_SENTIMENT_NEG 上面的龙虎榜统计。
    - 词表 = 通用表 + 调用方传入的个股专属表（sa_watchlist.keywords_pos/neg）。
    - 匹配用「词 in 标题」，不做分词。中文标题短，误召回可接受；
      真正的兜底是下游 —— 打不中就是 ''，LLM 看到「中性」而不是错误的情绪。
    """
    t = (title or "").strip()
    if not t:
        return ""
    negs = DEFAULT_SENTIMENT_NEG + list(neg_words or [])
    poss = DEFAULT_SENTIMENT_POS + list(pos_words or [])
    for w in negs:
        if w and w in t:
            return "neg"
    for w in poss:
        if w and w in t:
            return "pos"
    return ""


def save_to_db(items: list[dict], related_map: dict[str, list[str]] | None = None,
               senti_words: dict | None = None) -> int:
    """新闻写云库 sa_news（URL 唯一去重），并按 URL 聚合关联股票。

    related_map: {url: [codes]}，一条新闻命中多只自选股时的关联关系。
    注意：主 code 列保留"第一次发现该 URL 的股票"，全部关联在 sa_news_related。
    情绪标记：优先按**标题文本**判定（classify_sentiment），其次才用
    「利空词轮抓到的」这个线索（it['_senti']）兜底。利空优先。
    已存在的行只在利空时升级（保留首见情绪，避免被后轮冲掉）。
    senti_words: {code: {'pos': [...], 'neg': [...]}} 个股专属词表。
    返回新增条数。
    """
    if not items:
        return 0
    senti_words = senti_words or {}
    inserted = 0
    now = datetime.now()
    max_age_days = 7  # 只保留最近 7 天的新闻，旧新闻对决策没有价值
    with _db() as conn, conn.cursor() as cur:
        for it in items:
            # 过滤旧新闻：publish_time 存在且超过 max_age_days 天的跳过。
            # publish_time 为 None 的（如 SearXNG/bing）不过滤 —— 它们没有时间信息，
            # 无法判断新旧，宁可保留（下游可以按 fetched_at 过滤）。
            pub = it.get("publish_time")
            if pub:
                try:
                    if isinstance(pub, str):
                        pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    else:
                        pub_dt = pub
                    age_days = (now - pub_dt.replace(tzinfo=None)).days
                    if age_days > max_age_days:
                        continue
                except (ValueError, TypeError):
                    pass  # 时间解析失败不过滤，宁可保留
            sw = senti_words.get(it.get("code")) or {}
            senti = classify_sentiment(it.get("title") or "",
                                       sw.get("pos"), sw.get("neg"))
            if not senti:
                senti = it.get("_senti") or ""      # 关键词轮的线索兜底
            cur.execute(
                "INSERT INTO sa_news (code, title, url, source, media, publish_time, sentiment) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                (it["code"], it["title"], it["url"], it["source"], it["media"],
                 it.get("publish_time"), senti))
            inserted += cur.rowcount
            if senti == "neg":  # 已存在的旧新闻，利空标记仍要补上
                cur.execute(
                    "UPDATE sa_news SET sentiment = 'neg' "
                    "WHERE url = %s AND sentiment <> 'neg'", (it["url"],))
    _save_relations(related_map or {})
    return inserted


def _save_relations(related_map: dict[str, list[str]]) -> None:
    """写 sa_news_related 关联表（url + code 多对多）。"""
    if not related_map:
        return
    with _db() as conn, conn.cursor() as cur:
        for url, codes in related_map.items():
            for code in codes:
                cur.execute(
                    "INSERT INTO sa_news_related (url, code) VALUES (%s,%s) "
                    "ON CONFLICT (url, code) DO NOTHING", (url, code))


def _split_keywords(raw: str) -> list[str]:
    """逗号/中文逗号分隔的搜索词 → 去空去重列表（与 app.py 同规则）。"""
    return [k.strip() for k in re.split(r"[,，]", raw or "") if k.strip()]


def fetch_watchlist(conf: dict | None = None) -> dict:
    """抓取全部自选股并入库，返回轮次统计。

    关联逻辑：同一条新闻（URL 相同）命中多只自选股时，只入库一次，
    但通过 sa_news_related 关联到所有命中的股票。
    每只股票除按名搜索外，还按其搜索词各搜一轮：中性词（keywords，
    竞品动态如「Kimi,OpenAI」）、利好词（keywords_pos，如「中标,回购」）、
    利空词（keywords_neg，如「解禁,减持」）；利好/利空词命中的新闻带
    情绪标记（sa_news.sentiment），同一 URL 利空优先。
    """
    conf = conf or load_config()
    with _db() as conn:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT code, name, keywords, keywords_pos, keywords_neg "
                        "FROM sa_watchlist ORDER BY added_at")
            stocks = [dict(r) for r in cur.fetchall()]

    # 逐股抓取，按 URL 聚合：{url: item}，同 URL 补充关联而非重复入库。
    # 另做名称交叉匹配：标题里含其他自选股名称/简称的，也补上关联
    # （如「白酒行业」新闻同时提到茅台和五粮液）。
    url_items: dict[str, dict] = {}
    url_related: dict[str, list[str]] = {}
    per_stock = []
    name_index = [(s["code"], s["name"],
                   s["name"].replace("贵州", "").replace("股份", "").replace("-SW", ""))
                  for s in stocks]
    # 个股专属情绪词表，传给 save_to_db 做标题判定。
    # 通用表在 classify_sentiment 内部（DEFAULT_SENTIMENT_POS/NEG），
    # 这里只补 sa_watchlist 上逐只配的那部分 —— 35 只全都没配也不影响通用表生效。
    senti_words = {
        s["code"]: {"pos": _split_keywords(s.get("keywords_pos", "")),
                    "neg": _split_keywords(s.get("keywords_neg", ""))}
        for s in stocks
    }
    _prog_start(len(stocks), "自选股")
    for i, s in enumerate(stocks):
        _prog_step(current=f"{s['name']}({s['code']})", done=i)
        outcome = fetch_for_stock(s["code"], s["name"], conf,
                                  keywords=_split_keywords(s.get("keywords", "")),
                                  keywords_pos=_split_keywords(s.get("keywords_pos", "")),
                                  keywords_neg=_split_keywords(s.get("keywords_neg", "")))
        fetched = 0
        # 关键词命中的新闻打上标记，stats 单独计数（竞品动态是否有料一眼可见）
        kw_hits, pos_hits, neg_hits = 0, 0, 0
        for item in outcome["results"]:
            url = item["url"]
            if not url:
                continue
            if url in url_items:
                if s["code"] not in url_related[url]:
                    url_related[url].append(s["code"])
                # 同 URL 后到的利空标记，覆盖之前保留的条目（入库时利空优先）
                if item.get("_senti") == "neg":
                    url_items[url]["_senti"] = "neg"
            else:
                url_items[url] = item
                url_related[url] = [s["code"]]
            # 关键词轮次抓到的条目：来源标记 orig_query（供统计，不影响入库）
            if item.get("_kw") and item["_kw"] not in ("", s["name"]):
                kw_hits += 1
                if item.get("_senti") == "pos":
                    pos_hits += 1
                elif item.get("_senti") == "neg":
                    neg_hits += 1
            # 交叉关联：标题提到其他自选股
            title = item["title"]
            for code, full, short in name_index:
                if code != s["code"] and code not in url_related.get(url, []) \
                        and ((full and full in title) or (short and short in title)):
                    url_related.setdefault(url, []).append(code)
            fetched += 1
        per_stock.append({"code": s["code"], "name": s["name"],
                          "fetched": fetched, "kw_fetched": kw_hits,
                          "pos_fetched": pos_hits, "neg_fetched": neg_hits,
                          "errors": outcome["errors"]})
    # 全部股票抓完，统一入库一次（含关联），按关联主股票数分摊统计
    total_new = save_to_db(list(url_items.values()), url_related, senti_words)
    _prog_step(current="主题词", done=len(stocks))
    # 主题词阶段单独隔离：它崩了不能让整轮统计消失。
    # 2026-10-01 实测踩过 —— 主题阶段一个参数名写错抛异常，
    # 结果 36 只票已经入库的数据「看不见」了，接口只回一个 error，
    # 让人误以为整轮失败、还可能因此重跑。个股阶段的数据是好的，不该被牵连。
    try:
        topics = fetch_topics(conf)
    except Exception as exc:                                   # noqa: BLE001
        topics = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"[news] 主题词阶段失败（不影响本轮个股入库）: {exc}", flush=True)
    finally:
        _prog_finish()
    return {"fetched_at": datetime.now().isoformat(timespec="seconds"),
            "stocks": per_stock, "total_new": total_new,
            "topics": topics, "health": health_snapshot(conf)}


# ---------------- 主题词通道（2026-10-01 加）----------------
# 为什么必须有
# ----------
# `fetch_watchlist()` 只按**自选股**逐个搜词，所以库里 10856 条新闻全是
# 已有标的的动态 —— 它**结构性地发现不了新股票/新题材**：
# 新股没进自选股 → 不搜它 → 它的消息永远不进库。
# 2026-10-01 排查过：news 4 个 channel + 公众号 5 源 + B站 + 微博 + X
# 全是「按标的订阅」，没有一个是「按主题订阅」。打新/次新股这类
# 「还没进自选股的新东西」就是从这个缝里漏掉的。
#
# 做法
# ----
# 复用同一套 4 个 channel fetcher，只是把 query 从「股票名」换成「主题词」。
# 关键约束：**同一条新闻可能被个股轮和主题轮抓到** —— 去重靠 URL，
# 和个股轮之间也共享 URL 集合，不会重复入库。
#
# 主题词不入 sa_news.code（那是自选股代码），而是记在 `topic` 字段，
# 由 discover 模块决定要不要把命中的公司升级成自选股。

# 主题词分组：每组一个用途，便于统计与调权重。
# 都是「可能带来新标的」的词，不是行情复述词。
TOPIC_GROUPS: dict[str, list[str]] = {
    "ipo": [
        "新股申购", "新股上市", "新股发行", "申购日期", "中签号",
        "发行价", "招股意向书", "上市公告书", "网上申购", "顶格申购",
        "打新", "打新规则", "配号", "市值配售",
    ],
    "convertible": [
        "可转债", "转债申购", "转债上市", "转债发行", "可转债申购",
    ],
    "policy": [
        "证监会", "交易所公告", "注册制", "并购重组", "再融资",
        "产业政策", "国资委", "发改委",
    ],
    "hot_money": [
        "游资", "龙虎榜", "涨停潮", "题材炒作", "妖股", "连板",
        "主力资金", "北向资金", "大宗交易", "举牌",
    ],
    "earnings": [
        "业绩预告", "业绩快报", "年报", "一季报", "半年报", "三季报",
        "预增", "预亏", "扭亏",
    ],
}


def fetch_topics(conf: dict | None = None, *, groups: list[str] | None = None,
                 per_group: int = 8) -> dict:
    """按主题词跑一轮，把命中新闻入库。

    与 `fetch_watchlist` 的区别：
      - query 是主题词，不是股票名
      - 不建 sa_news_related 关联（还没确认是哪只票）
      - 落库时 `code` 留空，`topic` 记「分组:词」，交给 discover 去认领

    `per_group` 是每个分组抓多少条 —— 主题词比个股名噪声大得多
    （搜「龙虎榜」出来一堆复盘），所以限量而不是全要。
    """
    conf = conf or load_config()
    limit = min(int(conf.get("items_per_query", 10)), per_group)
    gnames = groups or list(TOPIC_GROUPS)

    url_items: dict[str, dict] = {}
    per_group_stat = []
    total_words = sum(len(TOPIC_GROUPS.get(g) or []) for g in gnames)
    done_words = 0
    for g in gnames:
        words = TOPIC_GROUPS.get(g) or []
        if not words:
            continue
        got_n, errs = 0, {}
        for kw in words:
            for channel, fetcher in FETCHERS.items():
                ch_conf = conf["channels"].get(channel) or {}
                if not ch_conf.get("enabled", False):
                    continue
                # 主题轮只跑「按词搜」的渠道：主题词不是股票代码，
                # ak_em（stock_news_em 认代码）跑不了；搜索类渠道单次 8~25s，
                # 5 组共 40+ 个词，跑全了光搜索就要一小时。
                if channel in ("duckduckgo", "bing", "searxng", "ak_em"):
                    continue
                ready, why = _channel_ready(channel, conf)
                if not ready:
                    _prog_step(skip=f"{channel}: {why}")
                    continue
                _prog_step(current=f"[{g}] {kw}", channel=channel)
                t_ch = time.time()
                try:
                    got = fetcher("", kw, limit, conf, keyword=kw)
                    for it in got:
                        it["topic"] = f"{g}:{kw}"
                        it["code"] = ""
                    if got:
                        got_n += len(got)
                    for it in got:
                        if it.get("url"):
                            url_items.setdefault(it["url"], it)
                except Exception as exc:                  # noqa: BLE001
                    errs[channel] = str(exc)[:120]
                _prog_step(channel=channel, got=len(url_items),
                            ms=int((time.time() - t_ch) * 1000))
            # 单个主题词间隔一点，别把搜索接口打爆
            time.sleep(0.3)
            done_words += 1
            _prog_step(done=done_words, total=total_words)
        per_group_stat.append({"group": g, "words": len(words),
                               "fetched": got_n, "errors": errs})
        print(f"[news] 主题组 {g}: {len(words)} 词 -> {got_n} 条", flush=True)

    items = list(url_items.values())
    # code 为空的条目：save_to_db 要能处理（sa_news.code 可空）
    new_n = _save_topic_items(items)
    return {"fetched_at": datetime.now().isoformat(timespec="seconds"),
            "groups": per_group_stat, "unique": len(items), "total_new": new_n}


# 主题词新闻的 code 哨兵值。
#
# 为什么不能用 NULL / 空串（实测 2026-10-01）：
#   sa_news.code 是 **NOT NULL**，插 NULL 直接报错。
#   而 `ON CONFLICT (url) DO NOTHING` 看着安全，其实有副作用：
#   DO NOTHING 确实不覆盖已有行，所以不会把自选股新闻的 code 冲掉 ——
#   但反过来说，**同一条新闻被个股轮先抓到时，主题轮这条就被丢弃**，
#   于是它永远不会带上 topic 标记，discover 就认不出它。
#   所以这里改用 DO UPDATE：只在「原来是主题词新闻（哨兵 code）」时才补 topic，
#   绝不碰已有自选股的行。
TOPIC_CODE = "_TOPIC_"


def _save_topic_items(items: list[dict]) -> int:
    """主题词新闻入库。与 save_to_db 分开是因为没有 related_map，也没有
    逐股情绪词；但去重/冲突策略必须和它对齐（共用 sa_news_url_key）。"""
    if not items:
        return 0
    new_n = 0
    with _db() as conn:
        with conn.cursor() as cur:
            for it in items:
                url = (it.get("url") or "").strip()
                title = (it.get("title") or "").strip()
                source = (it.get("source") or "").strip() or "topic"
                media = (it.get("media") or "").strip()
                senti = (it.get("sentiment") or "").strip()
                if not url or not title:
                    continue
                cur.execute(
                    """INSERT INTO sa_news
                       (code, title, url, source, media, publish_time,
                        fetched_at, sentiment, topic)
                       VALUES (%s,%s,%s,%s,%s,%s, now(), %s, %s)
                       ON CONFLICT (url) DO NOTHING""",
                    (TOPIC_CODE, title, url, source, media,
                     it.get("publish_time"), senti, it.get("topic") or ""))
                new_n += cur.rowcount or 0
    return new_n


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--health":
        # 各渠道健康度 + 冷却状态。排查「为什么这轮没抓到新闻」先看这个：
        # ok=0/fail=0 说明压根没跑（enabled=false），fail>0/cooling=True 说明源坏了。
        print(json.dumps({"config": load_config()["channels"],
                          "health": health_snapshot()},
                         ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 3:
        if sys.argv[1] == "--topics":
            print(json.dumps(fetch_topics(groups=sys.argv[3:4] or None),
                             ensure_ascii=False, indent=2))
        else:
            outcome = fetch_for_stock(sys.argv[1], sys.argv[2])
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(fetch_watchlist(), ensure_ascii=False, indent=2))
