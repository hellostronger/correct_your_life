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
# 文档页数：**同一个 URL 的文件永不改版**，页数不会变，缓存可以留很久。
# 它决定「要不要走分块」——没有它每次都要先拿整份去撞 200 页上限，白烧一次
# MinerU 的失败额度（见 mineru_probe_page_count 注释）。
PAGECOUNT_TTL_DAYS = 365
# 公告**列表**缓存：盘中基本不变，但一次搜索 8 只票实测 94 秒。
# 阶段1「只读缓存秒出」若每次都要重搜，这个「秒出」就是假的。
ANNOUNCE_TTL_DAYS = 0.25      # 6 小时


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

    ⚠️ 三个坑（都是实测踩的，其中第 3 条是这个功能**整体失效**的根因）：
      1. 必须 **GET + querystring**。POST 同路径返回 405。
      2. 返回体是 `{"result": "<JSON 字符串>"}` —— result 要再 json.loads 一次。
      3. 🔴 日期参数叫 **`fromDate` / `toDate`**，不是 `from` / `to`
         （2026-10-15 定位）。写成 from/to 时**服务端静默忽略**，不报错、
         不 400，只是照常返回**最近约 30 天**的公告 —— 于是
         `date_from = 今天 - 365 天` 是个笑话：
         | 代码 | 02513 公告数 | 实际覆盖窗口 |
         |---|---|---|
         | `from`/`to`（错） | **6** | 09/10~16/09/2026（约 30 天）|
         | `fromDate`/`toDate`（对） | **57** | 09/10/2025~10/09/2026 |
         传 `from=20160101` 与 `from=20250101` 返回**逐条相同**的结果，
         这是判据（不是「碰巧公告少」）。
         后果不是「慢一点」而是**功能完全无效**：禁售/锁定条款写在几个月前
         的配售、招股、认购公告里，而这个接口只看得到最近 30 天，
         于是 8 只港股**全部**显示「未识别到禁售条款」，
         而页面看起来一切正常（这正是 AGENTS.md 反复警告的假成功）。

    ⚠️ **公告列表本身也缓存**（`ann:` 前缀，6 小时）：8 只票一次搜索实测
       **94 秒**，而列表在盘中基本不变。阶段1「只读缓存秒出」如果每次
       都要重搜 94 秒，这个「秒出」就是假的 —— 用户还是会盯着空白等一分半。
    """
    cache_key = "ann:%s:%s:%s:%s:%d" % (stock_id, date_from, date_to,
                                         t1code, rows)
    c = cache_get(cache_key, ANNOUNCE_TTL_DAYS)
    if c is not None:
        return c
    q = urllib.parse.urlencode({
        "sortDir": "0", "sortByOptions": "DateTime", "category": "0",
        "market": "SEHK", "searchType": "0", "documentType": "-1",
        "t1code": t1code, "t2Gcode": "-2", "t2code": "-2",
        "stockId": stock_id, "fromDate": date_from, "toDate": date_to,
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
            out = json.loads(inner)        # ← 第二层，别漏
        except json.JSONDecodeError as exc:
            raise HKEXError("result 内层解析失败: %s" % str(inner)[:160]) from exc
    else:
        out = inner or []
    cache_put(cache_key, out)
    return out


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
                  timeout: int = 60, page_ranges: str | None = None) -> str:
    """提交解析任务，返回 task_id。

    HKEXnews 的 PDF 对第三方抓取是开放的（裸 UA / 无 UA / 浏览器 UA
    实测三种都是 200 + 完整 PDF），所以直接把公网 URL 交给 MinerU 抓，
    不用自己下载再上传 —— 少一次 1MB 传输，也少一处会 403 的地方。

    `page_ranges`（2026-10-15 实测可用）：形如 `"1-30"` / `"201-400"`。
    免费版**限 200 页**，招股章程动辄 500 页，传这个可以让 MinerU
    只处理指定页 —— 于是**既不用下载也不用上传**，一个依赖都不用加。
    """
    body_d = {"url": url, "model_version": model_version}
    if page_ranges:
        body_d["page_ranges"] = page_ranges
    body = json.dumps(body_d).encode()
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


def locate_pages(url: str, keywords, max_pages: int = 12) -> list:
    """用 PyMuPDF 定位「含禁售/锁定关键词」的页码（返回 (1-based, 0-based) 列表）。

    ⚠️⚠️ **PyMuPDF 在这里只做定位，绝不取数字** —— 这是本文件头那条
       「PDF 用未嵌入字体子集，PyMuPDF 会吞字（『交割日期後60日』丢『後6』，
        且 U+FFFD 计数为 0，任何缺失检测都发现不了）」的正确用法：
       定位只需要「这一页**含不含**某个关键词」，而子集字体丢字
       **不会整页丢光关键词**（常用字通常嵌得进）。真值一律来自 MinerU。

    为什么需要它：MinerU 免费版**限 200 页**，而招股章程/年报动辄 300~600 页
    —— 实测 8 只票 51 份公告里有 **5 份**因此直接失败，且失败的恰好是最有
    价值的那几份（`ipo_lockup` 的基石锁定期只写在招股章程里）。
    定位后只把这些页**切片成小 PDF** 再交给 MinerU，就绕开了页数上限。

    定位不到返回空列表 —— 调用方据此**走原来的整份路径**，
    宁可慢也不能因为「没定位到」就漏查。
    """
    try:
        import fitz                      # PyMuPDF
    except ImportError:
        return []
    try:
        raw = download_pdf(url)
    except Exception:                     # noqa: BLE001
        return []
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception:                     # noqa: BLE001
        return []
    try:
        hits = []
        for i, page in enumerate(doc):
            if len(hits) >= max_pages:
                break
            try:
                txt = page.get_text() or ""
            except Exception:             # noqa: BLE001
                continue
            if not txt:
                continue
            flat = re.sub(r"\s+", "", txt)
            if any(re.sub(r"\s+", "", k) in flat for k in keywords):
                hits.append((i + 1, i))
        return hits
    finally:
        try:
            doc.close()
        except Exception:                 # noqa: BLE001
            pass


def slice_pdf(url: str, pages, timeout: int = 90) -> bytes:
    """把指定页导出成一个小 PDF（页码列表为空则原样返回整份）。"""
    if not pages:
        return download_pdf(url, timeout)
    import fitz
    raw = download_pdf(url, timeout)
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        out = fitz.open()
        for _one, idx in pages:
            if 0 <= idx < doc.page_count:
                out.insert_pdf(doc, from_page=idx, to_page=idx)
        return out.tobytes()
    finally:
        try:
            doc.close()
        except Exception:                 # noqa: BLE001
            pass


def parse_pdf(url: str, token: str, model_version: str = "vlm",
              page_ranges: str | None = None) -> str:
    """一条龙：提交 -> 轮询 -> 取 markdown。

    ⚠️ 曾有个 bug：把 `mineru_poll()` 的返回值（**整个 data dict**）直接当
    zip_url 传给 `mineru_fetch_markdown`，于是报
    `urlopen error unknown url type: {'task_id'}`。
    `mineru_poll` 返回 dict，必须先取 `full_zip_url`。
    """
    data = mineru_poll(mineru_submit(url, token, model_version,
                                     page_ranges=page_ranges), token)
    zip_url = data.get("full_zip_url") or ""
    if not zip_url:
        raise HKEXError("MinerU 返回里没有 full_zip_url：%s" % sorted(data.keys()))
    return mineru_fetch_markdown(zip_url)


# ---------------- 绕过 MinerU 200 页上限（2026-10-15）----------------
# 症状：8 只票 51 份公告里，**招股章程（全球發售）全部失败**，报
#   `number of pages exceeds limit (200 pages), please split the file and try again`
#   —— 而基石投資者/主要股東的锁定期**只写在招股章程里**，恰好是最有价值的那几份。
#
# 试过并否决的路（都实测过，别再试）：
#   1) 本地切片再上传：容器里**没有任何 PDF 库**（fitz/pypdf 全无），
#      加依赖要先问用户；而且整份上传实测 30MB 要 **90 秒**（uri 传 403，
#      必须用 requests 且**不能带 Content-Type** —— OSS 预签名把 Content-Type
#      签进了签名，urllib 会自动加 `application/x-www-form-urlencoded` → 403）。
#   2) `extract/task` 不接受 `page_ranges`？—— **接受**（实测），所以连
#      下载+上传那 90 秒都不需要，直接让 MinerU 自己只取这些页。
MINERU_MAX_PAGES = 200          # 免费版硬上限（报错原文里的数字）
MINERU_MAX_CHUNKS = 12          # 12×200 = 2400 页封顶，防一夜烧光配额
# 探测范围（问文档页数用）：**起点远超任何真实公告 + 长度恰好 200 页**。
# 两个条件缺一不可，理由见 mineru_probe_page_count 的实测表。
MINERU_PROBE_RANGE = "5000-5199"

_PAGE_LIMIT_MARK = "exceeds limit"
_DOC_RANGE_RE = re.compile(r"document page range \(1-(\d+)\)")


def mineru_probe_page_count(url: str, token: str,
                            model_version: str = "vlm") -> int:
    """用一个**必然越界**的请求问出文档真实页数；问不到返回 0。

    实测（2026-10-15 深夜，02513 全球發售 504 页）—— 只有下表的中间形态
    能问出页数，两个细节都不能省：

    | page_ranges      | 服务端返回 |
    |------------------|------------|
    | `1-99999`        | `number of pages exceeds limit (200 pages)` ← ❌ 起点在文档内，先命中**长度**检查 |
    | `5000-5200`      | `page range 5000-5200 exceeds document page range (1-504)` ✅ |
    | `999900-1000000` | 同上 ✅ |

    即「起点超出文档页数」时才是越界错误、真实页数回显在里面；起点在文档内
    时先查「你要的页数 > 200」。所以探测范围必须**起点极大 + 长度 ≤ 200**
    （两条都满足，将来服务端把检查顺序对调也不会变）。

    ⚠️ 2026-10-15 首版是**假通过**（部署断言把 mineru_submit 换成 fake_submit
       直接 throw 越界串），真函数里同时埋着两个 bug：
       ① 用 `1-99999`（命中长度检查）；② 只 catch **提交**阶段的异常 ——
       而真实链路的越界错误是**轮询阶段**（`state=failed`）才回来的。
       两条叠加 → 真跑线上时探测恒返回 0（run5 的 5 份招股章程因此失败）。
       现在提交后**必须轮询**，两个阶段的错误串都要捞。
    """
    def _parse_count(exc: Exception) -> int:
        m = _DOC_RANGE_RE.search(str(exc))
        return int(m.group(1)) if m else 0

    try:
        tid = mineru_submit(url, token, model_version,
                            page_ranges=MINERU_PROBE_RANGE)
    except HKEXError as exc:
        return _parse_count(exc)
    try:
        mineru_poll(tid, token, poll_sec=5, max_wait=150)
    except HKEXError as exc:
        return _parse_count(exc)
    # 服务端居然接受了这个范围（文档真有 5000+ 页）——不猜页数，返回 0，
    # 调用方会给出明确报错，而不是编一个数字出来。
    return 0


def _chunk_cache_key(url: str, rng: str) -> str:
    """分块 markdown 的缓存键。

    ⚠️ 必须**逐块**落盘（不是等全部拼好再落）：MinerU 对同一个文件有
       「连续失败 5 次就暂时封禁」的滚动限额（报 `retry limit reached
       (5 attempts), please replace the file`）。整份提交 + 探测 + N 个块，
       任何一次失败都在消耗这个额度；如果一趟 run 的成果不能复用，
       几轮就会把额度烧完 —— run4 那 5 份招股章程就是这么被封的
       （确认过：一小时后同一文件又能提交，是滚动窗口不是永久）。
    """
    return "md:" + url + "#" + rng


def _parse_pdf_chunks(url: str, token: str, model_version: str, n: int) -> dict:
    """把 >200 页的文档按页切块解析，逐块缓存，最后拼接 markdown。

    返回 `{"md", "pages", "chunks", "truncated", "chunk_errors"}`；
    `chunk_errors` 是 {页段: 原因} —— **有洞时调用方不许把结果当完整文档用**
    （也不许缓存 merged md / 条款缓存），否则「少看了几页」会伪装成
    「这份公告没有条款」，属于最坏的一类静默失败。
    """
    parts, errors, truncated = [], {}, False
    starts = list(range(1, n + 1, MINERU_MAX_PAGES))
    for i, start in enumerate(starts):
        if i >= MINERU_MAX_CHUNKS:
            truncated = True
            break
        end = min(n, start + MINERU_MAX_PAGES - 1)
        rng = "%d-%d" % (start, end)
        ck = _chunk_cache_key(url, rng)
        c = cache_get(ck, MINEDOC_TTL_DAYS)
        part = c.get("md") if isinstance(c, dict) else None
        if not part:
            try:
                part = parse_pdf(url, token, model_version, page_ranges=rng)
            except HKEXError as exc:
                errors[rng] = str(exc)[:160]
                continue
            cache_put(ck, {"md": part, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        # 标出页码段：条款引用（quote）要能在拼接后的文本里**逐字**核对上，
        # 同时便于人工判断「这条出自第几页」。
        parts.append("\n\n<!-- MinerU 分块 %d-%d / 共 %d 页 -->\n\n"
                     % (start, end, n) + part)
    if not parts:
        raise HKEXError("MinerU 分块解析一份都没成功（n=%d）：%s"
                        % (n, "；".join("%s %s" % (k, v)
                                        for k, v in errors.items())[:200]))
    return {"md": "".join(parts), "pages": n, "chunks": len(parts),
            "truncated": truncated, "chunk_errors": errors}


def parse_pdf_auto(url: str, token: str, model_version: str = "vlm") -> dict:
    """解析 PDF；超 200 页时**自动按页分块**并拼接 markdown。

    返回 `{"md": str, "pages": int, "chunks": int, "truncated": bool,
           "chunk_errors": dict}`
    —— 页数/块数/是否截断/哪几块没成功都要往上报，不能只回一个字符串：
    调用方与界面需要知道「这份到底是全文、还是只解析了前 N 页 / 中间有洞」。

    ⚠️ `truncated=True` 或有 `chunk_errors` 时调用方**必须在 reason 里说明**。
       静默少看几页 = 界面看着正常、条款就是少几条，属于最坏的一类失败。

    流程（2026-10-15 深夜修正）：
      1. 已知页数 > 200（`pages:` 缓存）→ **直接分块**，不再拿整份去撞上限
         （每次撞 = 消耗一次 MinerU 的失败额度）。
      2. 否则先试整份；被 200 页上限拒绝才探测页数并缓存。
      3. 分块逐块解析、逐块落盘（见 _chunk_cache_key 注释）。
    """
    cached_n = cache_get("pages:" + url, PAGECOUNT_TTL_DAYS)
    n = cached_n if isinstance(cached_n, int) else 0
    if n > MINERU_MAX_PAGES:
        return _parse_pdf_chunks(url, token, model_version, n)
    try:
        md = parse_pdf(url, token, model_version)
        return {"md": md, "pages": 0, "chunks": 1, "truncated": False,
                "chunk_errors": {}}
    except HKEXError as exc:
        if _PAGE_LIMIT_MARK not in str(exc):
            raise
    n = mineru_probe_page_count(url, token, model_version)
    if n <= MINERU_MAX_PAGES:
        # 探不到页数（或页数明明没超却被拒）—— 不能假装成功，
        # 也不能瞎切：如实抛出去，调用方会把它记成 failed + 原因。
        raise HKEXError(
            "MinerU 报页数超限，但探测到的页数=%d（解析不了这份公告）" % n)
    cache_put("pages:" + url, n)
    return _parse_pdf_chunks(url, token, model_version, n)



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