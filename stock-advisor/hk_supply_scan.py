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
import re
from datetime import date, timedelta

import hk_lockup as HK
import hk_lockup_llm as HL
import hk_supply as HS

# 公告标题里值得解析的关键词。
# ⚠️ 「增发/配售」这类融资公告里**才有** issuer_pledge（禁售承诺）；
#    招股书/上市文件里才有 ipo_lockup。两者都收，但**分类交给 LLM**，
#    不靠关键词猜 —— AGENTS.md 记过「这不是新闻不能靠关键词黑名单」那次教训。
ANNOUNCE_KW = ("配售", "認購", "认购", "可換股", "可换股", "債券", "债券",
               "發行", "发行", "供股", "認購協議", "招股", "上市文件",
               "基石", "禁售", "禁售期", "限制出售", "超額配股", "配售股份",
               # ⚠️ 激��授予类**确实**含限售条款（新增股份的锁定），
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


def pick_announcements(rows: list[dict], max_n: int = 6) -> list[dict]:
    """从公告列表里挑出值得解析的，**总量上限 max_n 份**。

    排序：先按「像解禁条款的优先」，再按「离今天近」（最近的融资最要紧）。

    ⚠️ 语义坑（2026-10-12 端到端实测发现）：我第一版把 max_n 实现成
    「**每个日期**最多 max_n 份」，于是 `max_docs=1` 对智谱完全无效 ——
    它有 5 份公告分处 5 个不同日期，全被选中，实测耗了 **315 秒**
    （MinerU 每份约 40 秒 + LLM 抽取）。参数名写着 max_docs、
    注释写着「每家最多 max_n 份」、实现却是「每天最多」——
    **三者不一致，而测试只覆盖了「同一天多份」那个场景，所以没发现**。
    这里改成总量上限，并把该场景补进测试。
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
    for c in cands[:max(0, max_n)]:          # ← 总量截断，不再按日期分组
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
                            "招股", "上市文件", "超額配股")):
        return 1            # 融资类 —— 禁售承诺写在这里
    if any(k in t for k in ("月報表", "月报表", "翌日披露", "證券變動", "证券变动")):
        return 3            # 日常公告，几乎不会有条款，排最后别浪费配额
    return 2


def analyze_one(code: str, name: str, token: str, ask_fn=None,
                max_docs: int = 3, date_from: str = "", date_to: str = "",
                refresh: bool = False, progress=None) -> dict:
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
        rows = HS.search_announcements(sid, date_from, date_to)
    except Exception as exc:                        # noqa: BLE001
        res["error"] = "搜索公告失败：%s" % str(exc)[:160]
        return res

    res["announcements_found"] = len(rows)
    picks = pick_announcements(rows, max_n=max_docs)
    if not picks:
        res["ok"] = True                      # 真的没有融资类公告
        res["error"] = ("HKEXnews 近 %d 天内没有配售/发行/招股类公告"
                        "（这不是「没有解禁」，是「没有可查的公告」）"
                        % HS.HKEX_RETENTION_DAYS)
        return res

    parsed_any = False
    # 进度回调（2026-10-13 用户要求进度条）：每个阶段/每份公告都报一次。
    # 「done/total」以**已处理的公告份数**为单位 —— 一次 MinerU 解析约 40 秒，
    # 按股票数报进度会长时间停在同一个数字上（8 只票里 5 只可能是 0 份公告），
    # 看起来像卡死。extra 里带上 status，让前端能显示「命中缓存/正在解析」。
    def _tick(stage, idx, status=""):
        if progress:
            try:
                progress({"stage": stage, "code": code, "name": name,
                          "done": idx, "total": len(picks), "status": status})
            except Exception:                # noqa: BLE001
                pass

    _tick("search", 0)
    for pi, p in enumerate(picks, 1):
        item = {"date": p["date"], "title": p["title"], "url": p["url"],
                "status": "pending", "reason": "", "clauses": 0}
        # 进入这份就报它的序号（1-based）—— 进度条立刻从 0 跳到 1，
        # 而不是等这 40 秒做完才动。
        _tick("parse", pi, "第 %d/%d 份，查缓存" % (pi, len(picks)))
        cache_key = "clauses:" + p["url"]
        if not refresh:
            c = HS.cache_get(cache_key, HS.CLAUSE_TTL_DAYS)
            if c is not None:
                item["status"] = "cached"
                for cl in (c.get("clauses") or []):
                    res[cl["type"]].append(dict(cl, _date=p["date"],
                                                _title=p["title"]))
                item["clauses"] = len(c.get("clauses") or [])
                parsed_any = True
                res["announcements"].append(item)
                continue

        # 1) PDF -> markdown（MinerU）
        mcache_key = "md:" + p["url"]
        md = None if refresh else HS.cache_get(mcache_key, HS.MINEDOC_TTL_DAYS,
                                               suffix=".md")
        if isinstance(md, dict):               # 缓存里存 {md: ...}
            md = md.get("md")
        if md:
            item["status"] = "parsed(cached)"
        else:
            if not token:
                item["status"] = "skipped"
                item["reason"] = "未配置 MinerU token，跳过解析"
                res["announcements"].append(item)
                _tick("parse", pi, "跳过")
                continue
            # 真正要花时间的一步（约 40 秒/份）
            _tick("parse", pi, "第 %d/%d 份 · 解析 PDF（MinerU，约 40 秒）"
                 % (pi, len(picks)))
            try:
                md = HS.parse_pdf(p["url"], token)
                HS.cache_put(mcache_key, {"md": md, "ts": _now()})
                item["status"] = "parsed"
            except Exception as exc:            # noqa: BLE001
                item["status"] = "failed"
                item["reason"] = "PDF 解析失败：%s" % str(exc)[:160]
                res["announcements"].append(item)
                continue

        # 2) LLM 抽条款 + 三道校验
        r = HL.extract_clauses(md, p["title"][:40], code, p["date"],
                               ask_fn=ask_fn)
        if r.get("error"):
            item["status"] = "failed"
            item["reason"] = r["error"][:200]
            res["announcements"].append(item)
            continue
        item["status"] = "ok" if r["clauses"] else "no_clause"
        item["clauses"] = len(r["clauses"])
        item["rejected"] = r.get("rejected", 0)
        if not r["clauses"]:
            item["reason"] = "该公告里没有识别到禁售/锁定条款"
        for cl in r["clauses"]:
            res[cl["type"]].append(dict(cl, _date=p["date"], _title=p["title"]))
        HS.cache_put(cache_key, {"clauses": r["clauses"],
                                 "rejected": r.get("rejected", 0), "ts": _now()})
        parsed_any = True
        res["announcements"].append(item)

    res["ok"] = True
    if not parsed_any and not res["announcements"]:
        res["error"] = "没有任何公告可解析"
    return res


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def analyze_many(codes: list[str], names: dict | None = None, token: str = "",
                 ask_fn=None, max_docs: int = 3,
                 progress_fn=None, refresh: bool = False) -> list[dict]:
    """多只港股。**逐只捕获异常** —— 一只票取数失败不能让整批失败。

    `progress_fn(stage, done, total, code, name, status)` 用于进度条：
    `done/total` 是**已处理的公告份数**（不是股票数）。`total` 是估算值
    （`len(codes) * max_docs`），所以收尾时报 `done == total` 让进度条填满。
    """
    names = names or {}
    out = []
    total_docs = max(1, len(codes) * max(1, int(max_docs or 1)))
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
    for c in codes:
        try:
            out.append(analyze_one(
                c, names.get(c, ""), token, ask_fn=ask_fn, max_docs=max_docs,
                refresh=refresh,
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