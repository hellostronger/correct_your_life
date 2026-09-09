"""板块轮动监控：东财免费接口采集行业/概念板块快照，自建历史算轮动信号。

核心思路（零成本、不依赖任何历史K线接口——push2his 等历史域名被掐）：
    每个交易日收盘后采集一次全量板块快照（行业 ~496 + 概念 ~504 + 地域 31）存
    sa_sector_snapshots；N 日动量/排名分从自己的历史库算。跑 3 个交易日后信号完整。

数据源（全部公开 JSON，无需 key，2026-09-09 逐一实测）：
    - push2delay.eastmoney.com/api/qt/clist/get  板块列表快照（302 跳到 delay 域，
      直接请求 delay 免一跳）。fs=m:90+t:2 行业 / t:3 概念 / t:1 地域。
      字段：f3 涨幅、f6 成交额、f8 换手、f12 代码、f14 名称、f62 主力净流入、
            f104/f105 涨/跌家数、f128/f140/f136 领涨股名称/代码/涨幅、f124 快照时间
    - push2ex.eastmoney.com/getTopicZTPool      涨停池（连板数 lbc、炸板 zbc、
      封单 fund、行业 hybk），日期传 YYYYMMDD，返回 qdate 自解释实际交易日
    - push2delay.eastmoney.com/api/qt/clist/get fs=b:BKxxxx  板块成分股
    - ulist.np/get secids=主要指数               指数涨幅 + 样本内涨跌家数（宽度参考）

轮动评分（score，用于排序找主线）：
    score = 涨幅分(0-40) + 资金分(0-30) + 涨停分(0-20) + 动量分(0-10)
    - 涨幅分：当日涨幅在全部板块中的百分位排名 × 40
    - 资金分：主力净流入在正/负两侧分位 × 30（负流入得 0 分区间）
    - 涨停分：板块内涨停家数（涨停池 hybk 聚合）× 4，封顶 20
    - 动量分：5 日累计涨幅为正再加分（历史不足 5 日时按已有天数算）
"""

import json
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 板块类别：东财 fs 参数 -> 存库 kind
BOARD_TYPES = [("2", "industry"), ("3", "concept"), ("1", "region")]
BOARD_KIND_NAME = {"industry": "行业", "concept": "概念", "region": "地域"}

# 快照字段（东财 fltt=2 时数值已转好）：
# f3 涨跌幅% f6 成交额(元) f8 换手% f12 代码 f14 名称 f62 主力净流入(元)
# f104 上涨家数 f105 下跌家数 f128 领涨股 f140 领涨股代码 f136 领涨股涨幅
# f124 最后更新 unix 秒
CLIST_FIELDS = "f3,f6,f8,f12,f14,f62,f104,f105,f128,f136,f140,f124"

PAGE_SIZE = 100          # clist 每页上限（pz=200 实测仍回 100）
FETCH_WORKERS = 4        # 快照分页并发
REQUEST_TIMEOUT = 20

# 涨停池 ut 是东财网页固定公开值
ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"

# 大盘指数观察池（ulist 一次拿齐：涨幅 + 样本内涨跌家数）
INDEX_POOL = [
    ("1.000001", "上证指数"), ("0.399001", "深证成指"), ("0.399006", "创业板指"),
    ("1.000688", "科创50"), ("1.000300", "沪深300"), ("0.899050", "北证50"),
]

_state = {"snapshooting": False, "last_run": None, "last_result": None}
_state_lock = threading.Lock()


def get_status() -> dict:
    with _state_lock:
        return dict(_state)


# ---------------- 拉取：板块快照 ----------------

def _fetch_board_pages(fs: str) -> list[dict]:
    """按类别拉全量板块（按 f12 代码排序翻页，翻页期间行序稳定不重不漏）。"""
    rows: list[dict] = []
    pn = 1
    while True:
        resp = requests.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params={"pn": pn, "pz": PAGE_SIZE, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f12", "fs": fs, "fields": CLIST_FIELDS},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = (resp.json().get("data") or {})
        diff = data.get("diff") or []
        rows.extend(diff)
        if pn * PAGE_SIZE >= (data.get("total") or 0) or not diff:
            break
        pn += 1
        time.sleep(0.15)
    return rows


def _row_to_snapshot(row: dict, kind: str) -> dict | None:
    code = row.get("f12")
    if not code:
        return None
    pct = row.get("f3")
    return {
        "code": code, "name": row.get("f14") or "", "kind": kind,
        "pct": pct if isinstance(pct, (int, float)) else None,
        "turnover": row.get("f6") if isinstance(row.get("f6"), (int, float)) else None,
        "turnover_rate": row.get("f8") if isinstance(row.get("f8"), (int, float)) else None,
        "main_inflow": row.get("f62") if isinstance(row.get("f62"), (int, float)) else None,
        "up_count": row.get("f104") if isinstance(row.get("f104"), (int, float)) else None,
        "down_count": row.get("f105") if isinstance(row.get("f105"), (int, float)) else None,
        "lead_stock": row.get("f128") or "",
        "lead_stock_code": row.get("f140") or "",
        "lead_stock_pct": row.get("f136") if isinstance(row.get("f136"), (int, float)) else None,
        "quote_ts": (datetime.fromtimestamp(row["f124"]).isoformat(timespec="seconds")
                     if isinstance(row.get("f124"), (int, float)) and row["f124"] > 1e9 else None),
    }


def fetch_all_boards() -> list[dict]:
    """全量板块快照（行业+概念+地域），并发分页。"""
    all_rows: list[dict] = []
    with __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor(
            FETCH_WORKERS) as ex:
        futs = [ex.submit(_fetch_board_pages, f"m:90 t:{t}") for t, _ in BOARD_TYPES]
        for (t, kind), fut in zip(BOARD_TYPES, futs):
            for row in fut.result():
                snap = _row_to_snapshot(row, kind)
                if snap:
                    all_rows.append(snap)
    return all_rows


# ---------------- 拉取：涨停池 / 指数 ----------------

def fetch_zt_pool(trade_date: str | None = None) -> dict:
    """涨停池。trade_date 'YYYYMMDD'；不传传今天（接口回最近交易日的数据，qdate 自解释）。

    返回 {"qdate": "2026-09-08", "total": 73, "items": [...], "by_board": {板块名: 数}}
    """
    d = (trade_date or date.today().strftime("%Y%m%d")).replace("-", "")
    try:
        resp = requests.get(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={"ut": ZT_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 600,
                    "sort": "fbt:asc", "date": d},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = (resp.json().get("data") or {})
    except Exception:
        return {"qdate": None, "total": 0, "items": [], "by_board": {}}
    pool = data.get("pool") or []
    items = [{
        "code": p.get("c"), "name": p.get("n"), "price": (p.get("p") or 0) / 1000,
        "pct": p.get("zdp"), "lbc": p.get("lbc") or 1,        # 连板数
        "zbc": p.get("zbc") or 0,                              # 炸板次数
        "fund": p.get("fund"),                                 # 封单额(元)
        "hybk": p.get("hybk") or "",                           # 行业板块名
        "days": (p.get("zttj") or {}).get("days"),             # 几天几板
        "ct": (p.get("zttj") or {}).get("ct"),
    } for p in pool]
    by_board = Counter(x["hybk"] for x in items if x["hybk"])
    lbc_board = Counter()
    for x in items:
        if x["hybk"]:
            lbc_board[x["hybk"]] += x["lbc"]  # 连板高度加权
    return {
        "qdate": (str(data.get("qdate")) if data.get("qdate") else None),
        "total": data.get("tc") or len(items),
        "items": items,
        "by_board": dict(by_board),
        "lb_by_board": dict(lbc_board),
        "max_lb": max((x["lbc"] for x in items), default=0),
        "sum_zbc": sum(x["zbc"] for x in items),
    }


def fetch_index_overview() -> list[dict]:
    """主要指数：涨幅 + 样本内涨跌家数（市场宽度参考口径）。"""
    try:
        resp = requests.get(
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
            params={"fltt": 2, "fields": "f2,f3,f12,f14,f104,f105,f106,f124",
                    "secids": ",".join(sec for sec, _ in INDEX_POOL)},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        diff = (resp.json().get("data") or {}).get("diff") or []
    except Exception:
        return []
    out = []
    for x in diff:
        out.append({
            "code": x.get("f12"), "name": x.get("f14"), "price": x.get("f2"),
            "pct": x.get("f3") if isinstance(x.get("f3"), (int, float)) else None,
            "up": x.get("f104"), "down": x.get("f105"), "flat": x.get("f106"),
        })
    # 按配置顺序排
    order = {sec.split(".")[1]: i for i, (sec, _) in enumerate(INDEX_POOL)}
    out.sort(key=lambda i: order.get(i["code"], 99))
    return out


def fetch_market_breadth() -> dict:
    """全市场宽度：涨/跌/平家数（A 股全部，56 页 × 100，并发 ~20s）。

    只在收盘后快照时调（每天一次），不在页面请求里实时调。
    """
    import concurrent.futures as cf

    total_expected = 0
    # 先拿 total
    try:
        r = requests.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params={"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f3", "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23", "fields": "f3"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        total_expected = (r.json().get("data") or {}).get("total") or 0
    except Exception:
        pass
    if not total_expected:
        return {}

    pages = min((total_expected + PAGE_SIZE - 1) // PAGE_SIZE, 80)

    def page(pn):
        resp = requests.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params={"pn": pn, "pz": PAGE_SIZE, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f3", "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23", "fields": "f3"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return [x.get("f3") for x in (resp.json().get("data") or {}).get("diff") or []]

    up = down = flat = 0
    with cf.ThreadPoolExecutor(FETCH_WORKERS) as ex:
        for arr in ex.map(page, range(1, pages + 1)):
            for v in arr:
                if isinstance(v, (int, float)):
                    up += v > 0
                    down += v < 0
                    flat += v == 0
    n = up + down + flat
    return {"up": up, "down": down, "flat": flat, "scanned": n,
            "up_ratio": round(up / n * 100, 1) if n else None}


def fetch_board_constituents(bk_code: str, limit: int = 20) -> list[dict]:
    """板块成分股（按涨幅降序前 limit 只）。bk_code 如 BK1515。"""
    try:
        resp = requests.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params={"pn": 1, "pz": max(limit, 1), "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f3", "fs": f"b:{bk_code}", "fields": "f2,f3,f12,f14,f62"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        diff = (resp.json().get("data") or {}).get("diff") or []
    except Exception:
        return []
    return [{"code": x.get("f12"), "name": x.get("f14"),
             "price": x.get("f2"), "pct": x.get("f3"),
             "main_inflow": x.get("f62") if isinstance(x.get("f62"), (int, float)) else None}
            for x in diff]


# ---------------- 存储：云库 ----------------

def _get_conn():
    from app import get_conn  # 延迟导入避免循环依赖（与 bili_monitor 同款）
    return get_conn()


SNAPSHOT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS sa_sector_snapshots (
    snap_date  DATE NOT NULL,
    code       VARCHAR(16) NOT NULL,
    name       VARCHAR(64) NOT NULL DEFAULT '',
    kind       VARCHAR(12) NOT NULL DEFAULT 'industry',
    pct        NUMERIC(8,2),
    turnover   NUMERIC(18,2),
    turnover_rate NUMERIC(8,2),
    main_inflow   NUMERIC(18,2),
    up_count   INTEGER,
    down_count INTEGER,
    lead_stock VARCHAR(32) NOT NULL DEFAULT '',
    lead_stock_pct NUMERIC(8,2),
    quote_ts   TIMESTAMPTZ,
    PRIMARY KEY (snap_date, code)
);
CREATE TABLE IF NOT EXISTS sa_sector_daily (
    snap_date  DATE PRIMARY KEY,
    zt_total   INTEGER,
    zt_max_lb  INTEGER,
    zt_sum_zbc INTEGER,
    zt_by_board JSONB NOT NULL DEFAULT '{}'::jsonb,
    breadth    JSONB NOT NULL DEFAULT '{}'::jsonb,
    indexes    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sa_sector_snap_date ON sa_sector_snapshots (snap_date DESC);
"""


def ensure_tables():
    ddl = SNAPSHOT_TABLE_DDL
    for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute(stmt)


def _is_trading_day(d: date) -> bool:
    """周末不算交易日；法定节假日不判断（快照空跑一天无副作用）。"""
    return d.weekday() < 5


def save_snapshot(rows: list[dict], zt: dict, breadth: dict, indexes: list[dict],
                  snap_date: date | None = None) -> dict:
    """写入当日快照（整日覆盖式 upsert，重复跑安全）。"""
    d = snap_date or date.today()
    ensure_tables()
    from psycopg2.extras import Json, execute_values
    with _get_conn() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO sa_sector_snapshots
               (snap_date, code, name, kind, pct, turnover, turnover_rate, main_inflow,
                up_count, down_count, lead_stock, lead_stock_pct, quote_ts)
               VALUES %s
               ON CONFLICT (snap_date, code) DO UPDATE SET
                 name=EXCLUDED.name, kind=EXCLUDED.kind, pct=EXCLUDED.pct,
                 turnover=EXCLUDED.turnover, turnover_rate=EXCLUDED.turnover_rate,
                 main_inflow=EXCLUDED.main_inflow, up_count=EXCLUDED.up_count,
                 down_count=EXCLUDED.down_count, lead_stock=EXCLUDED.lead_stock,
                 lead_stock_pct=EXCLUDED.lead_stock_pct, quote_ts=EXCLUDED.quote_ts""",
            [(d, r["code"], r["name"], r["kind"], r["pct"], r["turnover"],
              r["turnover_rate"], r["main_inflow"], r["up_count"], r["down_count"],
              r["lead_stock"], r["lead_stock_pct"], r["quote_ts"]) for r in rows],
            page_size=500)
        cur.execute(
            """INSERT INTO sa_sector_daily
               (snap_date, zt_total, zt_max_lb, zt_sum_zbc, zt_by_board, breadth, indexes)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (snap_date) DO UPDATE SET
                 zt_total=EXCLUDED.zt_total, zt_max_lb=EXCLUDED.zt_max_lb,
                 zt_sum_zbc=EXCLUDED.zt_sum_zbc, zt_by_board=EXCLUDED.zt_by_board,
                 breadth=EXCLUDED.breadth, indexes=EXCLUDED.indexes""",
            (d, zt.get("total"), zt.get("max_lb"), zt.get("sum_zbc"),
             Json(zt.get("by_board") or {}), Json(breadth or {}),
             Json({i["code"]: i for i in indexes or {}})))
    return {"date": d.isoformat(), "boards": len(rows),
            "zt_total": zt.get("total"), "breadth": breadth}


def collect_once(snap_date: date | None = None, with_breadth: bool = True) -> dict:
    """采集一轮：板块全量 + 涨停池 + 指数 (+ 市场宽度)，写库。可重入锁保护。"""
    with _state_lock:
        if _state["snapshooting"]:
            return {"skipped": True, "reason": "already running"}
        _state["snapshooting"] = True
    try:
        boards = fetch_all_boards()
        zt = fetch_zt_pool()
        indexes = fetch_index_overview()
        breadth = fetch_market_breadth() if with_breadth else {}
        result = save_snapshot(boards, zt, breadth, indexes, snap_date)
        _state["last_result"] = result
        _state["last_run"] = datetime.now().isoformat(timespec="seconds")
        return result
    finally:
        with _state_lock:
            _state["snapshooting"] = False


# ---------------- 分析：动量 / 轮动评分 ----------------

def _load_history(days: int = 6) -> list[tuple]:
    """取最近 days 个快照日的板块数据（升序）。返回 [(snap_date, code, name, kind, pct, main_inflow, turnover)]。"""
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT snap_date, code, name, kind, pct, main_inflow, turnover
               FROM (SELECT DISTINCT snap_date FROM sa_sector_snapshots
                     ORDER BY snap_date DESC LIMIT %s) d
               JOIN sa_sector_snapshots s USING (snap_date)
               ORDER BY snap_date, code""",
            (days,))
        return cur.fetchall()


def _load_daily_meta(days: int = 6) -> list[dict]:
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT snap_date, zt_total, zt_max_lb, zt_sum_zbc, zt_by_board,
                      breadth, indexes FROM sa_sector_daily
               ORDER BY snap_date DESC LIMIT %s""", (days,))
        out = []
        for r in cur.fetchall():
            out.append({
                "date": r[0].isoformat(), "zt_total": r[1], "zt_max_lb": r[2],
                "zt_sum_zbc": r[3],
                "zt_by_board": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}"),
                "breadth": r[5] if isinstance(r[5], dict) else json.loads(r[5] or "{}"),
                "indexes": r[6] if isinstance(r[6], dict) else json.loads(r[6] or "{}"),
            })
        return list(reversed(out))  # 升序


def _pct_rank(values: list[float], v: float) -> float:
    """v 在 values 中的百分位（0~1）。"""
    if not values:
        return 0.5
    below = sum(1 for x in values if x < v)
    return below / len(values)


def build_overview(with_details: bool = True) -> dict:
    """板块轮动总览：当日榜 + N日动量 + 轮动评分 + 市场情绪。

    数据不足（冷启动 <2 个快照日）时动量/评分字段返回空但当日榜可用。
    """
    history = _load_history(6)
    if not history:
        return {"empty": True,
                "message": "还没有快照数据。点「立即采集」，或等收盘后自动采集。",
                "indexes": fetch_index_overview(), "zt": fetch_zt_pool()}

    latest_date = history[-1][0]
    # 最新日全部板块
    latest: dict[str, dict] = {}
    for snap_date, code, name, kind, pct, inflow, turnover in history:
        if snap_date == latest_date:
            latest[code] = {"code": code, "name": name, "kind": kind, "pct": pct,
                            "main_inflow": inflow, "turnover": turnover}
    # 按板块聚合历史涨幅（N日累计）
    by_code_dates: dict[str, dict] = {}
    for snap_date, code, name, kind, pct, inflow, turnover in history:
        by_code_dates.setdefault(code, {})[snap_date] = pct
    dates_sorted = sorted({r[0] for r in history})
    d3 = dates_sorted[-4:]   # 3日动量窗口（最多4个点=3段）
    d5 = dates_sorted[-6:]   # 5日

    all_pct = [v["pct"] for v in latest.values() if v["pct"] is not None]
    pos_inflows = [v["main_inflow"] for v in latest.values()
                   if v["main_inflow"] is not None and v["main_inflow"] > 0]

    zt_today = fetch_zt_pool()
    boards = []
    for code, info in latest.items():
        pct = info["pct"]
        inflow = info["main_inflow"]
        # N日累计涨幅（简单求和近似复利，板块级别够用）
        mom3 = (sum(by_code_dates[code].get(d) or 0 for d in d3) if len(d3) >= 2 else None)
        mom5 = (sum(by_code_dates[code].get(d) or 0 for d in d5) if len(d5) >= 3 else None)
        # 轮动评分
        score_pct = _pct_rank(all_pct, pct) * 40 if pct is not None else 0
        if inflow is not None and inflow > 0 and pos_inflows:
            score_inflow = _pct_rank(pos_inflows, inflow) * 30
        else:
            score_inflow = 0
        zt_cnt = zt_today.get("by_board", {}).get(info["name"], 0)
        score_zt = min(zt_cnt * 4, 20)
        score_mom = 0
        if mom3 is not None and mom3 > 0:
            score_mom = min(mom3 * 2, 10)
        score = round(score_pct + score_inflow + score_zt + score_mom, 1)
        boards.append({
            **info, "pct_rank": round(_pct_rank(all_pct, pct) * 100) if pct is not None else None,
            "mom3": round(mom3, 2) if mom3 is not None else None,
            "mom5": round(mom5, 2) if mom5 is not None else None,
            "zt_count": zt_cnt, "score": score,
        })

    boards.sort(key=lambda b: b["score"], reverse=True)
    daily_meta = _load_daily_meta(6)

    out = {
        "empty": False,
        "snap_date": latest_date.isoformat(),
        "days_collected": len(dates_sorted),
        "indexes": fetch_index_overview(),
        "zt": {k: zt_today[k] for k in ("qdate", "total", "max_lb", "sum_zbc", "by_board")},
        "breadth": (daily_meta[-1].get("breadth") if daily_meta else {}),
        "daily_history": daily_meta,
        "boards": boards[:200] if with_details else boards[:50],
        "total_boards": len(boards),
        "momentum_note": (f"已积累 {len(dates_sorted)} 个交易日快照"
                          + ("，动量信号完整" if len(dates_sorted) >= 4
                             else "，历史不足，动量/评分信号将随积累变准")),
    }
    return out


# ---------------- Claude 报告摘要 ----------------

def digest(hours: int = 24) -> str:
    """给 Claude 定时报告用的板块轮动 markdown 摘要。"""
    ov = build_overview(with_details=False)
    if ov.get("empty"):
        return "（板块快照数据尚未积累，无轮动摘要）"
    lines = [f"## 板块轮动（快照 {ov['snap_date']}）"]
    idx = ov.get("indexes") or []
    if idx:
        lines.append("指数：" + "，".join(
            f"{i['name']} {i['pct']:+.2f}%" for i in idx if i.get("pct") is not None))
    zt = ov.get("zt") or {}
    br = ov.get("breadth") or {}
    if br:
        lines.append(f"宽度：上涨 {br.get('up')} / 下跌 {br.get('down')}（上涨占比 {br.get('up_ratio')}%）")
    if zt:
        lines.append(f"涨停 {zt.get('total')} 家，最高 {zt.get('max_lb')} 连板，炸板 {zt.get('sum_zbc')} 次")
    boards = ov.get("boards") or []
    top = [b for b in boards if (b.get("pct") or 0) > 0][:8]
    bottom = sorted([b for b in boards if b.get("pct") is not None],
                    key=lambda b: b["pct"])[:5]
    if top:
        lines.append("领涨板块：" + "，".join(
            f"{b['name']} {b['pct']:+.2f}%" + (f"（{b['zt_count']}涨停）" if b["zt_count"] else "")
            for b in top))
    if bottom:
        lines.append("领跌板块：" + "，".join(f"{b['name']} {b['pct']:+.2f}%" for b in bottom))
    hot = boards[:5]
    if hot and hot[0].get("score"):
        lines.append("轮动评分最高（主线候选）：" + "，".join(
            f"{b['name']}({b['score']}分)" for b in hot))
    return "\n".join(lines)


# ---------------- 自动采集线程 ----------------

def _is_after_close(now: datetime) -> bool:
    """收盘后（15:10 之后）才算当日快照时间。"""
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (15, 10)


def _sector_auto_loop():
    """交易日收盘后（15:10-23:59 间每小时检查一次）自动采集当日快照；已采过则跳过。

    判重：sa_sector_daily 当日已有记录即跳过（整日覆盖 upsert，手动重跑也安全）。
    """
    while True:
        try:
            now = datetime.now()
            if _is_after_close(now):
                with _get_conn() as conn, conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM sa_sector_daily WHERE snap_date = %s",
                                (now.date(),))
                    done = cur.fetchone()
                if not done:
                    try:
                        result = collect_once()
                        print(f"[sector] auto snapshot: {result}", flush=True)
                        time.sleep(600)  # 采完歇 10 分钟再回主循环
                    except Exception as exc:
                        print(f"[sector] auto snapshot failed: {exc}", flush=True)
            time.sleep(1800)  # 半小时检查一次
        except Exception as exc:
            print(f"[sector] loop error: {exc}", flush=True)
            time.sleep(600)
