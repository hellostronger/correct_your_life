# -*- coding: utf-8 -*-
"""港股「供给冲击」编排层：HKEXnews 公告 -> MinerU 解析 -> LLM 抽条款。

这个模块**只编排**，自己不碰网络细节与解析细节（分别在 hk_supply /
hk_lockup_llm）。它负责三件容易被忽略、但决定成败的事：

1. **区分两条语义完全不同的线**（用户 2026-10-12 明确要求分开显示）：
   - `ipo_lockup`  股东股份被锁定 -> 锁定期满那批股票**真的可以卖**
   - `issuer_pledge` 发行人承诺不再发新股 -> **不是股份解禁**
     把两者混在一张表里 = 让人以为「解禁后有货」，会误导判断。
2. **如实上报「未取到」**。抽不到条款时必须给可读原因，不能返回空列表
   让界面显示成「本来就没有禁售」（AGENTS.md 反复警告的那种假成功）。
3. **缓存**。解析一份公告要一次网络往返 + 一次 LLM 调用；不缓存的话
   每刷新一次页面就烧一次。缓存键是公告 URL，天然唯一。

它**不 import app**（app 的 import 要 80 秒且会起守护线程），
LLM 与 token 由调用方注入。
"""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import hk_lockup as HK
import hk_lockup_llm as HL
import hk_supply as HS

# 同一只票里**并发**解析几份公告。
# ⚠️ 为什么能并发（AGENTS.md 记过的老坑在这里不成立）：
#    MinerU 解析 + LLM 抽取都走网络，等待期间不占 CPU；
#    唯一被并发触碰的共享状态是 `HS.cache_*`，而缓存键是**公告 URL**
#    （天然唯一）、写入走 `tmp -> replace` 原子替换，所以不同线程写的
#    是不同文件。真正危险的 `res` 字典已完全移出工作线程（见 _one_doc）。
# ⚠️ 别开太大：MinerU 免费额度按份计，且服务端对同账号并发有上限；
#    4 是「冷启动 30 分钟 -> 约 6 分钟」这个性价比的拐点。
PARSE_WORKERS = max(1, int(os.environ.get("HK_PARSE_WORKERS") or 4))

# 公告标题里值得解析的关键词。
# ⚠️ 「增发/配售」这类融资公告里**才有** issuer_pledge（禁售承诺）；
#    招股书/上市文件里才有 ipo_lockup。两者都收，但**分类交给 LLM**，
#    不靠关键词猜 —— AGENTS.md 记过「这不是新闻不能靠关键词黑名单」那次教训。
ANNOUNCE_KW = ("配售", "認購", "认购", "可換股", "可换股", "債券", "债券",
               "發行", "发行", "供股", "認購協議", "招股", "上市文件",
               "基石", "禁售", "禁售期", "限制出售", "超額配股", "配售股份",
               # ⚠️ 「全球發售」是港股的**招股章程/发售文件**标准名称，而
               #    `ipo_lockup`（基石 6 个月 / 控股股东 6~12 个月）**只**写在
               #    里面。原列表靠「招股」两字命中「招股章程」，于是
               #    标题只写「全球發售」的票会**一份都选不中**
               #    （06658 实测：23 份公告里 2 份「全球發售」全被漏掉，
               #      结果报「没有配售/发行/招股类公告」）。
               "全球發售",
               # ⚠️ 激励授予类**确实**含限售条款（新增股份的锁定），
               #    所以保留在候选里；但它排在 _rank 的第 2 层，
               #    不会像现在这样把「月報表」这种纯日常公告顶上来。
               "獎勵", "奖励", "股份激勵", "股份獎勵")
# ⚠️ 刻意**不**包含「月報表 / 證券變動 / 翌日披露」：这些是每月例行披露，
#    里面没有禁售条款，解析它们纯属浪费 MinerU 配额 + 40 秒等待。
#    实测它们曾因「最近」而被优先选中，把真正有用的配售公告挤出 max_docs。

# 哪些公告**大概率**含禁售条款。仅用来**决定是否值得花一次解析**，
# 抽不抽得出来仍然由 LLM + 校验决定 —— 这里只是省钱，不是判定依据。
LIKELY_KW = ("禁售", "禁限", "鎖定", "锁定", "限制出售", "基石",
             "lock", "Lock", "LOCK")


def _is_routine_disclosure(title: str) -> bool:
    """纯日常披露（无禁售条款）—— 解析它纯属浪费 40 秒 + MinerU 配额。

    「月報表 / 證券變動 / 翌日披露」是港交所的例行申报，没有融资行为，
    因而不存在「禁售承诺」。⚠️ 标题里含「發行」（股份發行人的證券變動）
    会被 ANNOUNCE_KW 命中，所以必须在这里单独剔掉，
    仅仅在 _rank 里排到最后**不够** —— 占的仍是 max_docs 名额。
    """
    t = title or ""
    return any(k in t for k in ("月報表", "月报表", "翌日披露",
                                "證券變動", "证券变动"))


def _is_parsable(link: str) -> bool:
    """这份公告**能不能真的解析**。

    ⚠️ HKEXnews 会返回两种拿不到正文的行，选进来只会白烧一次 MinerU
       （约 40 秒 + 配额）**而且必然失败**：

       1. `發行人剛在本網站英文版面發布一項公告，相應中文版本或會／或不會在本版面發布。`
          —— 这是**英文版先发、中文版稍后补**时的占位行，FILE_LINK 指向
          `2026100801184_c.htm`。实测把它改写成 `.pdf` 是 **HTTP 404**
          （同一 doc id 下根本没有 PDF），所以「换个后缀救回来」这条路不通。
          它的标题里含「發行」，会被 ANNOUNCE_KW 命中 —— 01888 建滔積層板
          45 份公告里它是 rank 1，实测白白占掉一个 max_docs 名额并报
          `model_version 'vlm' cannot process html files`。
       2. `展示項目` 之类指向 `hk/dod/tc.html` 的行（06658 实测）。

    判据用 **FILE_LINK 的后缀**，而不是标题关键词：占位行的文案会变，
    「不是 PDF 就没法解析」这件事不会变。
    """
    return bool(link) and link.lower().split("?")[0].endswith(".pdf")


def _file_kb(raw: str) -> int | None:
    """`FILE_INFO` -> KB。⚠️ 单位**不固定**：实测同一次响应里同时出现
    `"1049KB"`、`"1M"`、`"29MB"`（智谱那份 1MB / 中期报告 29MB）。
    原来写 `int(x.rstrip("KB"))` -> 遇到 "1M" 直接 ValueError，
    **让整只票的分析全崩**（8 只里 6 只能跑、智谱直接挂）。
    取不到就返回 None —— 这个字段只用于展示，**不该让整条链路失败**。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.match(r"^([0-9.]+)\s*([KMG]?)B?$", s, re.I)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    mult = {"": 1 / 1024, "K": 1, "M": 1024, "G": 1024 * 1024}[m.group(2).upper()]
    return int(n * mult)


def _days_from_ann_date(s: str) -> int:
    """'13/09/2026 18:13' -> 到今天的天数（负数=过去）。"""
    try:
        d, m, y = s.split(" ")[0].split("/")
        dt = date(int(y), int(m), int(d))
    except (ValueError, IndexError):
        return 9999
    return (dt - date.today()).days


def _norm_date(s: str) -> str:
    try:
        d, m, y = s.split(" ")[0].split("/")
        return "%s-%s-%s" % (y, m, d)
    except (ValueError, IndexError):
        return ""


def pick_announcements(rows: list[dict], max_n: int = 0) -> list[dict]:
    """从公告列表里挑出值得解析的。**`max_n <= 0` = 全部解析，不设上限。**

    排序：先按「像解禁条款的优先」，再按「离今天近」（最近的融资最要紧）。

    ⚠️ 语义坑（2026-10-12 端到端实测发现）：我第一版把 max_n 实现成
    「**每个日期**最多 max_n 份」，于是 `max_docs=1` 对智谱完全无效 ——
    它有 5 份公告分处 5 个不同日期，全被选中，实测耗了 **315 秒**
    （MinerU 每份约 40 秒 + LLM 抽取）。参数名写着 max_docs、
    注释写着「每家最多 max_n 份」、实现却是「每天最多」——
    **三者不一致，而测试只覆盖了「同一天多份」那个场景，所以没发现**。
    这里改成总量上限，并把该场景补进测试。

    ⚠️⚠️ 默认值从 3 改成 0（= 不限），因为 `max_docs` 这个上限**从来不是
    数据源的限制，只是「每份约 40 秒」的时间预算**——而 2026-10-15 修好
    `fromDate` 参数后（见 hk_supply.search_announcements），8 只自选港股的
    真实候选数是 **46 份**，`max_docs=3` 只看得到 21 份：

    | 股票 | 候选 | max_docs=3 能看到 |
    |---|---|---|
    | 02513 智谱 | 10 | 3 |
    | 00136 中國儒意 | 8 | 3 |
    | 00100 MINIMAX-W | 7 | 3 |
    | 01024 快手-W | 7 | 3 |
    | 01368 特步國際 | 6 | 3 |
    | 09696 | 5 | 3 |
    | 01888 建滔積層板 | 4 | 3 |
    | 06658 | 0 | 0 |

    在搜索只能看到最近 30 天的时代，每只票本来就只有 0~3 份候选，
    上限形同虚设；修好之后它才真正生效 —— 而它生效的方式是
    **「每只票有一半以上的相关公告根本没被看过就下结论」**，
    这本身就是 AGENTS.md 反复警告的假成功。
    现在改成：全量解析 + 并发（见 PARSE_WORKERS），30 分钟的冷启动
    压到 ~6 分钟，而且条款缓存 30 天，**之后每次点击都是秒开**。
    `max_docs` 保留为「安全上限」，不再当默认口径。
    """
    cands = []
    for r in rows or []:
        title = r.get("TITLE") or ""
        link = r.get("FILE_LINK") or ""
        if not link:
            continue
        if not any(k in title for k in ANNOUNCE_KW):
            continue
        # ⚠️ 纯日常披露直接**排除**（不分层）——它们没有禁售条款，
        #   而标题里的「發行」会让 ANNOUNCE_KW 命中（「股份發行人的證券變動」）。
        #   实测智谱的月報表曾被选进来，挤掉了真正有用的配售公告。
        if _is_routine_disclosure(title):
            continue
        # ⚠️ 非 PDF 一律不选（占位行 / 展示项目页）—— 判据见 _is_parsable。
        #   否则它会占掉 max_docs 名额、再花 40 秒得到一个必然失败的报错。
        if not _is_parsable(link):
            continue
        off = _days_from_ann_date(r.get("DATE_TIME") or "")
        cands.append({
            "date": _norm_date(r.get("DATE_TIME") or ""),
            "title": title,
            "url": HS.pdf_url(link),
            "size_kb": _file_kb(r.get("FILE_INFO")),
            "likely": any(k in title for k in LIKELY_KW),
            "_offset": off,
        })
    # 排序：**相关度分层**优先，日期只在同层内 tiebreak。
    # ⚠️ 见 _rank() 的说明 —— 只按日期排会挑中「月報表」这类日常公告，
    #   把真正写着禁售条款的配售公告切掉（2026-10-13 实测 8/8 全「未识别到」）。
    cands.sort(key=lambda c: (_rank(c["title"]), abs(c["_offset"])))
    out = []
    for c in cands[:max_n if max_n and max_n > 0 else len(cands)]:
        c.pop("_offset", None)
        out.append(c)
    return out


def _rank(title: str) -> int:
    """公告**相关度**分层，越小越该解析（用于挑公告，不是判「有没有条款」）。

    ⚠️ 2026-10-13 真 bug：原来只按「距今天近」排序，于是 `max_docs=3`
    挑中智谱的「授予獎勵 / 月報表 / 股份激勵」—— **三份恰好都没有禁售条款**，
    而真正写着「交割日期後60日止」的配售公告排第 5 被切掉，
    结果 8 只票全部显示「未在公告中识别到禁售条款」。
    实测 `max_docs=5` 才选中它并正确抽出 2 条 60 日。
    → 「最近」不等于「相关」：月报表/激励授予是日常公告，**禁售条款在融资公告里**。
    """
    t = title or ""
    if any(k in t for k in ("禁售", "禁限", "鎖定", "锁定", "限制出售",
                            "基石", "Lock", "LOCK", "lock")):
        return 0            # 明确写着禁售/基石，几乎必然有条款
    if any(k in t for k in ("配售", "認購", "认购", "可換股", "可换股",
                            "債券", "债券", "供股", "發行", "发行",
                            "招股", "上市文件", "超額配股", "全球發售")):
        return 1            # 融资类 —— 禁售承诺写在这里
    if any(k in t for k in ("月報表", "月报表", "翌日披露", "證券變動", "证券变动")):
        return 3            # 日常公告，几乎不会有条款，排最后别浪费配额
    return 2


def analyze_one(code: str, name: str, token: str, ask_fn=None,
                max_docs: int = 0, date_from: str = "", date_to: str = "",
                refresh: bool = False, progress=None,
                rows: list | None = None, cache_only: bool = False) -> dict:
    """单只港股的完整分析。返回

        {
          "code", "name", "ok", "error",
          "ipo_lockup":  [...],        # 股东股份锁定期（这才是解禁）
          "issuer_pledge": [...],      # 发行人不再发新股（不是解禁）
          "announcements": [...],      # 看过哪些公告（含未能解析的原因）
          "boundary": "HKEXnews 仅保留近 12 个月，更早的公告查不到",
        }

    ⚠️ `ok=False` 时 `announcements` 里仍然保留**可读原因**，界面要显示出来。
       静默返回空列表 = 用户以为「这票没有解禁」，实际是没取到。
    """
    res = {"code": code, "name": name, "ok": False, "error": None,
           "ipo_lockup": [], "issuer_pledge": [], "announcements": [],
           "boundary": ("HKEXnews 标题搜索只保留近 %d 天，更早的公告查不到"
                        % HS.HKEX_RETENTION_DAYS)}
    try:
        sid, resolved = HS.resolve_stock_id(code)
    except Exception as exc:                        # noqa: BLE001
        res["error"] = "查不到该股票的 HKEX 内部编号：%s" % str(exc)[:120]
        return res
    res["name"] = name or resolved
    res["stock_id"] = sid

    if not date_to:
        date_to = date.today().isoformat().replace("-", "")
    if not date_from:
        date_from = (date.today() - timedelta(days=HS.HKEX_RETENTION_DAYS)) \
            .isoformat().replace("-", "")

    try:
        # ⚠️ `rows` 由 analyze_many 的预扫描传入 —— 同一批搜索结果复用，
        #    不要为了拿分母把 HKEXnews 请求数翻倍（AGENTS.md：密集请求会被 WAF 记）。
        if rows is None:
            rows = HS.search_announcements(sid, date_from, date_to)
    except Exception as exc:                        # noqa: BLE001
        res["error"] = "搜索公告失败：%s" % str(exc)[:160]
        return res

    res["announcements_found"] = len(rows)
    picks = pick_announcements(rows, max_n=max_docs)
    res["candidates"] = len(picks)
    if not picks:
        res["ok"] = True                      # 真的没有融资类公告
        res["error"] = ("HKEXnews 近 %d 天内没有配售/发行/招股类公告"
                        "（这不是「没有解禁」，是「没有可查的公告」）"
                        % HS.HKEX_RETENTION_DAYS)
        return res

    parsed_any = False
    res["phase"] = "cache" if cache_only else "parse"

    # ---------- 快通道：只读条款缓存，零网络零 LLM ----------
    # ⚠️ 为什么要它（2026-10-15 用户选「先出已有缓存，其余后台补」）：
    #   冷启动要几分钟，而**已经查过的**结果本来就在磁盘上。
    #   先把缓存里有的秒级吐出来给页面，剩下的在后台慢慢补 ——
    #   这样「点一下要等 6 分钟才看到东西」变成「立刻看到已知的，
    #   进度条继续跑，补完自动刷新」。
    #   ⚠️ 只读 `clauses:` 缓存，**不**碰 MinerU、**不**调 LLM：
    #     `extract_clauses()` 每次必调 LLM（hk_lockup_llm:181），放进快通道
    #     就等于「快通道」名不副实，还会在用户点按钮的瞬间烧掉一串调用。
    if cache_only:
        for p in picks:
            item = {"date": p["date"], "title": p["title"], "url": p["url"],
                    "status": "pending", "reason": "尚未解析（正在后台补）",
                    "clauses": 0}
            c = None if refresh else HS.cache_get("clauses:" + p["url"],
                                                  HS.CLAUSE_TTL_DAYS)
            if c is not None:
                item["status"] = "cached"
                item["clauses"] = len(c.get("clauses") or [])
                for cl in (c.get("clauses") or []):
                    res[cl["type"]].append(dict(cl, _date=p["date"],
                                                _title=p["title"]))
                parsed_any = True
            res["announcements"].append(item)
        res["ok"] = True
        res["cached_only"] = True
        if not parsed_any:
            res["error"] = None      # 还没查过，不是「没有」—— 别提前下结论
        return res

    # 进度回调（2026-10-13 用户要求进度条）：每个阶段/每份公告都报一次。
    # 「done/total」以**已处理的公告份数**为单位 —— 一次 MinerU 解析约 40 秒，
    # 按股票数报进度会长时间停在同一个数字上（8 只票里 5 只可能是 0 份公告），
    # 看起来像卡死。extra 里带上 status，让前端能显示「命中缓存/正在解析」。
    prog_lock = threading.Lock()
    prog = {"done": 0}

    def _tick(stage, done, status=""):
        if progress:
            try:
                progress({"stage": stage, "code": code, "name": name,
                          "done": done, "total": len(picks), "status": status})
            except Exception:                # noqa: BLE001
                pass

    def _bump(status=""):
        """已完成份数 +1（**必须单调不减**）。锁内自增，否则并发下丢数。"""
        with prog_lock:
            prog["done"] += 1
            d = prog["done"]
        _tick("parse", d, status)
        return d

    def _done_now():
        """当前**已完成**份数（与 `_bump` 的写共用同一把锁）。

        阶段 tick（查缓存 / 解析 PDF）报「已完成 + 1」= 正在处理第 N 份：
        进入一份公告时它还没完成，若报已完成数会显示成「0/3」，看着像没开始。
        """
        with prog_lock:
            return prog["done"]

    _tick("search", 0)

    def _one_doc(p):
        """解析**一份**公告。返回 (item, clauses) —— **绝不碰 res**。

        ⚠️ 为什么把共享状态全部踢出工作线程：`res["announcements"].append()`
           和 `res[cl["type"]].append()` 是对同一个 dict 的并发写，
           list.append 在 CPython 里恰好是原子的，但**遍历它的那一端**
           （summarize / 渲染）没有锁 —— 宁可让工作线程只返回纯数据，
           由主线程按固定顺序合并。合并顺序 = pick 顺序，
           所以并发**不影响输出的确定性**。
        """
        item = {"date": p["date"], "title": p["title"], "url": p["url"],
                "status": "pending", "reason": "", "clauses": 0}
        clauses = []
        # 进入每份公告先报一次「查缓存」（10-13 初版即有，10-15 重构 _one_doc
        # 时丢了）：这段很快，但前端靠 status 区分「命中缓存秒过」与「进 MinerU」。
        _tick("parse", _done_now() + 1, "查缓存")
        cache_key = "clauses:" + p["url"]
        if not refresh:
            c = HS.cache_get(cache_key, HS.CLAUSE_TTL_DAYS)
            if c is not None:
                item["status"] = "cached"
                item["clauses"] = len(c.get("clauses") or [])
                return item, list(c.get("clauses") or []), True

        # 1) PDF -> markdown（MinerU；**超 200 页自动按页分块**）
        #    ⚠️ 2026-10-15 前这里调 `HS.parse_pdf`，于是招股章程（全球發售）
        #       全部以 `number of pages exceeds limit (200 pages)` 失败 ——
        #       而基石投資者/主要股東的锁定期只写在招股章程里。
        mcache_key = "md:" + p["url"]
        # ⚠️ 这里原来写 `suffix=".md"`，而 `HS.cache_put()` **没有 suffix 参数**、
        #   永远写 `.json` → 读`.md`/写`.json` 两边从来对不上，
        #   **md 缓存从上线起就没命中过一次**（每次扫描都在重新解析全部公告）。
        #   实测依据（2026-10-15）：101 缓存目录 110 个文件全是 `.json`、零个 `.md`。
        #   现在两边同为默认 `.json`。
        cached = None if refresh else HS.cache_get(mcache_key, HS.MINEDOC_TTL_DAYS)
        md, pages, chunks, truncated = None, 0, 0, False
        chunk_errors = {}
        if isinstance(cached, dict):           # 缓存里存 {md: ..., pages: ...}
            md = cached.get("md")
            pages = int(cached.get("pages") or 0)
            chunks = int(cached.get("chunks") or 0)
            truncated = bool(cached.get("truncated"))
        elif cached:                           # 旧格式（纯文本）也能读
            md = cached
        if md:
            item["status"] = "parsed(cached)"
        else:
            if not token:
                item["status"] = "skipped"
                item["reason"] = "未配置 MinerU token，跳过解析"
                return item, [], False
            # MinerU 解析每份约 40 秒 —— 先报一次状态，前端显示「解析 PDF」
            # 而不是长时间停在「查缓存」（10-13 初版即有，重构 _one_doc 时丢了）。
            _tick("parse", _done_now() + 1, "解析 PDF（MinerU，约 40 秒/份）")
            try:
                pr = HS.parse_pdf_auto(p["url"], token)
                md = pr["md"]
                pages, chunks = pr["pages"], pr["chunks"]
                truncated = pr["truncated"]
                chunk_errors = dict(pr.get("chunk_errors") or {})
                if chunk_errors:
                    # 有洞：**不缓存 merged md** —— 块缓存（HS._chunk_cache_key）
                    # 已经把成功的部分保住了，下次只重试缺的块；若在这里缓存了，
                    # 这一份就会带着洞被当成完整文档用 7 天。
                    item["chunk_errors"] = chunk_errors
                else:
                    HS.cache_put(mcache_key, {"md": md, "ts": _now(), "pages": pages,
                                              "chunks": chunks,
                                              "truncated": truncated})
                item["status"] = ("parsed(%d块/%d页)" % (chunks, pages)
                                  if chunks > 1 else "parsed")
            except Exception as exc:            # noqa: BLE001
                item["status"] = "failed"
                item["reason"] = "PDF 解析失败：%s" % str(exc)[:160]
                return item, [], False
        if pages:
            item["pages"] = pages
        if chunks > 1:
            item["chunks"] = chunks

        # 2) LLM 抽条款 + 三道校验
        r = HL.extract_clauses(md, p["title"][:40], code, p["date"],
                               ask_fn=ask_fn)
        if r.get("error"):
            item["status"] = "failed"
            item["reason"] = r["error"][:200]
            return item, [], False
        item["status"] = "ok" if r["clauses"] else "no_clause"
        item["clauses"] = len(r["clauses"])
        item["rejected"] = r.get("rejected", 0)
        if not r["clauses"]:
            item["reason"] = "该公告里没有识别到禁售/锁定条款"
        if truncated:
            # ⚠️ 绝不能静默：只解析了前 N 页却报「没识别到条款」，会被当成
            #   「这份公告真的没锁定期」，而真相是**后面那些页根本没看**。
            item["truncated"] = True
            cap = HS.MINERU_MAX_PAGES * HS.MINERU_MAX_CHUNKS
            item["reason"] = ("⚠ 公告 %d 页，超过分块上限，只解析了前 %d 页"
                              "（后续页未查，可能漏条款）%s"
                              % (pages, cap,
                                 ("；" + item["reason"]) if item.get("reason")
                                 else ""))
        if chunk_errors:
            # 有洞 = 这部分页**根本没看**。写进 reason（界面按它展示），
            # 并且**不写条款缓存** —— 块缓存已保住进度，下次只重试缺的块，
            # 补全后自然会重抽一遍；若这里缓存了，30 天内都不会再补。
            item["reason"] = ("⚠ 分块解析有缺：%s 这几段没成功（可能漏条款）%s"
                              % ("、".join(sorted(chunk_errors)),
                                 ("；" + item["reason"]) if item.get("reason")
                                 else ""))
        if r.get("partial") and r.get("note"):
            # ⚠️ 分批抽取有片段**没查过**（批数封顶 / 某批失败）——
            #   「没查到」与「真的没有」是两回事，必须显示出来；
            #   且**不写条款缓存**，否则这个「没查完」会被固化 30 天
            #   （与 chunk_errors 同一条纪律）。
            item["reason"] = (r["note"] + ("；" + item["reason"])
                              if item.get("reason") else r["note"])
        if not chunk_errors and not r.get("partial"):
            HS.cache_put(cache_key, {"clauses": r["clauses"],
                                     "rejected": r.get("rejected", 0),
                                     "ts": _now()})
        return item, list(r["clauses"]), True

    workers = max(1, min(PARSE_WORKERS, len(picks)))
    if workers == 1:                             # 串行（测试 / 单份）
        out = []
        for p in picks:
            try:
                out.append(_one_doc(p))
            except Exception as exc:             # noqa: BLE001
                out.append(({"date": p["date"], "title": p["title"],
                             "url": p["url"], "status": "failed",
                             "reason": "解析异常：%s" % str(exc)[:160],
                             "clauses": 0}, [], False))
            _bump()
    else:
        out = [None] * len(picks)

        def _work(i):
            try:
                out[i] = _one_doc(picks[i])
            except Exception as exc:             # noqa: BLE001
                out[i] = ({"date": picks[i]["date"], "title": picks[i]["title"],
                           "url": picks[i]["url"], "status": "failed",
                           "reason": "解析异常：%s" % str(exc)[:160],
                           "clauses": 0}, [], False)
            _bump()

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_work, range(len(picks))))

    # 主线程按 pick 顺序合并（并发只影响速度，不影响结果顺序）
    for item, clauses, seen in out:
        res["announcements"].append(item)
        if seen:
            parsed_any = True
        for cl in clauses:
            cl = dict(cl)
            # 🔴 解禁日 = 起算锚点日期 + 期限。**算不出就是 None**，
            #    下游渲染成「—」并说明缺起算日；绝不用公告发布日近似
            #    （AGENTS.md：抓取时间 / 公告时间 ≠ 事实时间）。
            ud = HL._unlock_date(cl.get("anchor_date"), cl.get("days") or 0,
                                 cl.get("raw") or "")
            cl["unlock_date"] = ud.isoformat() if ud else None
            # 没有股数就没有金额。issuer_pledge 按定义锁的是「不许再发新股」，
            # 0 股被锁 —— 页面据此写「不涉及股份」，而不是显示 0 元。
            cl["amount_shares"] = cl.get("shares")
            cl["amount_pct"] = cl.get("pct")
            res[cl["type"]].append(dict(
                cl, _date=item["date"], _title=item["title"]))

    res["ok"] = True
    if not parsed_any and not res["announcements"]:
        res["error"] = "没有任何公告可解析"
    return res


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def analyze_many(codes: list[str], names: dict | None = None, token: str = "",
                 ask_fn=None, max_docs: int = 0,
                 progress_fn=None, refresh: bool = False,
                 cache_only: bool = False) -> list[dict]:
    """多只港股。**逐只捕获异常** —— 一只票取数失败不能让整批失败。

    `progress_fn(stage, done, total, code, name, status)` 用于进度条：
    `done/total` 是**已处理的公告份数**（不是股票数）。
    ⚠️ `max_docs=0`（默认）= 每只票**全部**候选都解析，于是分母不能再用
       `len(codes) * max_docs` 算 —— 那样会算成 0。真实份数要先搜一遍才知道，
       而搜索本身很快（8 只票实测 < 10 秒），所以这里**先全部搜、
       拿到真实分母，再开跑**。进度条的分母是**估的**、进度是**真的**，
       宁可分母最终修正，也不要一路显示 1/0。
    """
    names = names or {}
    out = []
    # 先搜一遍拿真实分母（只搜不解析：search_announcements 无副作用）
    pre = []
    for c in codes:
        try:
            sid, resolved = HS.resolve_stock_id(c)
            frm = (date.today() - timedelta(days=HS.HKEX_RETENTION_DAYS)) \
                .strftime("%Y%m%d")
            to = date.today().strftime("%Y%m%d")
            rows = HS.search_announcements(sid, frm, to)
            pre.append((c, len(pick_announcements(rows, max_n=max_docs)), rows))
        except Exception:                     # noqa: BLE001
            pre.append((c, max(1, int(max_docs or 1)), None))
    total_docs = max(1, sum(n for _c, n, _r in pre))
    # **单调递增**的已完成份数。
    # ⚠️ 为什么不用 `_emit(..., add=1)` 累加：analyze_one 报的是「**这一只票**
    #    里的第几份」，每只票都从 1 重新开始 —— 直接透传会让进度条在
    #    换股票时**跳回去**（8 只票就是 1/3 → 0/3 → 1/3…），看起来像卡死又重试。
    #    所以要加上「前面几只票已完成的份数」作为偏移。
    done_base = 0
    max_seen = 0

    def _emit(stage, done, total, code, name_, status):
        nonlocal max_seen
        max_seen = max(max_seen, int(done or 0))
        if progress_fn:
            try:
                progress_fn(stage, max_seen, total, code, name_, status)
            except Exception:                    # noqa: BLE001
                pass

    _emit("start", 0, total_docs, "", "",
          f"共 {len(codes)} 只港股，预计最多 {total_docs} 份公告")
    for c, _n, _rows in pre:
        try:
            out.append(analyze_one(
                c, names.get(c, ""), token, ask_fn=ask_fn, max_docs=max_docs,
                refresh=refresh, rows=_rows, cache_only=cache_only,
                # 直接透传「第几份」并加上前面几只票的偏移 —— 不自己 +1：
                # 一份要 40 秒，只在**做完**时 +1 会让进度条 40 秒纹丝不动。
                progress=lambda d, _b=done_base, _c=c: _emit(
                    d.get("stage", "parse"), _b + d.get("done", 0),
                    total_docs, _c, names.get(_c, ""), d.get("status", ""))))
        except Exception as exc:                 # noqa: BLE001
            out.append({"code": c, "name": names.get(c, ""), "ok": False,
                        "error": "分析异常：%s" % str(exc)[:200],
                        "ipo_lockup": [], "issuer_pledge": [], "announcements": []})
        # 这只票处理完，把它的份数累进偏移（取 max 而不是加，避免重复计数）
        done_base = max(done_base, max_seen)
    # ⚠️ 收尾必须报 done=total（而不是 max_seen）：
    #   total 是**估算**（有公告的票通常少于 max_docs），不补齐的话
    #   进度条永远停在比如 87% 然后突然换成结果，看着像没跑完。
    #   而且任务此时**确实已经完成**，报 100% 是事实不是修饰。
    if progress_fn:
        try:
            progress_fn("done", total_docs, total_docs, "", "", "完成")
        except Exception:                        # noqa: BLE001
            pass
    return out


def summarize(rs: list[dict]) -> str:
    """人读摘要（微信/日报用）。**每只票都要有交代**，不许静默省略。"""
    L = []
    for r in rs or []:
        head = "%s %s" % (r.get("code"), r.get("name") or "")
        if not r.get("ok"):
            L.append(f"· {head}：⚠ 未取到 —— {r.get('error') or '未知原因'}")
            continue
        parts = []
        for label, key, unit in (("股东锁定期", "ipo_lockup", "个月"),
                                 ("发行人禁售承诺", "issuer_pledge", "个月")):
            for c in r.get(key) or []:
                d = c.get("days") or 0
                txt = f"{d/30:.1f}个月" if d >= 30 else f"{d}日"
                parts.append(f"{label} {txt}"
                             + (f"（{c['_date']} 公告）" if c.get("_date") else ""))
        L.append(f"· {head}：" + ("；".join(parts) if parts else "未在公告中识别到禁售条款"))
    return "\n".join(L)