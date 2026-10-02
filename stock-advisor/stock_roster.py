# -*- coding: utf-8 -*-
"""全市场股票名册 —— 挖新股的���基。

为什么必须有它（2026-09-29）
---------------------------
系统的新闻抓取是**按自选股关键词**驱动的，所以 `sa_news` 里出现过的代码 100% 都在
自选股里（实测 10066 行、0 个例外）。也就是说：**靠现有新闻永远发现不了新股票**。
要挖「你还没关注的票」，得先有一份「公司全称 -> 代码」的字典，才能把社媒/新闻正文里
提到的公司名解析成代码。

数据源（实测）
--------------
东方财富**数据中心**报表 `RPTA_APP_IPOAPPLY`：
    https://datacenter-web.eastmoney.com/api/data/v1/get
    count=5638，pageSize=500 -> 12 个请求、15 秒拿到全市场
    字段含 SECURITY_CODE / SECURITY_NAME_ABBR / LISTING_DATE / INDUSTRY_NAME

为什么不用更常见的 push2 clist（同样能列全市场）
------------------------------------------------
push2 有 IP 风控，我实测连续请求后被**所有 6 个镜像主机**一起封掉（17/29/79/82/7/push2），
十几分钟不恢复。而 datacenter-web 是另一套限频，push2 挂的时候它照常工作。
名册一天只需要刷新一次，没必要去碰那个会被封的接口。

为什么「按全称匹配文本」不会有假阳性
----------------------------------
之前社媒概念层吃过亏：手写静态概念表，「创新药」里的「创新」会匹配到蓝色光标。
这里不一样 —— 用的是**交易所登记的公司全称**，不是关键词。全市场 5638 只里
只有 1 个两字名（柳工），剩下的都是 3~8 字且高度特异，匹配歧义天然极低。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import holiday_calendar

from psycopg2.extras import execute_values

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT = "RPTA_APP_IPOAPPLY"
COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,SECURITY_NAME_FULL,LISTING_DATE,"
           "INDUSTRY_NAME,TRADE_MARKET,SECUCODE")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PAGE_SIZE = 500          # 实测 1000 也行，但 500 更稳、失败重试代价更小
MAX_PAGES = 30           # 上限保护：正常 12 页
SLEEP = 0.4              # 礼貌限速
RETRY = 2
TIMEOUT = 20             # 单次请求超时
DEADLINE = 150           # 整轮硬上限。
# 为什么要全局截止时间：网页按钮是同步等待的。原来只有「每页重试 3 次 × 30s 超时」，
# 一旦源开始超时，12 页最坏能拖 18 分钟 —— 用户看到的就是「点了没反应」。
# 有截止时间后，超时就带着已抓到的部分正常返回（本轮已按 LISTING_DATE 倒序抓，
# 失败时至少保住最近上市的那批，也就是挖新股最需要的部分）。


def _get(params: dict, timeout: int = TIMEOUT) -> dict:
    url = DC_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_roster(max_pages: int = MAX_PAGES,
                 deadline: int = DEADLINE) -> tuple[dict, dict]:
    """拉全市场名册。返回 (roster, meta)。

    roster: {code: {code,name,full,list_date,industry,market}}
    按 LISTING_DATE 倒序翻页，这样「最新上市」天然排在前面，
    拉取中途失败时至少能拿到最近上市的那批（挖新股最需要的部分）。
    """
    roster: dict[str, dict] = {}
    pages_done = 0
    pages_total = None
    count = None
    err = ""
    stopped = ""
    t0 = time.time()
    pn = 1
    while pn <= max_pages:
        if time.time() - t0 > deadline:
            stopped = "超过整轮截止 %ds，已抓 %d 页 / %d 只" % (
                deadline, pages_done, len(roster))
            break
        params = {"reportName": REPORT, "columns": COLUMNS, "pageNumber": pn,
                  "pageSize": PAGE_SIZE, "sortColumns": "LISTING_DATE",
                  "sortTypes": "-1", "source": "WEB", "client": "WEB"}
        data = None
        for attempt in range(RETRY):
            try:
                data = _get(params)
                break
            except Exception as exc:      # noqa: BLE001
                err = str(exc)[:150]
                if time.time() - t0 > deadline:
                    break
                time.sleep(1.0 * (attempt + 1))
        if data is None:
            break
        result = data.get("result") or {}
        rows = result.get("data") or []
        if pages_total is None:
            pages_total = result.get("pages")
            count = result.get("count")
        if not rows:
            break
        for x in rows:
            code = str(x.get("SECURITY_CODE") or "")
            # 只要 6 位数字的 A 股/北交所代码；排除 B 股、指数等杂项
            if len(code) != 6 or not code.isdigit():
                continue
            ld = x.get("LISTING_DATE")
            roster[code] = {
                "code": code,
                "name": (x.get("SECURITY_NAME_ABBR") or "").strip(),
                "full": (x.get("SECURITY_NAME_FULL") or "").strip(),
                "list_date": str(ld)[:10] if ld else None,
                "industry": (x.get("INDUSTRY_NAME") or "").strip(),
                "market": (x.get("TRADE_MARKET") or "").strip(),
            }
        pages_done = pn
        if pages_total and pn >= pages_total:
            break
        pn += 1
        time.sleep(SLEEP)

    meta = {"pages": pages_done, "pages_total": pages_total,
            "declared_count": count, "got": len(roster),
            "seconds": round(time.time() - t0, 1), "error": err,
            "stopped": stopped,
            "complete": bool(pages_total) and pages_done >= pages_total}
    return roster, meta


def roster_stale_hours(cur) -> float:
    """名册上次刷新的小时数。没有记录返回 1e9（视为极旧）。"""
    cur.execute("SELECT max(fetched_at) FROM sa_stock_roster_meta")
    row = cur.fetchone()
    if not row or not row[0]:
        return 1e9
    return (datetime.now() - row[0].replace(tzinfo=None)).total_seconds() / 3600.0


def refresh_roster(deps: dict, force: bool = False) -> dict:
    """把名册写进库。返回 {ok, got, meta}。

    只在「距上次刷新超过 threshold_hours」时才真拉，避免每次挖股都打 12 个请求。
    """
    conf = deps.get("conf") or {}
    get_conn = deps["get_conn"]
    hours = float(conf.get("roster_refresh_hours", 20))

    with get_conn() as conn, conn.cursor() as cur:
        if not force:
            age = roster_stale_hours(cur)
            if age < hours:
                cur.execute("SELECT count(*) FROM sa_stock_roster")
                n = cur.fetchone()[0]
                return {"ok": True, "skipped": True, "got": n,
                        "age_hours": round(age, 1),
                        "why": "距上次刷新 %.1f 小时 < 阈值 %.0f 小时" % (age, hours)}

    roster, meta = fetch_roster()
    if not roster:
        return {"ok": False, "skipped": False, "got": 0, "meta": meta}
    if meta.get("stopped"):
        # 部分成功也算成功：宁可名册少几百只（旧数据仍在库里），也不要整轮作废
        print("[discover] 名册部分刷新：%s" % meta["stopped"], flush=True)

    now = datetime.now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 必须用 execute_values 而不是 executemany。
            # psycopg2 的 executemany 是**每行一次网络往返**，而这台是远程共享云库
            # （同实例还跑着 dify），实测单语句往返约 65ms：
            # executemany 写 1200 行 = 81.7 秒，写全量 5638 行就是 6 分钟以上 ——
            # 网页上表现为「点了刷新没反应」。execute_values 把整批拼成一条
            # 多行 INSERT，1~6 次往返搞定（sector.py 早就是这个写法）。
            rows = [(r["code"], r["name"], r["full"], r["list_date"], r["industry"],
                     r["market"], now, now) for r in roster.values()]
            execute_values(cur, """INSERT INTO sa_stock_roster
                   (code, name, full_name, list_date, industry, market,
                    first_seen, last_seen)
                   VALUES %s
                   ON CONFLICT (code) DO UPDATE SET
                     name = EXCLUDED.name,
                     full_name = EXCLUDED.full_name,
                     list_date = EXCLUDED.list_date,
                     industry = EXCLUDED.industry,
                     market = EXCLUDED.market,
                     last_seen = EXCLUDED.last_seen""", rows, page_size=1000)
            # 本次没出现的（退市/停牌）保留 last_seen 但标记 stale，别删——
            # 删了会让「这只票昨天还在名册里」这个判断失效。
            cur.execute("UPDATE sa_stock_roster SET stale = TRUE "
                        "WHERE last_seen < %s", (now,))
            cur.execute("UPDATE sa_stock_roster SET stale = FALSE "
                        "WHERE last_seen >= %s", (now,))
            cur.execute(
                """INSERT INTO sa_stock_roster_meta
                   (id, fetched_at, got, pages, declared_count, meta)
                   VALUES (1,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                     fetched_at = EXCLUDED.fetched_at, got = EXCLUDED.got,
                     pages = EXCLUDED.pages,
                     declared_count = EXCLUDED.declared_count, meta = EXCLUDED.meta""",
                (now, len(roster), meta["pages"], meta["declared_count"],
                 json.dumps(meta, ensure_ascii=False)))
        conn.commit()
    return {"ok": True, "skipped": False, "got": len(roster), "meta": meta}


def load_name_index(cur, min_len: int = 3) -> dict:
    """从库里读 name -> code 索引（挖新股用；不联网）。

    只收长度 >= min_len 的简称与全称。全市场只有 1 个两字名，留着只会制造歧义。
    """
    cur.execute("SELECT code, name, full_name FROM sa_stock_roster "
                "WHERE stale = FALSE AND name <> ''")
    idx: dict[str, str] = {}
    for code, name, full in cur.fetchall():
        for n in (name, full):
            n = (n or "").strip()
            if len(n) >= min_len and n not in idx:
                idx[n] = code
    return idx


def listed_days(cur, code: str, today: str | None = None) -> int | None:
    """上市至今的交易日数（自然日粗算，周末不计）。None = 名册里没这只。"""
    cur.execute("SELECT list_date FROM sa_stock_roster WHERE code=%s", (code,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        d0 = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    end = (datetime.strptime(today, "%Y-%m-%d").date() if today
           else datetime.now().date())
    if end < d0:
        return 0
    days, d = 0, d0
    while d < end:
        d = d.fromordinal(d.toordinal() + 1)
        if holiday_calendar.is_trading(d):
            days += 1
    return days
