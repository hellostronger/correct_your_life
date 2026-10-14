# -*- coding: utf-8 -*-
"""港股供给冲击：IPO 锁定期 + 融资禁售承诺，分开两条线。

为什么需要这个模块（2026-10-12 用户指出）
--------------------------------------------
`alerts.py` / `supply_events.py` 都只覆盖 A 股：东财的解禁表
（RPT_LIFT_STAGE / RPT_EXECUTIVE_HOLD_DETAILS）是**沪深交易所**口径，
港股解禁在东财**根本没有对应报表**（2026-10-12 实测 6 个候选 reportName
全部返回 `code=9501 报表配置不存在`，见 AGENTS.md）。
前端那句「港股不适用」是真的，但它是**没实现**，不是「查过了没有」。

港股的「解禁期」有两种，语义完全不同，**混在一起就是答非所问**
（智谱 02513 实测，下面每条都有 PDF 出处）：

| 线 | 是什么 | 出处 | 拿到没 |
|---|---|---|---|
| `ipo_lockup` | IPO 时基石 6 个月、控股股东 6~12 个月、Pre-IPO 股东各自 | **招股书** | 智谱 2026-01 的已滑出 HKEXnews 12 个月窗口 |
| `pledge_lock` | 每次配售/可转债时**发行人**承诺不再发新股的期限 | 配售/认购公告 | 智谱 2026-09 配售 90 日、可转债 60 日，**已拿到** |

⚠️ 第二种**不是「股份解禁」**，是「公司不能再融资」。界面上必须分开显示，
   否则会让人以为「解禁 90 日后有一批股票可以卖」—— 那批新股发行日就可流通。

数据源（2026-10-12 本机实测）
------------------------------
- HKEXnews 静态搜索 `titleSearchServlet.do`（GET + querystring）。
  ⚠️ POST 返回 **405**，GET 才通；返回体是 `{"result": "<json string>"}`，
     **result 本身还是一层字符串** —— 少 parse 一次就拿到空列表
     （与 AGENTS.md 记的 lfasr 双层 JSON 同源）。
- 股票代码 -> 内部 stockId 走 `search/prefix.do?callback=cb`（返回 JSONP）。
- **只保留近 12 个月**，更早的公告查不到 —— 这是硬边界，必须如实上报，
  不能用 LLM 抽的数字冒充公告原文。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www1.hkexnews.hk"
SEARCH_URL = BASE + "/search/titleSearchServlet.do"
PREFIX_URL = BASE + "/search/prefix.do"
PDF_BASE = BASE

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": BASE + "/search/titlesearch.xhtml?lang=zh",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# HKEXnews 的公告保留窗口。年数写死是为了让人一眼看到边界，
# 超过它 -> 该公告查不到，必须走 LLM 兜底并在结果里标出来。
HKEX_RETENTION_DAYS = 365


class HKEXError(RuntimeError):
    pass


# ================= 缓存（配额保护）=================
# MinerU 免费额度 5000 份/天，看似很宽裕，但**每次解析都是真金白银的
# 一次网络往返 + 一次 LLM 调用**。同一家公司的同一份公告如果每次打开页面
# 都重新解析，一上午就能把当天的额度烧光，而且用户看不出区别。
# 缓存键用公告 URL（含文件路径与日期，天然唯一），不是用「代码+日期」——
# 后者会把「同一天不同文件」误判成同一份。
CACHE_DIR = Path(os.environ.get("HK_CACHE_DIR")
                  or (Path(__file__).resolve().parent / "data" / "hk_supply_cache"))
_CACHE_WARNED = False
# 条款抽取结果可以复用更久：公告内容不会变，变的是「什么时候该重查公告列表」。
CLAUSE_TTL_DAYS = 30
MINEDOC_TTL_DAYS = 7


def _cache_path(key: str, suffix: str = ".json") -> Path:
    import hashlib
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / (h + suffix)


def cache_get(key: str, ttl_days: int, suffix: str = ".json"):
    """读缓存。**过期或损坏一律返回 None**，绝不用半个文件凑合。"""
    p = _cache_path(key, suffix)
    try:
        if not p.exists():
            return None
        if ttl_days >= 0 and (time.time() - p.stat().st_mtime) > ttl_days * 86400:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 磁盘坏/半写：当成没有，重新算。不能把解析失败的半个结果当成功。
        return None


def cache_put(key: str, value) -> bool:
    """写缓存。

    ⚠️ 失败**不能静默**（2026-10-13 实测踩到）：容器里 /app/data 归 root:755，
       进程以 svc 身份跑 -> mkdir/write 全 Permission denied，而这里原本
       `except OSError: return False` 一声不吭。结果跑了 **25 分钟**的解析
       **一份都没缓存住**，下次点按钮又是 25 分钟 —— 而界面上完全看不出异常。
       这正是 AGENTS.md 反复警告的「失败被吞 -> 看起来成功、实际没生效」。
       现在**首次失败打印一次**（之后不刷屏），并把原因带回给调用方。
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(key)
        tmp = p.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)                      # 原子替换，避免半写文件
        return True
    except OSError as exc:
        global _CACHE_WARNED
        if not _CACHE_WARNED:
            _CACHE_WARNED = True
            print("[hk_supply] ⚠ 缓存写入失败（之后不再重复提示）：%s | 目录=%s"
                  % (exc, CACHE_DIR), flush=True)
            print("[hk_supply] ⚠ 后果：每次都会重新解析（慢且烧 MinerU 配额）。"
                  "修法：确保挂载目录对容器内用户可写"
                  "（chown 10001:10001 或 chmod 777）。", flush=True)
        return False


def get(url: str, timeout: int = 30, retries: int = 3, backoff: float = 2.0) -> str:
    """带重试的 GET。

    ⚠️ 这台机器到 hkexnews / cdn-mineru 的连接**间歇性失败**是常态：
    实测一轮扫描 5 份公告里 4 份挂在
    `getaddrinfo failed` / `WinError 10060`（DNS 与连接超时），
    而紧接着手动重试就成功。
    判据是 AGENTS.md 第 2 条：**超时必须重试才能区分**，一次失败就下
    「源挂了」的结论会误杀可用源。只重试网络类异常，HTTP 4xx/5xx 直接抛。
    """
    import socket
    import urllib.error

    last = None
    for i in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError:
            raise                      # HTTP 层错误重试没意义，直接抛
        except (urllib.error.URLError, socket.timeout, socket.gaierror,
                ConnectionError, TimeoutError) as exc:
            last = exc
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    raise HKEXError("GET 失败（已重试 %d 次）%s: %s" % (retries, url[:110], last))


def resolve_stock_id(code: str) -> tuple[str, str]:
    """港股代码 -> (内部 stockId, 公司名)。

    必须走 prefix.do：stockId 不是代码本身。智谱 02513 的 stockId 是
    1000286748，写死在配置里迟早会错。
    """
    code = str(code).strip().lstrip("0").zfill(5) if len(str(code).lstrip("0")) <= 5 \
        else str(code).strip()
    q = urllib.parse.urlencode({"callback": "cb", "lang": "ZH", "type": "A",
                                "name": code, "market": "SEHK"})
    txt = get(PREFIX_URL + "?" + q)
    m = re.search(r"callback\((.*)\)\s*;?\s*$", txt, re.S)
    if not m:
        raise HKEXError("prefix.do 返回无法解析: %s" % txt[:120])
    d = json.loads(m.group(1))
    for it in d.get("stockInfo") or []:
        # 精确匹配代码，别拿同前缀的其它股票（如 25137 江铜摩通七三沽A）
        if str(it.get("code")) == code or str(it.get("code")).lstrip("0") == code.lstrip("0"):
            return str(it["stockId"]), it.get("name", "")
    raise HKEXError("prefix.do 未找到 %s（返回 %d 条）" % (code, len(d.get("stockInfo") or [])))


def search_announcements(stock_id: str, date_from: str, date_to: str,
                         t1code: str = "-2", rows: int = 200) -> list[dict]:
    """按内部 stockId 搜公告。date 格式 YYYYMMDD。

    ⚠️ 两个坑（都是实测踩的）：
      1. 必须 **GET + querystring**。POST 同路径返回 405。
      2. 返回体是 `{"result": "<JSON 字符串>"}` —— result 要再 json.loads 一次。
    """
    q = urllib.parse.urlencode({
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
        "market": "SEHK", "searchType": "0", "documentType": "-1",
        "t1code": t1code, "t2Gcode": "-2", "t2code": "-2",
        "stockId": stock_id, "from": date_from, "to": date_to,
        "title": "", "searchText": "", "lang": "zh",
        "page": "1", "rowRange": str(rows),
    })
    raw = get(SEARCH_URL + "?" + q)
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HKEXError("titleSearch 返回非 JSON: %s" % raw[:160]) from exc
    inner = outer.get("result")
    if not inner:
        return []
    if isinstance(inner, str):
        try:
            return json.loads(inner)          # ← 第二层，别漏
        except json.JSONDecodeError as exc:
            raise HKEXError("result 内层解析失败: %s" % str(inner)[:160]) from exc
    return inner or []


def pdf_url(file_link: str) -> str:
    if not file_link:
        return ""
    return file_link if file_link.startswith("http") else PDF_BASE + file_link


# ================= MinerU：PDF -> Markdown =================
# 为什么非 MinerU 不可（2026-10-12 智谱 02513 实测，别再走回头路）：
#   HKEXnews 的中文 PDF 用**未嵌入字体子集**。PyMuPDF 抽出来是
#     「交割日期�?0 日止」—— 丢的是「後6」这两个字，而 **90/60 这个
#     唯一的期限数字正好在被丢的字里**。pypdf 更糟，整篇乱码。
#   ⚠️ 最阴的一点：`�` 的 U+FFFD 计数是 **0**（丢的是「後6」整个字，
#     不是替换字符），所以**任何缺失检测都发现不了**，正则写得再准也
#     匹配不到一个不存在的字符串。必须换解析器。

MINERU_TASK_URL = "https://mineru.net/api/v4/extract/task"
MINERU_STATE_DONE = "done"
MINERU_STATE_FAILED = "failed"
DEFAULT_MINERU_POLL_SEC = 8
DEFAULT_MINERU_MAX_WAIT = 900      # 单份 15 分钟上限；实测智谱那份 ~40s


def _mineru_headers(token: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": "Bearer " + token,
            "User-Agent": "MinerU/1.0"}


def mineru_submit(url: str, token: str, model_version: str = "vlm",
                  timeout: int = 60) -> str:
    """提交解析任务，返回 task_id。

    HKEXnews 的 PDF 对第三方抓取是开放的（裸 UA / 无 UA / 浏览器 UA
    实测三种都是 200 + 完整 PDF），所以直接把公网 URL 交给 MinerU 抓，
    不用自己下载再上传 —— 少一次 1MB 传输，也少一处会 403 的地方。
    """
    body = json.dumps({"url": url, "model_version": model_version}).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(MINERU_TASK_URL, data=body,
                                         headers=_mineru_headers(token))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            break
        except urllib.error.HTTPError as exc:
            # 401/配额耗尽时重试没意义，直接把 body 带出去（它才说明原因）
            detail = exc.read().decode("utf-8", "ignore")[:200]
            raise HKEXError("MinerU 提交被拒 HTTP %s: %s" % (exc.code, detail))
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))       # 网络抖动，重试
    else:
        raise HKEXError("MinerU 提交失败（已重试 3 次）: %s" % last)
    # ⚠️ 必须查 code：401/配额用尽时 HTTP 也是 200，body 里 code!=0。
    #    只看 HTTP 状态码会把「没解析成功」当成「解析中」。
    if d.get("code") != 0:
        raise HKEXError("MinerU 提交失败 code=%s msg=%s" % (d.get("code"), d.get("msg")))
    return str(d["data"]["task_id"])


def mineru_poll(task_id: str, token: str, poll_sec: int = DEFAULT_MINERU_POLL_SEC,
                max_wait: int = DEFAULT_MINERU_MAX_WAIT) -> dict:
    """轮询任务直到完成，返回 data 里的 full_zip_url。

    实测：智谱那份 31 页 PDF 约 40 秒完成；`state` 只有 'done' / 'failed'，
    完成时 data 里只有 task_id / state / err_msg / full_zip_url 四个键
    （**没有** markdown_url，markdown 藏在 zip 里，别去找那个字段）。
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        req = urllib.request.Request(MINERU_TASK_URL + "/" + task_id,
                                     headers=_mineru_headers(token))
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        data = d.get("data") or {}
        state = data.get("state")
        if state == MINERU_STATE_DONE:
            if not data.get("full_zip_url"):
                raise HKEXError("MinerU 报 done 但没有 full_zip_url: %s" % data)
            return data
        if state == MINERU_STATE_FAILED:
            raise HKEXError("MinerU 解析失败: %s" % (data.get("err_msg") or "")[:200])
        time.sleep(poll_sec)
    raise HKEXError("MinerU 轮询超时（%ds）task=%s" % (max_wait, task_id))


def mineru_fetch_markdown(zip_url: str, timeout: int = 180) -> str:
    """下载结果 zip，取出 full.md 的文本。

    zip 里第一个 .md 就是正文（实测智谱那份是 `full.md`，另有
    content_list.json / layout.json / images/）—— 但**不要按名字硬取**，
    要挑「最大的那个 .md」，否则将来出多份 md 会静默拿错。
    """
    import io
    import zipfile

    raw = None
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(zip_url, headers={"User-Agent": "MinerU/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except (urllib.error.URLError, OSError) as exc:   # 网络抖动，重试
            last = exc
            time.sleep(2.0 * (attempt + 1))
    if raw is None:
        raise HKEXError("下载 MinerU 结果失败（已重试 3 次）: %s" % last)
    if raw[:2] != b"PK":
        raise HKEXError("MinerU 结果不是 zip（magic=%r len=%d）" % (raw[:8], len(raw)))
    z = zipfile.ZipFile(io.BytesIO(raw))
    mds = [(z.getinfo(n).file_size, n) for n in z.namelist() if n.endswith(".md")]
    if not mds:
        raise HKEXError("MinerU 结果 zip 里没有 .md：%s" % z.namelist()[:10])
    name = max(mds)[1]
    return z.read(name).decode("utf-8", "ignore")


def parse_pdf(url: str, token: str, model_version: str = "vlm") -> str:
    """一条龙：提交 -> 轮询 -> 取 markdown。

    ⚠️ 曾有个 bug：把 `mineru_poll()` 的返回值（**整个 data dict**）直接当
    zip_url 传给 `mineru_fetch_markdown`，于是报
    `urlopen error unknown url type: {'task_id'}`。
    `mineru_poll` 返回 dict，必须先取 `full_zip_url`。
    """
    data = mineru_poll(mineru_submit(url, token, model_version), token)
    zip_url = data.get("full_zip_url") or ""
    if not zip_url:
        raise HKEXError("MinerU 返回里没有 full_zip_url：%s" % sorted(data.keys()))
    return mineru_fetch_markdown(zip_url)


def download_pdf(file_link: str, timeout: int = 90) -> bytes:
    """自己下载 PDF。**当前链路用不到**（MinerU 直接抓公网 URL），
    留着是为了将来「解析失败时本地兜底」或人工排查用。

    ⚠️ 别把它当成主路径：实测 HKEXnews 对第三方抓取是开放的
    （裸 UA / 无 UA / 浏览器 UA 都是 200 + 完整 PDF），所以没必要中转，
    中转只会多一次 1MB 传输和一处会 403 的地方。
    """
    url = pdf_url(file_link)
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"],
                                               "Referer": BASE + "/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    # 200 但返回 HTML（多半是跳了错误页）—— 不校验 magic 会把 HTML 当 PDF 解析，
    # 得到一堆空文本然后「看起来成功、实际没数据」
    if not data[:5].startswith(b"%PDF"):
        raise HKEXError("下载的不是 PDF（magic=%r, len=%d）" % (data[:8], len(data)))
    return data