"""多渠道免费新闻抓取模块（全部免 key，零成本）。

渠道（2026-09-05 本机实测可用性见 README）：
- eastmoney   东财公告接口 + 资讯搜索 JSONP —— 主力，纯 JSON，0.2s
- baidu       百度新闻垂直搜索 HTML —— 主力，时效最好，反爬敏感需控频
- sina        新浪财经滚动 JSON —— 备用，按股票名过滤
- duckduckgo  DDG Lite HTML —— 备用，需长超时（本机走代理约 8s）

用法：
    python news_fetcher.py                      # 抓全部自选股
    python news_fetcher.py 600519 贵州茅台      # 单股单跑（验证用）
"""

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# ---------------- 配置加载（无 pyyaml 时回退到简易解析） ----------------

DEFAULT_CONF = {
    "channels": {
        "eastmoney": {"enabled": True, "min_interval": 1.0},
        "baidu": {"enabled": True, "min_interval": 3.0},
        "sina": {"enabled": True, "min_interval": 2.0},
        "duckduckgo": {"enabled": True, "min_interval": 5.0, "timeout": 30},
    },
    "fetch_interval_minutes": 60,
    "items_per_query": 10,
    "keywords_extra": [],
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
    conf["channels"].update(news.get("channels") or {})
    for key in ("fetch_interval_minutes", "items_per_query"):
        if news.get(key) is not None:
            conf[key] = int(news[key])
    if news.get("keywords_extra") is not None:
        conf["keywords_extra"] = list(news["keywords_extra"])
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


# ---------------- 各渠道实现（返回统一结构的 list） ----------------

def _norm(code: str, title: str, url: str, source: str,
          media: str = "", publish_time=None) -> dict:
    return {
        "code": code,
        "title": re.sub(r"<[^>]+>", "", title or "").strip(),
        "url": url,
        "source": source,
        "media": media or {"eastmoney": "东方财富", "baidu": "百度新闻",
                           "sina": "新浪财经", "duckduckgo": "DuckDuckGo"}.get(source, source),
        "publish_time": publish_time,   # ISO 字符串或 None
    }


def fetch_eastmoney(code: str, name: str, limit: int, conf: dict,
                    keyword: str = "") -> list[dict]:
    """东财公告 + 资讯搜索，两个接口合一渠道。港股(5位)无 A 股公告接口，走资讯搜索。

    keyword 非空时为"自定义搜索词"模式（竞品动态等）：只跑资讯搜索，不跑公告。
    """
    items: list[dict] = []
    ch_conf = conf["channels"]["eastmoney"]
    is_hk = re.fullmatch(r"\d{5}", code) is not None
    query = keyword or name
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
            pass
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
    except Exception:
        pass
    return items


def fetch_baidu(code: str, name: str, limit: int, conf: dict,
                keyword: str = "") -> list[dict]:
    """百度新闻垂直搜索（tn=news&rtt=4 按时间排序）。keyword 非空 = 自定义搜索词模式。"""
    ch_conf = conf["channels"]["baidu"]
    _rate_limit("baidu", ch_conf.get("min_interval", 3.0))
    try:
        r = requests.get(
            "https://www.baidu.com/s",
            params={"wd": keyword or name, "tn": "news", "rtt": 4, "bsst": 1, "cl": 2},
            headers=UA, timeout=10)
        r.raise_for_status()
        html = r.text
        if "百度安全验证" in html:  # 反爬命中，本渠道本轮放弃
            return []
        items = []
        # 结果块：<h3><a href="URL" ...>TITLE</a>（百度新闻搜索结果标题链接）
        for m in re.finditer(
                r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            url = m.group(1)
            if title and url.startswith("http"):
                items.append(_norm(code, title, url, "baidu"))
            if len(items) >= limit:
                break
        return items
    except Exception:
        return []


def fetch_sina(code: str, name: str, limit: int, conf: dict,
               keyword: str = "") -> list[dict]:
    """新浪财经滚动新闻：拉多页财经流，按股票名/简称过滤标题。

    自定义搜索词模式（keyword 非空）同样只做标题过滤——滚动流是全市场混排，
    竞品关键词（如"OpenAI"）命中率不高，但零成本顺带扫一遍。
    """
    ch_conf = conf["channels"]["sina"]
    _rate_limit("sina", ch_conf.get("min_interval", 2.0))
    # 新浪滚动流是全市场混排，"贵州茅台"全名命中太苛刻；改用短简称集合匹配
    short_names = {name, name.replace("贵州", "").replace("股份", "")}
    if name.startswith(("ST", "*")):
        short_names.add(name.lstrip("*ST"))
    if keyword:
        short_names = {keyword}
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
                    return items
            _rate_limit("sina", ch_conf.get("min_interval", 2.0))
        return items
    except Exception:
        return []


def fetch_duckduckgo(code: str, name: str, limit: int, conf: dict,
                     keyword: str = "") -> list[dict]:
    """DDG Lite HTML 搜索（本机走代理，需长超时）。

    实测 DDG 返回的多是行情/个股主页而非新闻文章页，价值有限；
    只保留"新闻聚合页"类链接（如 investing.com 股票新闻页），作为兜底渠道。
    """
    ch_conf = conf["channels"]["duckduckgo"]
    _rate_limit("duckduckgo", ch_conf.get("min_interval", 5.0))
    query = f"{keyword} 新闻" if keyword else f"{name} {code} 新闻"
    try:
        r = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers=UA,
            timeout=ch_conf.get("timeout", 30))
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
        return items
    except Exception:
        return []


FETCHERS = {
    "eastmoney": fetch_eastmoney,
    "baidu": fetch_baidu,
    "sina": fetch_sina,
    "duckduckgo": fetch_duckduckgo,
}


# ---------------- 聚合入口 ----------------

def fetch_for_stock(code: str, name: str, conf: dict | None = None,
                    keywords: list[str] | None = None) -> dict:
    """对一只股票跑所有启用渠道，返回 {code, name, results, errors}。

    keywords: 该股的自定义搜索词（竞品动态等，逗号分隔存 sa_watchlist.keywords）。
    每个关键词对每个渠道额外跑一轮（东财跳过公告、只搜资讯），命中即关联到该股。
    """
    conf = conf or load_config()
    limit = conf.get("items_per_query", 10)
    kw_limit = max(3, limit // 2)   # 关键词轮次取条数减半，控制总量
    results, errors = [], {}
    queries = [(name, "")] + [(kw, kw) for kw in (keywords or [])]
    for stock_name, keyword in queries:
        for channel, fetcher in FETCHERS.items():
            ch_conf = conf["channels"].get(channel) or {}
            if not ch_conf.get("enabled", False):
                continue
            # 关键词轮跳过 DDG：它单次要 8-30s 且返回多为聚合页，竞品动态
            # 靠东财+百度已够；关键词一多整轮时长远超抓取周期就得不偿失了
            if keyword and channel == "duckduckgo":
                continue
            try:
                got = fetcher(code, stock_name, kw_limit if keyword else limit,
                              conf, keyword=keyword)
                if keyword:  # 标记来自哪个关键词的轮次（去重后统计用）
                    for it in got:
                        it["_kw"] = keyword
                results.extend(got)
            except Exception as exc:
                errors[channel] = str(exc)
    # 跨渠道按 URL 去重（关键词标记只保留第一个命中的）
    seen, uniq = set(), []
    for item in results:
        if item["url"] and item["url"] not in seen:
            seen.add(item["url"])
            uniq.append(item)
    return {"code": code, "name": name, "results": uniq, "errors": errors}


def save_to_db(items: list[dict], related_map: dict[str, list[str]] | None = None) -> int:
    """新闻写云库 sa_news（URL 唯一去重），并按 URL 聚合关联股票。

    related_map: {url: [codes]}，一条新闻命中多只自选股时的关联关系。
    注意：主 code 列保留"第一次发现该 URL 的股票"，全部关联在 sa_news_related。
    返回新增条数。
    """
    if not items:
        return 0
    from app import get_conn  # 延迟导入避免循环依赖
    inserted = 0
    with get_conn() as conn, conn.cursor() as cur:
        for it in items:
            cur.execute(
                "INSERT INTO sa_news (code, title, url, source, media, publish_time) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                (it["code"], it["title"], it["url"], it["source"], it["media"],
                 it.get("publish_time")))
            inserted += cur.rowcount
    _save_relations(related_map or {})
    return inserted


def _save_relations(related_map: dict[str, list[str]]) -> None:
    """写 sa_news_related 关联表（url + code 多对多）。"""
    if not related_map:
        return
    from app import get_conn
    with get_conn() as conn, conn.cursor() as cur:
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
    每只股票除按名搜索外，还按其自定义搜索词（sa_watchlist.keywords，
    如智谱配「Kimi,OpenAI,DeepSeek」）各搜一轮，竞品动态也挂到该股新闻流。
    """
    conf = conf or load_config()
    from app import get_conn
    with get_conn() as conn:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT code, name, keywords FROM sa_watchlist ORDER BY added_at")
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
    for s in stocks:
        outcome = fetch_for_stock(s["code"], s["name"], conf,
                                  keywords=_split_keywords(s.get("keywords", "")))
        fetched = 0
        # 关键词命中的新闻打上标记，stats 单独计数（竞品动态是否有料一眼可见）
        kw_hits = 0
        for item in outcome["results"]:
            url = item["url"]
            if not url:
                continue
            if url in url_items:
                if s["code"] not in url_related[url]:
                    url_related[url].append(s["code"])
            else:
                url_items[url] = item
                url_related[url] = [s["code"]]
            # 关键词轮次抓到的条目：来源标记 orig_query（供统计，不影响入库）
            if item.get("_kw") and item["_kw"] not in ("", s["name"]):
                kw_hits += 1
            # 交叉关联：标题提到其他自选股
            title = item["title"]
            for code, full, short in name_index:
                if code != s["code"] and code not in url_related.get(url, []) \
                        and ((full and full in title) or (short and short in title)):
                    url_related.setdefault(url, []).append(code)
            fetched += 1
        per_stock.append({"code": s["code"], "name": s["name"],
                          "fetched": fetched, "kw_fetched": kw_hits,
                          "errors": outcome["errors"]})
    # 全部股票抓完，统一入库一次（含关联），按关联主股票数分摊统计
    total_new = save_to_db(list(url_items.values()), url_related)
    return {"fetched_at": datetime.now().isoformat(timespec="seconds"),
            "stocks": per_stock, "total_new": total_new}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:  # 单股验证模式
        outcome = fetch_for_stock(sys.argv[1], sys.argv[2])
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(fetch_watchlist(), ensure_ascii=False, indent=2))
