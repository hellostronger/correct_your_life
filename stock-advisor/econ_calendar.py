"""财经日历：固定规则宏观/交割事件自动生成 + 手动事件 + 盘前微信提醒。

为什么用规则生成而不是接口：东财/金十的经济日历接口未验证且随时可能变，
而这类事件里最影响盘面的几个本身就是日历规则，可以纯本地算——

| 事件 | 规则 | 影响 |
|---|---|---|
| 美国非农就业 | 每月第一个周五（北京 20:30 夏/21:30 冬） | 当晚美股剧烈波动，隔天 A/港情绪 |
| 四巫日 Quad Witching | 季月（3/6/9/12）第三个周五 | 美股期货期权到期 + 富时罗素/标普季调生效，被动资金调仓砸盘/拉尾盘高发 |
| 股指期货交割日 | 每月第三个周五（非季月单列） | A股期现收敛，尾盘易波动 |
| ETF期权到期 | 每月第四个周三 | 标的（50/300/500ETF 等）临近行权价磁吸/甩离 |
| LPR 报价 | 每月 20 日（遇周末顺延下一工作日）9:15 | 利率敏感板块（银行/地产）异动 |

FOMC 议息这类「不精确到规则」的日期没有可靠推算方式，
走 sa_calendar_events 手动/脚本添加（UI 上有入口，也可让 Claude 定时查好写库），
同样参与每日提醒。

中/美 CPI 的公布时间**不用手动**：从百度经济日历自动同步（见「CPI 排期自动同步」一节，
2026-10-17 加）。它带确切日期时间和未来排期（实测 76 天逐日扫描 0 失败），
缓存到 data/econ_sync.json，只读展示、**不进数据库**。

提醒策略（_calendar_loop，交易日早 8:47 盘前）：
- 今天/明天发生的事件合并一条推微信
- level=high（非农/四巫）额外提前 3 天预告一次
- 提醒键记在 data/calendar_state.json，同键只推一次
"""

import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import holiday_calendar

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "calendar_state.json"

EVENT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS sa_calendar_events (
    id          BIGSERIAL PRIMARY KEY,
    event_date  DATE NOT NULL,
    time_hint   VARCHAR(16) NOT NULL DEFAULT '',
    title       VARCHAR(128) NOT NULL,
    note        VARCHAR(255) NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_calendar_date ON sa_calendar_events (event_date);
"""


def ensure_tables(conn):
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in EVENT_TABLE_DDL.split(";") if s.strip()]:
            cur.execute(stmt)


# ---------------- 日期规则工具 ----------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """当月第 n 个星期 weekday（0=周一 … 4=周五 5=周六 6=周日）。"""
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7, weeks=n - 1)
    return d


def _next_weekday(d: date) -> date:
    """顺延到下一个**工作日**（含调休上班的周六/周日）。

    原来只判 `weekday() >= 5`：像 LPR「每月 20 日遇周末顺延」这类规则，
    遇到调休上班的周末会顺延错一天 —— 2026-10-10(周六) 就是国庆的调休上班日。
    日历不可用时退回「周一~周五」，不改变原行为。
    """
    for _ in range(30):                      # 最多顺延一个月，防御性上限
        if holiday_calendar.is_work(d):
            return d
        d += timedelta(days=1)
    return d


def _us_midterm(year: int) -> date:
    """美国中期选举日：**偶数年 11 月第一个周一之后的周二**（联邦法律固定规则）。

    2026 年 = 11 月 3 日（435 个众院席位 + 35 个参院席位全部改选，
    结果决定第 120 届国会格局，2027-01 履职）。不是「11 月第一个周二」——
    那在 2016 年就出过错（11/1 撞万圣节投票日，实际推迟到 11/8）。
    """
    if year % 2:
        return None
    return _nth_weekday(year, 11, 0, 1) + timedelta(days=1)


# ---------------- 两会开幕日：从《政府工作报告》正文抽取 ----------------

_LH_STATE = BASE_DIR / "data" / "lianghui.json"
_LH_SYSTEM = (
    "你从中国政府网的《政府工作报告》全文里抽取全国人民代表大会会议的开幕日期。"
    "报告开头一句形如「政府工作报告 —— 2026年3月5日在第十四届全国人民代表大会"
    "第四次会议上」。只认这句话里的日期，**不要**用报告里的其它日期"
    "（如公布 GDP 增速的日期、提及上年会议日期）。"
)
_LH_SCHEMA = '{"opening": "2026-03-05", "session": "第十四届全国人民代表大会第四次会议", "quote": "原文里那句"}'


def lianghui_opening(year: int, *, llm_conf: dict | None = None,
                     force: bool = False) -> dict:
    """全国两会开幕日。优先从《政府工作报告》正文 LLM 抽取，取不到用惯例估算。

    为什么这么绕：全国人大会议的**闭幕日不固定**（会期 10~15 天，按议程走），
    而开幕日虽惯例是 3 月 5 日但偶尔顺延（2020 年因疫情延到 5 月 22 日）。
    《政府工作报告》第一句是国家公布的权威记载，抽它比拍脑袋准。

    返回 {date, level: 'exact'|'estimated', source_url, quote}。level 一定带上，
    页面上要能看出「这天是抽出来的」还是「按惯例估的」。
    """
    try:
        cached = json.loads(_LH_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    if not force and str(year) in cached:
        return cached[str(year)]

    out = {"date": None, "level": "estimated", "source_url": "", "quote": "",
           "note": ""}
    try:
        import notice_parser as np
        import requests
        r = requests.get("https://sousuo.www.gov.cn/search-gov/data",
                         params={"t": "zhengcelibrary", "q": f"{year}年政府工作报告",
                                 "p": 1, "n": 10, "sort": "score", "sortType": 1,
                                 "searchfield": "title"},
                         headers={"User-Agent": np.DEFAULT_UA}, timeout=20)
        items = []
        for g in ((r.json().get("searchVO") or {}).get("catMap") or {}).values():
            if isinstance(g, dict):
                items.extend(g.get("listVO") or [])
        import re
        url = ""
        for it in items:
            title = re.sub(r"<[^>]+>", "", it.get("title") or "")
            if str(year) in title and "政府工作报告" in title and it.get("url"):
                url = it["url"]
                break
        if url:
            res = np.parse_notice(
                url, system_prompt=_LH_SYSTEM, schema_hint=_LH_SCHEMA,
                llm_conf=llm_conf,
                must_contain=["全国人民代表大会", str(year)])
            d = res.data if isinstance(res.data, dict) else {}
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(d.get("opening") or ""))
            if m:
                out = {"date": m.group(0), "level": "exact",
                       "source_url": url, "quote": str(d.get("quote") or "")[:200],
                       "note": str(d.get("session") or "")}
            else:
                out["note"] = "抽到页面但没解析出日期"
    except Exception as exc:
        out["note"] = f"抽取失败（{type(exc).__name__}: {str(exc)[:120]}）"

    if not out["date"]:                      # 惯例估算：3 月 5 日，遇非工作日顺延
        est = _next_weekday(date(year, 3, 5))
        suffix = "顺延至最近工作日" if est != date(year, 3, 5) else ""
        out["date"] = est.isoformat()
        out["level"] = "estimated"
        out["note"] = ((out["note"] + "；") if out["note"] else "") + \
            f"按惯例估算（3月5日{suffix}）"
    _LH_STATE.parent.mkdir(exist_ok=True)
    _LH_STATE.write_text(json.dumps({**cached, str(year): out}, ensure_ascii=False),
                         encoding="utf-8")
    return out


# ---------------- 自动生成事件 ----------------

AUTO_EVENT_TYPES = {
    "nfp":   {"title": "美国非农就业数据", "market": "us", "level": "high"},
    "quad":  {"title": "四巫日：美股期货期权到期 + 富时罗素/标普季调生效", "market": "us", "level": "high"},
    "future": {"title": "股指期货交割日（A股期现收敛，尾盘易波动）", "market": "cn", "level": "mid"},
    "option": {"title": "ETF期权到期日（第四个周五，行权价磁吸效应）", "market": "cn", "level": "low"},
    "lpr":   {"title": "LPR贷款市场报价利率公布", "market": "cn", "level": "mid"},
    # 政治事件（2026-10-01 加）。这两类的影响不在于数据本身，而在于政策/关税/
    # 财政路径的重新定价窗口，所以都按 high 级提前 3 天预告。
    "midterm": {"title": "美国中期选举日（众院435席+参院35席全改选）", "market": "us", "level": "high"},
    "lianghui": {"title": "全国两会开幕（政府工作报告/预算/赤字率等政策定调）", "market": "cn", "level": "high"},
}


def generate_events(from_date: date | None = None, months: int = 3) -> list[dict]:
    """生成 from_date 起 months 个月内的规则事件（按日期升序）。"""
    today = from_date or date.today()
    y, m = today.year, today.month
    events: list[dict] = []

    def add(d: date, code: str, time_hint: str, note: str = ""):
        meta = AUTO_EVENT_TYPES[code]
        events.append({"date": d.isoformat(), "code": code,
                       "title": meta["title"], "time": time_hint,
                       "market": meta["market"], "level": meta["level"],
                       "note": note, "source": "auto"})

    for _ in range(months + 1):
        # 非农：第一个周五（数据北京时间周六凌晨发布，周五晚行情先抢跑）
        nfp = _nth_weekday(y, m, 4, 1)
        add(nfp, "nfp", "北京周六凌晨公布",
            "当晚美股剧烈波动，关注对下周一 A/港情绪传导")
        # 交割：每月第三个周五；季月升级为四巫日
        third_fri = _nth_weekday(y, m, 4, 3)
        if m in (3, 6, 9, 12):
            add(third_fri, "quad", "全天（美收盘为调仓生效点）",
                "被动基金集中换仓，尾盘波动放大；A股同期指期货交割")
        else:
            add(third_fri, "future", "尾盘")
        # ETF期权：第四个周五（2024-12 起由周三改到周五）
        add(_nth_weekday(y, m, 4, 4), "option", "收盘")
        # LPR：20 日遇周末顺延，9:15
        lpr = _next_weekday(date(y, m, 20))
        add(lpr, "lpr", "09:15", "1 年期/5 年期品种价，银行地产敏感")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    out = [e for e in events if e["date"] >= today.isoformat()]
    out.sort(key=lambda e: (e["date"], e["code"]))
    return out


def yearly_events(year: int, *, llm_conf: dict | None = None) -> list[dict]:
    """年度政治事件：中期选举（规则）+ 全国两会开幕（抽取，附 level 标注）。

    与月度规则事件分开，因为它们的生成方式不同：中期选举是纯规则（法律固定），
    两会是「抽取优先、惯例兜底」，后者的日期可信度要如实标出来。
    """
    events: list[dict] = []
    mid = _us_midterm(year)
    if mid:
        events.append({"date": mid.isoformat(), "code": "midterm",
                       "title": AUTO_EVENT_TYPES["midterm"]["title"],
                       "time": "北京当日 13:00 起陆续开票（美东 11 月第一个周二）",
                       "market": "us", "level": "high", "source": "auto",
                       "note": "结果决定第 120 届国会两院归属；选后美股/黄金/美债"
                               "波动放大，次日 A 股/港股情绪受传导"})
    lh = lianghui_opening(year, llm_conf=llm_conf)
    if lh.get("date"):
        note = lh.get("note") or ""
        if lh.get("level") != "exact":
            note = "⚠️ 开幕日为惯例估算（" + note + "），闭幕日不固定"
        events.append({"date": lh["date"], "code": "lianghui",
                       "title": AUTO_EVENT_TYPES["lianghui"]["title"],
                       "time": "09:00 开幕，闭幕日不固定（会期约 10-15 天）",
                       "market": "cn", "level": "high", "source": "auto",
                       "note": note,
                       "source_url": lh.get("source_url", "")})
    return events


# ---------------- 手动事件（FOMC/CPI 等非规则日期） ----------------

def load_manual(conn, from_iso: str) -> list[dict]:
    """库里的手动事件（from_iso 起）。"""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, event_date, time_hint, title, note "
                    "FROM sa_calendar_events WHERE event_date >= %s "
                    "ORDER BY event_date, id", (from_iso,))
        return [{"id": r["id"], "date": r["event_date"].isoformat(),
                 "code": "manual", "title": r["title"], "time": r["time_hint"],
                 "market": "cn", "level": "mid", "note": r["note"],
                 "source": "manual"} for r in cur.fetchall()]


# ---------------- CPI 排期自动同步（百度经济日历，2026-10-17 加） ----------------
# 用户要的：「财经日历里要有中/美 CPI 的公布时间」。
#
# 为什么是百度经济日历（实测依据，别换）：
# - 官方源走不通：BLS schedule 页从本机请求 403（Akamai 拦截），NBS 无稳定日程接口；
# - 金十 CDN（cdn-rili.jin10.com）本机 DNS 解析不了（getaddrinfo failed）；
# - 百度经济日历**带未来排期**（实测 76 天逐日扫描 0 失败），中/美 CPI 都带
#   确切北京时间（中国 09:30、美国 20:30/21:30 随夏令时变）；
# - akshare 已封装成 news_economic_baidu()，不需要手写私有接口。
#
# 缓存到 data/econ_sync.json（与 lianghui.json 同模式），**不进数据库**——
# 日历只是只读视图，没必要占一张表，也不引入 schema 变更。
#
# 诚实标注（页面也会看到同步时间）：
# - 百度只收录「已排期」的数据，远月（2 个月外）可能还没收录 → 每天滚动重扫，
#   出现即补上（所以看不到更远的 CPI 不一定是不存在，是源头还没排）。
# - 只取日期/时间/系列名，**不用**预期/前值数值列（临近发布才会填，且天天变）。

SYNC_FILE = BASE_DIR / "data" / "econ_sync.json"
SYNC_HORIZON_DAYS = 60            # 默认滚动扫描窗口（天）
SYNC_REQUEST_DELAY = 0.25         # 逐日请求间隔（秒），对源礼貌

_SYNC_LOCK = threading.Lock()     # 同进程内防并发同步（守护线程 + 手动接口）

# 只认「头条系列」，不抓 CPI 分项（住房/新车/能源/食品/二手车/克里夫兰等）和他国 CPI。
# 中国香港的「综合CPI年率」也刻意不收（不是内地口径；^中国 后面必须直接跟月份数字）。
_CN_CPI_RE = re.compile(r"^中国(\d+)月CPI(年率|月率)\(%\)$")
_US_CPI_RE = re.compile(r"^美国(\d+)月((?:核心)?CPI(?:年率-?未季调|月率-?季调后))\(%\)$")

_SYNC_META = {
    "cn": {"code": "cpi_cn", "market": "cn", "level": "mid",
           "title": "中国{mm}月CPI公布",
           "note": "国家统计局发布：{series}（同比/环比）"},
    "us": {"code": "cpi_us", "market": "us", "level": "high",
           "title": "美国{mm}月CPI公布",
           "note": "美国劳工统计局发布：{series}"},
}

# 百度日历的「地区」列 → 内部键（两者必须显式映射：早期版本的 bug 是直接
# 拿中文地区当 bucket 键，而 _SYNC_META 键是 cn/us，必然 KeyError）
_REGION_KEY = {"中国": "cn", "美国": "us"}


def _clean_cell(v) -> str:
    """把 pandas 的 NaN/None 等脏值统一成空串。"""
    s = str(v if v is not None else "").strip()
    return "" if s in ("nan", "NaT", "None") else s


def _fetch_day(day: date) -> list[dict]:
    """取某一天的经济日历行（akshare 封装百度）。[] = 当天无安排。"""
    import akshare as ak
    df = ak.news_economic_baidu(date=day.strftime("%Y%m%d"))
    if df is None or len(df) == 0:
        return []
    return [dict(r) for r in df.to_dict("records")]


def _cpi_events_for_day(day: date, rows: list[dict]) -> list[dict]:
    """从一天的行里汇总中/美 CPI 头条系列 → 事件（每国至多 1 条）。"""
    bucket: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = _REGION_KEY.get(_clean_cell(r.get("地区")))
        if not key:
            continue
        name = _clean_cell(r.get("事件"))
        m = (_CN_CPI_RE if key == "cn" else _US_CPI_RE).match(name)
        if not m:
            continue
        label = f"CPI{m.group(2)}" if key == "cn" else m.group(2)
        info = bucket.setdefault(key, {"month": m.group(1), "times": set(),
                                       "series": []})
        info["series"].append(label)
        t = _clean_cell(r.get("时间"))
        if t:
            info["times"].add(t)

    out = []
    for key, info in bucket.items():
        meta = _SYNC_META[key]
        series = list(dict.fromkeys(info["series"]))       # 去重保序
        times = sorted(info["times"])
        out.append({
            "date": day.isoformat(), "code": meta["code"],
            "title": meta["title"].format(mm=info["month"]),
            "time": times[0] if times else "",
            "market": meta["market"], "level": meta["level"],
            "note": meta["note"].format(series="、".join(series)),
            "source": "econ_sync",
        })
    return out


def _load_sync() -> dict:
    try:
        d = json.loads(SYNC_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sync(payload: dict) -> None:
    SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SYNC_FILE)


def load_synced_events(from_iso: str | None = None) -> list[dict]:
    """缓存里 from_iso（含）之后的事件（只读，不联网）。"""
    since = from_iso or date.today().isoformat()
    return [e for e in (_load_sync().get("events") or [])
            if isinstance(e, dict) and str(e.get("date") or "") >= since]


def sync_meta() -> dict:
    """同步状态（页面显示用）。"""
    d = _load_sync()
    window = d.get("window") or ["", ""]
    return {"last_synced_at": d.get("synced_at") or "",
            "event_count": len(d.get("events") or []),
            "window_end": window[1] if len(window) > 1 else "",
            "failed_days": len(d.get("failed_days") or []),
            "warn": d.get("warn") or "",
            "running": _SYNC_LOCK.locked()}


def sync_due(interval_hours: float) -> bool:
    """距上次成功同步是否已超过 interval_hours（没同步过 = 到期）。"""
    ts = _load_sync().get("synced_at")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return True
    return (datetime.now() - last).total_seconds() >= float(interval_hours) * 3600


def sync_cpi_events(*, days: int | None = None, delay: float | None = None,
                    fetch_fn=None, today: date | None = None) -> dict:
    """滚动扫描未来窗口，汇总中/美 CPI 排期写入缓存（拿不到锁就跳过）。

    三条防呆都写在这里（每条对应一种「静默把数据弄丢」的场景）：
    - 单日失败不中断整轮，**失败的日期沿用旧缓存**（网络抖一下不该抹掉已知排期）；
    - 全部日期都失败 → 不写缓存、synced_at 不更新，下一轮自动重试；
    - 扫到 0 条而旧缓存非空 → 保留旧数据并标 warn（源头口径变了也不能
      让 CPI 从日历里静默消失）。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "已有同步任务在跑"}
    try:
        return _sync_impl(days=days, delay=delay, fetch_fn=fetch_fn, today=today)
    finally:
        _SYNC_LOCK.release()


def _sync_impl(*, days: int | None = None, delay: float | None = None,
               fetch_fn=None, today: date | None = None) -> dict:
    today = today or date.today()
    days = int(days or SYNC_HORIZON_DAYS)
    delay = SYNC_REQUEST_DELAY if delay is None else delay
    fetch = fetch_fn or _fetch_day

    old = _load_sync()
    old_events = [e for e in (old.get("events") or []) if isinstance(e, dict)]
    events: list[dict] = []
    failed: list[str] = []
    requests = 0
    for i in range(days + 1):
        day = today + timedelta(days=i)
        requests += 1
        try:
            rows = fetch(day)
        except Exception as exc:
            failed.append(day.isoformat())
            events += [e for e in old_events if e.get("date") == day.isoformat()]
            print(f"[calendar] CPI 同步 {day} 失败: "
                  f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
        else:
            events += _cpi_events_for_day(day, rows or [])
        if delay:
            time.sleep(delay)

    if failed and len(failed) >= requests:
        return {"ok": False, "error": "全部日期抓取失败，缓存未更新",
                "failed_days": failed}

    warn = ""
    if not events and old_events:
        events = old_events
        warn = "本次未扫到任何 CPI（保留旧缓存）"

    dedup: dict[tuple, dict] = {}
    for e in events:
        dedup[(e.get("date"), e.get("code"))] = e
    events = [dedup[k] for k in sorted(dedup)]

    payload = {"synced_at": datetime.now().isoformat(timespec="seconds"),
               "window": [today.isoformat(), (today + timedelta(days=days)).isoformat()],
               "requests": requests, "failed_days": failed, "warn": warn,
               "events": events}
    _write_sync(payload)
    return {"ok": True, "events": len(events), "requests": requests,
            "failed_days": failed, "warn": warn}


def sync_async(**kw) -> dict:
    """手动触发：后台线程跑（别占住 HTTP 请求线程），立刻返回。

    锁在本函数先拿、由工作线程释放（threading.Lock 允许跨线程 release），
    这样「两个请求同时点」不会起两个同步；拿不到锁就是 busy。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"started": False, "busy": True}

    def _run():
        try:
            res = _sync_impl(**kw)
            print(f"[calendar] CPI 手动同步完成: {res}", flush=True)
        except Exception as exc:
            print(f"[calendar] CPI 手动同步失败: {type(exc).__name__}: {exc}",
                  flush=True)
        finally:
            _SYNC_LOCK.release()

    threading.Thread(target=_run, daemon=True, name="econ_sync_manual").start()
    return {"started": True}


# ---------------- 每日盘前提醒 ----------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    # 只保留最近 60 天的键，防文件无限膨胀
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if k >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def upcoming_events(conn, months: int = 3) -> list[dict]:
    """规则事件 + 年度政治事件 + 手动事件 + 新股排期 + CPI/财报同步 合并视图（按日期排序）。"""
    today = date.today()
    auto = generate_events(months=months)
    end = (today + timedelta(days=months * 31)).isoformat()
    for y in (today.year, today.year + 1):        # 跨年时把明年的也带上
        auto += yearly_events(y)
    manual = [e for e in load_manual(conn, today.isoformat()) if e["date"] <= end]
    ipo = _ipo_events(end)
    synced = [e for e in load_synced_events(today.isoformat()) if e["date"] <= end]
    earn = _earn_events(end)
    return sorted(auto + manual + ipo + synced + earn,
                  key=lambda e: (e["date"], e["source"], e["code"]))


def _ipo_events(end_iso: str) -> list[dict]:
    """新股排期并进日历视图。

    失败静默：IPO 排期是锦上添花，拉不到不该让整个财经日历 500。
    """
    try:
        import ipo_calendar
        return [e for e in ipo_calendar.calendar_events(days=120)
                if e["date"] <= end_iso]
    except Exception as exc:
        print(f"[calendar] 新股排期并入失败（忽略）: {exc}", flush=True)
        return []


def _earn_events(end_iso: str) -> list[dict]:
    """财报排期（持仓 + 所属板块龙头）并进日历视图。失败静默（同 _ipo_events）。"""
    try:
        import earnings_sync
        return [e for e in earnings_sync.load_events() if e["date"] <= end_iso]
    except Exception as exc:
        print(f"[calendar] 财报排期并入失败（忽略）: {exc}", flush=True)
        return []


def check_calendar_once(conn, notify_fn=None) -> list[dict]:
    """盘前提醒：今天/明天有事件合并推一条；high 级再提前 3 天预告一次。

    notify_fn(title, content) 注入（app 传 notifier.notify），None 时只返回
    将推内容（测试用）。返回本次触发的提醒列表；同键只推一次（state 文件记账）。
    """
    today = date.today()
    events = [e for e in upcoming_events(conn, months=1)
              if e["date"] <= (today + timedelta(days=31)).isoformat()]
    state = _load_state()
    sent = state["sent"]
    alerts: list[dict] = []

    def fmt(e: dict) -> str:
        mark = "⚡" if e["level"] == "high" else "•"
        t = f" {e['time']}" if e.get("time") else ""
        return f"{mark} {e['date']}{t} {e['title']}"

    # 今天/明天
    near = [e for e in events if e["date"] in (today.isoformat(),
                                               (today + timedelta(days=1)).isoformat())]
    key = f"near:{today.isoformat()}"
    if near and key not in sent:
        content = "今明两日财经日历：\n\n" + "\n".join(fmt(e) for e in near) + \
                  "\n\n（交割/调仓日尾盘易波动，注意仓位）"
        alerts.append({"key": key, "title": "📅 财经日历提醒", "content": content})
        sent[key] = today.isoformat()
    # high 级提前 3 天
    for e in events:
        if e["level"] != "high":
            continue
        d0 = date.fromisoformat(e["date"])
        if (d0 - today).days == 3:
            k3 = f"{e['date']}:pre3"
            if k3 not in sent:
                alerts.append({"key": k3, "title": "📅 重要事件预告",
                               "content": f"3 天后（{e['date']}）：{e['title']}"
                                          + (f"\n{e['note']}" if e.get("note") else "")})
                sent[k3] = today.isoformat()
    _save_state(state)
    if notify_fn:
        for a in alerts:
            try:
                notify_fn(a["title"], a["content"], event="calendar")
            except Exception as exc:
                print(f"[calendar] 通知失败: {exc}", flush=True)
    return alerts
