"""holiday_calendar.py —— 中国法定节假日/调休日历 → 交易日判定（唯一真源）。

## 为什么必须有这个模块

项目里判断「今天是不是交易日」的地方有 9 处，原先全是
`d.weekday() < 5`（周一~周五），注释写着「节假日从简」。后果不是「多跑一天空转」
这么轻——2026-10-01 国庆当天，本地会把盘中采样、盘后快照、止盈扫描、模拟盘轮次
全部当交易日跑一遍，而行情源在休市日返回的是**上一个交易日**的数据，于是
`sa_sector_snapshots` 会写进错标日期的行，`paper_trading` 会拿陈旧价判止盈。

## 数据源：国务院办公厅的放假通知（权威、每年一次）

官方源是《中国政府网》上国务院办公厅《关于 YYYY 年部分节假日安排的通知》，
例如 2026 年那份（国办发明电〔2025〕7 号，2025-11-04 发布）：

    一、元旦：1月1日（周四）至3日（周六）放假调休，共3天。1月4日（周日）上班。
    七、国庆节：10月1日（周四）至7日（周三）放假调休，共7天。9月20日（周日）、10月10日（周六）上班。

抽取走 `notice_parser`（LLM 出 JSON + 确定性自校验），不写死正则——措辞每年微调，
正则每年 11 月都要修一次。**调休上班日必须抽出来**：那些日子是周六/周日却是交易日，
只判「假期区间」会把它们错判成休市。

## 为什么不自己维护一张表

`scripts/load_all_valuation.py` 已经写过这条教训：每年调休都会变，
**错的表比没有表更糟**（它会让采集静默跳过半个交易日）。所以缓存只是产物，
真源永远是公告；缓存里记 `source_url`/`fetched_at`/`used`（llm 还是兜底）以便核验。

## 三态语义（与 data_service/sources.py 一致）

`is_trading_day()` 返回 True / False / **None**：
`None` = 「拿不到日历，日历可能挂了」——**绝不能当休市**，否则采集会被静默跳过。
调用方（如板块盘后采集）必须区分 None 与 False。

## 交叉校验

交易所自己的交易日历（`akshare.tool_trade_date_hist_sina()`）是另一个独立源。
`crosscheck_akshare()` 逐日对比并把差异记进缓存的 `crosscheck` 字段：
不一致时**只报告不修改**，让差异可见（这跟 config 的 `unknown` 覆盖率一个思路）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

import notice_parser as np  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "data" / "holidays.json"

SEARCH_URL = "https://sousuo.www.gov.cn/search-gov/data"
# 政策文件库里的正文页（zhengceku）与国务院公报（gongbao）都收录了同一份通知，
# 前者是首选（正文更干净），公报版作为兜底。
TITLE_HINT = "部分节假日安排"

_lock = threading.Lock()
_cache: dict[str, dict] = {}          # year(str) -> 该年日历
_meta: dict = {"updated_at": "", "source_url": "", "used": "", "model": "",
               "notes": [], "crosscheck": {}}
_loaded = False
_ex_days: set[date] | None = None     # akshare 交易日历（进程内缓存；None=还没拉过）


# ==========================================================================
# 缓存读写
# ==========================================================================

def _load_cache() -> None:
    global _loaded, _meta
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    _cache.update(raw.get("years") or {})
    _meta.update({k: v for k, v in raw.items() if k != "years"})


def _save_cache() -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(_meta)
    payload["years"] = _cache
    payload["version"] = 1
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CACHE_FILE)


# ==========================================================================
# ① 找公告（中国政府网检索接口）
# ==========================================================================

def find_notice(year: int, *, timeout: int = 20) -> dict | None:
    """检索并定位 YYYY 年的放假通知。返回 {url,title,pubtime,wenhao} 或 None。

    为什么用检索接口而不是拼 URL：正文页地址里那个 `content_7047091.htm`
    是 CMS 的自增 id，**无法推算**，每年都不一样。
    """
    import requests
    try:
        r = requests.get(
            SEARCH_URL,
            params={"t": "zhengcelibrary", "q": f"{year}年{TITLE_HINT}",
                    "p": 1, "n": 10, "sort": "score", "sortType": 1,
                    "searchfield": "title"},
            headers={"User-Agent": np.DEFAULT_UA}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        raise np.NoticeError(f"检索公告失败 {type(exc).__name__}: {str(exc)[:140]}") from exc

    items: list[dict] = []
    cat = ((data.get("searchVO") or {}).get("catMap") or {})
    for group in cat.values():
        if isinstance(group, dict):
            items.extend(group.get("listVO") or [])

    def clean(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s or "").strip()

    best = None
    for it in items:
        title = clean(it.get("title"))
        url = it.get("url") or ""
        if TITLE_HINT not in title or str(year) not in title or "gov.cn" not in url:
            continue
        if "通知" not in title:
            continue
        # 正文优先 zhengceku（页面干净），其次 gongbao
        rank = 0 if "zhengceku" in url else (1 if "gongbao" in url else 2)
        cand = {"url": url, "title": title, "pubtime": it.get("pubtimeStr") or "",
                "wenhao": it.get("wenhao") or "", "rank": rank}
        if best is None or rank < best["rank"]:
            best = cand
    return best


# ==========================================================================
# ② 抽取 + 自校验（不盲信 LLM）
# ==========================================================================

_LLM_SYSTEM = (
    "你是从中国国务院办公厅『部分节假日安排』公告里抽取放假与调休安排的专用解析器。"
    "公告形如：『七、国庆节：10月1日（周四）至7日（周三）放假调休，共7天。"
    "9月20日（周日）、10月10日（周六）上班。』"
    "规则：\n"
    "1) 只抽取正文里明确写到的日期，绝不推测。\n"
    "2) 区间里出现的农历日期（如『农历腊月二十八』）忽略，只用公历日期。\n"
    "3) 『X月X日上班』这类调休上班日要单独放进 workdays，它们多是周六或周日。\n"
    "4) 日期一律转成 YYYY-MM-DD。跨年的区间也要按真实年份展开。"
)

_SCHEMA = (
    '{"year": 2026,'
    ' "holidays": [{"name":"国庆节","start":"2026-10-01","end":"2026-10-07","days":7}],'
    ' "workdays": ["2026-09-20","2026-10-10"]}'
)

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _norm_date(v: Any, year: int) -> date | None:
    if isinstance(v, (date,)):
        return v
    m = _DATE_RE.search(str(v or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def validate(raw: dict, year: int) -> tuple[dict, list[str]]:
    """把 LLM 输出整形成可用日历，并记录所有可疑之处（不静默丢弃）。"""
    notes: list[str] = []
    holidays: list[dict] = []
    workdays: list[date] = []

    for h in (raw.get("holidays") or []):
        s = _norm_date(h.get("start"), year)
        e = _norm_date(h.get("end"), year) or s
        if s is None:
            notes.append(f"丢弃无法解析的假期条目：{h!r}"[:160])
            continue
        if e < s:
            s, e = e, s
            notes.append(f"{h.get('name')} 起止日期颠倒，已交换")
        span = (e - s).days + 1
        claimed = h.get("days")
        if isinstance(claimed, int) and claimed != span:
            notes.append(f"{h.get('name')} 公告写「共{claimed}天」但区间是 {span} 天，按区间采用")
        holidays.append({"name": str(h.get("name") or "假期").strip(),
                         "start": s.isoformat(), "end": e.isoformat(), "days": span})
        if s.year != year or e.year != year:
            notes.append(f"{h.get('name')} 区间跨年（{s}~{e}），注意年份")

    # 区间重叠：合并（真实公告不重叠，重叠=抽错）
    holidays.sort(key=lambda x: x["start"])
    merged: list[dict] = []
    for h in holidays:
        if merged and h["start"] <= merged[-1]["end"]:
            prev = merged[-1]
            if h["end"] > prev["end"]:
                prev["end"] = h["end"]
                prev["days"] = (date.fromisoformat(prev["end"])
                                - date.fromisoformat(prev["start"])).days + 1
            notes.append(f"假期区间与「{prev['name']}」重叠，已合并")
            continue
        merged.append(h)

    for w in (raw.get("workdays") or []):
        d = _norm_date(w, year)
        if d is None:
            notes.append(f"丢弃无法解析的调休上班日：{w!r}"[:120])
            continue
        if d.weekday() < 5:
            notes.append(f"调休上班日 {d} 是工作日，公告口径异常，已忽略")
            continue
        workdays.append(d)

    holiday_dates: set[date] = set()
    for h in merged:
        d = date.fromisoformat(h["start"])
        end = date.fromisoformat(h["end"])
        while d <= end:
            holiday_dates.add(d)
            d += timedelta(days=1)
    clash = sorted(set(workdays) & holiday_dates)
    if clash:
        notes.append(f"调休上班日与假期重叠：{[str(x) for x in clash]}，已移除")
        workdays = [d for d in workdays if d not in holiday_dates]

    return ({"year": year,
             "holidays": merged,
             "workdays": sorted({d.isoformat() for d in workdays}),
             "validated": True, "notes": notes},
            notes)


def _fallback_regex(text: str, year: int) -> dict:
    """LLM 不可用时的确定性兜底：只认「数字、逗号、放假」这种最稳的句式。

    刻意保守 —— 宁可少抽也不要抽错：调休上班日的句式（"X月X日（周X）上班"）
    也一并认，认不出就交给 crosscheck 去暴露差异。
    """
    out = {"year": year, "holidays": [], "workdays": []}
    cn = "一二三四五六七八九十"
    for line in text.split("\n"):
        if "放假" not in line:
            continue
        m = re.match(rf"[{cn}]+、\s*([^：:]{{1,6}})[：:]\s*"
                     r"(\d{{1,2}})月(\d{{1,2}})日.*?至(\d{{1,2}})日.*?放假", line)
        if m:
            name, m1, d1, m2, d2 = m.groups()
            try:
                s = date(year, int(m1), int(d1))
                e = date(year, int(m2), int(d2))
            except ValueError:
                continue
            if e < s:
                s, e = e, s
            out["holidays"].append({"name": name.strip(), "start": s.isoformat(),
                                    "end": e.isoformat(),
                                    "days": (e - s).days + 1})
        for wm in re.finditer(r"(\d{1,2})月(\d{1,2})日（周[一二三四五六日]）上班", line):
            try:
                out["workdays"].append(date(year, int(wm.group(1)),
                                            int(wm.group(2))).isoformat())
            except ValueError:
                pass
    return out


# ==========================================================================
# ③ 构建 / 读取某年日历
# ==========================================================================

def build(year: int, *, llm_conf: dict | None = None, force: bool = False) -> dict:
    """抓公告 → LLM 抽取 → 校验 → 落缓存。返回该年日历（含 provenance）。"""
    with _lock:
        _load_cache()
    if not force and str(year) in _cache:
        return _cache[str(year)]

    notice = find_notice(year)
    if not notice:
        raise np.NoticeError(
            f"没找到 {year} 年的放假通知（正常情况：该公告每年 11 月上旬才发布）")

    def fb(text: str) -> dict:
        return _fallback_regex(text, year)

    res = np.parse_notice(
        notice["url"],
        system_prompt=_LLM_SYSTEM,
        schema_hint=_SCHEMA,
        fallback=fb,
        must_contain=["放假", str(year)],
        llm_conf=llm_conf,
    )
    cal, notes = validate(res.data if isinstance(res.data, dict) else {}, year)
    cal.update({
        "source_url": notice["url"],
        "source_title": notice["title"],
        "doc_no": notice.get("wenhao") or "",
        "published": notice.get("pubtime") or "",
        "fetched_at": res.fetched_at,
        "used": res.used,
        "model": res.model,
        "parser_notes": res.notes + notes,
    })
    cal["crosscheck"] = crosscheck_akshare(year, cal)

    with _lock:
        _load_cache()
        _cache[str(year)] = cal
        _meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _meta["source_url"] = notice["url"]
        _meta["used"] = res.used
        _meta["model"] = res.model
        _save_cache()
    return cal


def calendar(year: int) -> dict | None:
    _load_cache()
    return _cache.get(str(year))


# ==========================================================================
# ④ 交易日判定
# ==========================================================================

def _in_holidays(d: date, cal: dict) -> str | None:
    for h in cal.get("holidays") or []:
        if date.fromisoformat(h["start"]) <= d <= date.fromisoformat(h["end"]):
            return h["name"]
    return None


def _is_trading_day_with(cal: dict, d: date) -> bool:
    """纯函数版判定：给定该年日历算某天是否交易日（不碰全局缓存）。

    对账时必须用它 —— 那时候年份日历还没落盘，`is_trading_day()` 会去回落
    akshare，一天调一次、一年调 365 次，慢且易失败（实测第二次调用失败 ->
    全年判成非交易日 -> 对账 mine=0）。

    ⚠️ 调休上班日**不算交易日**。国务院的调休是给上班族补班，股市周末休市
    是交易所自己的安排 —— 2026-01-04（周日）、02-14（周六）A 股都不开。
    这个假设最初写成「周末但开市」，被 crosscheck_akshare 抓了出来：
    mine=248 vs exchange=242，差异恰好是那 6 个调休上班日。交易所日历才是准的。
    调休日要用来判「工作日」请用 is_workday()。
    """
    if _in_holidays(d, cal):
        return False
    return d.weekday() < 5


def is_workday(d: date | None = None) -> bool | None:
    """True=法定工作日 / False=法定休息日 / None=日历不可用。

    与 `is_trading_day` 的区别就在调休上班日：那天上班（is_workday=True）
    但股市不开（is_trading_day=False）。日报该不该生成、提醒该不该推用这个。
    """
    d = d or date.today()
    cal = calendar(d.year)
    if not cal:
        return None
    if d.isoformat() in set(cal.get("workdays") or []):
        return True
    return _in_holidays(d, cal) is None and d.weekday() < 5


def is_trading(d: date | None = None) -> bool:
    """三态折叠成布尔：日历不可用(None)时退回「周一~周五」。

    守护线程用这个而不是 is_trading_day：日历源挂掉时，宁可按周一~周五跑
    （可能用到陈旧数据，但状态字段仍会标出来），也不要**整天静默跳过** ——
    后者会让止盈/提款/报告在真实交易日上不执行，且没有任何报错。
    需要严格区分 None 的地方（入库日期判定）请直接用 is_trading_day()。
    """
    d = d or date.today()
    r = is_trading_day(d)
    return (d.weekday() < 5) if r is None else bool(r)


def is_work(d: date | None = None) -> bool:
    """is_workday 的布尔折叠版（None 时退回「周一~周五」）。"""
    d = d or date.today()
    r = is_workday(d)
    if r is None:
        return d.weekday() < 5
    return bool(r)


def holiday_name(d: date) -> str | None:
    cal = calendar(d.year)
    if not cal:
        return None
    return _in_holidays(d, cal)


def _exchange_trading_days() -> set[date] | None:
    """akshare 的交易日历（进程内缓存：一次拉，全天复用）。拉不到返回 None。"""
    global _ex_days
    if _ex_days is not None:
        return _ex_days or None
    try:
        import akshare as ak
        import pandas as pd
        df = ak.tool_trade_date_hist_sina()
        _ex_days = {pd.to_datetime(x).date() for x in df["trade_date"]}
    except Exception:
        _ex_days = set()
    return _ex_days or None


def is_trading_day(d: date | None = None) -> bool | None:
    """True=交易日 / False=休市 / **None=拿不到日历（日历可能挂了）**。

    ⚠️ 调用方必须区分 None 与 False：把 None 当休市会让整段采集被静默跳过
    （data_service/sources.py 的 is_trading_day 是同一个约定）。
    """
    d = d or date.today()
    cal = calendar(d.year)
    if not cal:
        # 没有公告日历 -> 试交易所日历 -> 都没有就返回 None（不当休市）
        days = _exchange_trading_days()
        return None if days is None else (d in days)
    return _is_trading_day_with(cal, d)

def _days_in_range(a: date, b: date, pred) -> list[date]:
    out, cur = [], a
    while cur <= b:
        if pred(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def next_trading_day(d: date | None = None, n: int = 1) -> date | None:
    d = d or date.today()
    step = 1 if n >= 0 else -1
    left = abs(n)
    cur = d
    for _ in range(400):
        if is_trading_day(cur) is True:
            left -= 1
            if left == 0:
                return cur
        cur += timedelta(days=step)
    return None


def trading_days_between(a: date, b: date) -> list[date]:
    return _days_in_range(a, b, lambda d: is_trading_day(d) is True)


# ==========================================================================
# ⑤ 交叉校验（交易所日历 vs 公告日历）
# ==========================================================================

def crosscheck_akshare(year: int, cal: dict | None = None) -> dict:
    """用 akshare 的交易日历逐日对账。差异只记录、不修改 —— 让不一致可见。"""
    cal = cal or calendar(year) or {}
    official = _exchange_trading_days()
    if not official:
        return {"ok": False, "why": "交易所交易日历不可用（akshare 拉取失败）"}

    mine = set(_days_in_range(date(year, 1, 1), date(year, 12, 31),
                              lambda d: _is_trading_day_with(cal, d)))
    theirs = {d for d in official if d.year == year}
    extra = sorted(mine - theirs)          # 我方说交易、交易所说休市
    missing = sorted(theirs - mine)        # 我方说休市、交易所说交易
    return {
        "ok": True,
        "mine": len(mine), "exchange": len(theirs),
        "extra_as_trading": [str(d) for d in extra[:40]],
        "missing_as_trading": [str(d) for d in missing[:40]],
        "agree": not extra and not missing,
    }


# ==========================================================================
# ⑥ 状态 / CLI
# ==========================================================================

def status() -> dict:
    _load_cache()
    years = sorted(_cache)
    cur = date.today().year
    covered = cur in [int(y) for y in years]
    return {
        "years": years,
        "current_year": cur,
        "current_year_covered": covered,
        "updated_at": _meta.get("updated_at", ""),
        "source_url": _meta.get("source_url", ""),
        "used": _meta.get("used", ""),
        "model": _meta.get("model", ""),
        "today": date.today().isoformat(),
        "today_is_trading_day": is_trading_day(),
        "today_holiday": holiday_name(date.today()),
        "next_trading_day": (next_trading_day().isoformat()
                             if next_trading_day() else None),
        "detail": {y: {"holidays": len(c.get("holidays") or []),
                       "workdays": len(c.get("workdays") or []),
                       "used": c.get("used"), "fetched_at": c.get("fetched_at"),
                       "source_url": c.get("source_url"),
                       "crosscheck": c.get("crosscheck")}
                   for y, c in _cache.items()},
    }


def check_holiday_alert(notify_fn=None) -> list[dict]:
    """长假/调休提醒：两个时点各推一次（同键只推一次）。

    - **长假前最后一个交易日** 推「明日休市 N 天」：这是最有用的时点 ——
      尾盘是调仓窗口，且提醒里带上休市天数与下一个交易日。
    - **调休上班的周末** 推「明天要上班（股市不开）」：避免真的跑去交易大厅，
      也提醒当天别安排交易动作。

    冷却按 key 记账（每天的 key 不同，不会互相压制）。
    """
    today = date.today()
    state_file = CACHE_FILE.parent / "holiday_state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"sent": {}}
    sent = state.setdefault("sent", {})
    alerts: list[dict] = []

    # ① 今天之后紧接着就是长假，且今天是交易日 -> 明天起休市
    nxt = today + timedelta(days=1)
    name = _in_holidays(nxt, calendar(nxt.year) or {})
    if name and is_trading(today):
        # 算休市长度（从明天起连续假期天数）
        span, cur = 0, nxt
        cal = calendar(nxt.year) or {}
        while cur.year == nxt.year or True:
            nm = _in_holidays(cur, cal)
            if nm is None:
                break
            span += 1
            cur += timedelta(days=1)
            if span > 30:
                break
        back = next_trading_day(nxt)
        key = f"pre-holiday:{today.isoformat()}"
        if key not in sent and span >= 1:
            lines = [f"明日（{nxt.isoformat()}）起休市 {span} 天（{name}）",
                     f"下一个交易日：{back.isoformat() if back else '待定'}",
                     "休市期间行情接口返回的是最后一个交易日的数据，"
                     "据此下单会用到过期价。"]
            if is_workday(back):
                lines.append(f"注意 {back.isoformat()} 是调休上班日，但股市不开。")
            alerts.append({"key": key, "title": f"🌴 {name}休市提醒",
                           "content": "\n".join(lines), "span": span})
            sent[key] = today.isoformat()

    # ② 今天是调休上班的周末 -> 明天上班但股市不开
    if (is_workday(today) and not is_trading_day(today)
            and today.weekday() >= 5):
        key = f"makeup-workday:{today.isoformat()}"
        if key not in sent:
            alerts.append({
                "key": key, "title": "🧰 调休上班日提醒",
                "content": f"今天是调休上班（{'六' if today.weekday() == 5 else '日'}）"
                           f"，但**股市不开**。\n不要按工作日安排交易动作；"
                           f"下一个交易日 {next_trading_day() or '待定'}。",
                "span": 0})
            sent[key] = today.isoformat()

    if alerts:
        # 只留 120 天内的键，防文件膨胀
        cutoff = (today - timedelta(days=120)).isoformat()
        state["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if notify_fn:
            for a in alerts:
                try:
                    notify_fn(a["title"], a["content"], event="market_holiday")
                except Exception as exc:
                    print(f"[holiday] 通知失败: {exc}", flush=True)
    return alerts


def refresh(years: list[int] | None = None, *, llm_conf: dict | None = None) -> dict:
    """确保覆盖指定年份（默认今年+明年）。单年失败不影响其他年。"""
    years = years or [date.today().year, date.today().year + 1]
    ok, failed = {}, {}
    for y in years:
        try:
            c = build(y, llm_conf=llm_conf, force=True)
            ok[y] = {"holidays": len(c["holidays"]), "workdays": len(c["workdays"]),
                     "used": c["used"], "crosscheck_agree":
                         (c.get("crosscheck") or {}).get("agree")}
        except Exception as exc:
            failed[y] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return {"ok": ok, "failed": failed}


if __name__ == "__main__":               # python holiday_calendar.py [年份...]
    yrs = [int(a) for a in os.sys.argv[1:]] or None
    print(json.dumps(refresh(yrs), ensure_ascii=False, indent=1))
    print(json.dumps(status(), ensure_ascii=False, indent=1))
