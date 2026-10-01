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
import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import os

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

_state = {"snapshooting": False, "last_run": None, "last_result": None,
          # 采集健康度（2026-10-01 加，见 _note_collect_* ）
          "consec_fail": 0, "last_error": "", "last_error_at": None,
          "backoff_until": None, "notified_fail": False}
_state_lock = threading.Lock()


def get_status() -> dict:
    with _state_lock:
        return dict(_state)


# ---------------- 采集健康度：失败退避 + 告警 ----------------
#
# 为什么要这个（2026-09-21 → 10-01 实测）：
# 东财把 push2* 全封之后，_intraday_loop 并没有停下来 —— 它每 5 分钟照常打 12 个
# clist 请求，日志里连着 87 轮 "intraday collect failed"。也就是说：
#   1) 它在给一个已经死掉的端点永久续期，封禁永远不会自己解开；
#   2) 失败只 print 到日志，页面和微信都静默 —— 板块数据停在 2026-09-21，
#      用户看到"407 个板块净流出、半导体流出 55 亿"，以为是当天的，其实是 9 天前的。
# 所以：失败要退避、要上报、要能从接口看见。

FAIL_ALERT_THRESHOLD = 3      # 连续失败几次后推一次告警
BACKOFF_BASE_SECONDS = 300    # 首次失败后等 5 分钟
BACKOFF_MAX_SECONDS = 3600    # 最多退到 1 小时


def _backoff_seconds(fail: int) -> int:
    """指数退避：5min -> 10 -> 20 -> 40 … 封顶 1h。"""
    return min(BACKOFF_BASE_SECONDS * (2 ** max(0, fail - 1)), BACKOFF_MAX_SECONDS)


def _note_collect_success() -> None:
    with _state_lock:
        was_failing = _state["consec_fail"] > 0
        _state["consec_fail"] = 0
        _state["last_error"] = ""
        _state["backoff_until"] = None
        _state["notified_fail"] = False
    if was_failing:
        print("[sector] collect recovered", flush=True)


def _note_collect_failure(exc: Exception) -> int:
    """记一次失败，返回该睡多久（秒）。连续失败到阈值时推一次告警。"""
    with _state_lock:
        _state["consec_fail"] += 1
        fail = _state["consec_fail"]
        _state["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        _state["last_error_at"] = datetime.now().isoformat(timespec="seconds")
        wait = _backoff_seconds(fail)
        _state["backoff_until"] = (datetime.now()
                                   + timedelta(seconds=wait)).isoformat(timespec="seconds")
        should_notify = (fail == FAIL_ALERT_THRESHOLD and not _state["notified_fail"])
        if should_notify:
            _state["notified_fail"] = True

    print(f"[sector] collect failed #{fail}, backoff {wait}s: "
          f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)

    if should_notify:
        try:
            import notifier
            notifier.notify(
                "🔌 板块采集失败",
                f"板块数据采集已连续失败 {fail} 次。\n\n"
                f"最后错误：{type(exc).__name__}: {str(exc)[:200]}\n\n"
                f"已自动退避到 {wait // 60} 分钟一轮。"
                f"页面上的板块数据是旧的，不是当天的。\n"
                f"（来自 stock-advisor 板块监控）",
                event="sector_collect_failed")
            print(f"[sector] collect-failure alert sent (fail={fail})", flush=True)
        except Exception as notify_exc:
            print(f"[sector] collect-failure alert failed: {notify_exc}", flush=True)
    return wait


def collect_health() -> dict:
    """给 /api/sector/status 用：采集是否健康 + 数据陈旧几天。"""
    st = get_status()
    out = {
        "consec_fail": st["consec_fail"],
        "last_error": st["last_error"],
        "last_error_at": st["last_error_at"],
        "backoff_until": st["backoff_until"],
        "healthy": st["consec_fail"] == 0,
    }
    try:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT max(snap_date) FROM sa_sector_snapshots")
            row = cur.fetchone()
        latest = row[0] if row else None
        if latest:
            out["latest_snap_date"] = latest.isoformat()
            out["stale_days"] = (date.today() - latest).days
            out["stale"] = out["stale_days"] >= 2
    except Exception as exc:                      # 查库失败不该拖垮状态接口
        out["stale_days"] = None
        out["stale"] = None
        out["db_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
    return out


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


# ---------------- 外部数据服务（多厂商聚合）----------------
#
# 2026-10-01 起板块快照优先走 data_service（见 data_service/README.md）。
# 为什么不再直连东财：sector._sector_intraday_loop 交易时段每 5 分钟一轮，
# 每轮 12 个 clist 请求全打 push2delay 一个 host = 576 请求/日，触发 WAF；
# 被封后循环不停继续捶，封禁被永久续期，且失败只进日志，页面静默显示 9 天前
# 的旧数据（server.log 实测 321 轮 ≈ 3852 个请求）。
#
# data_service 内部用同花顺 + 开盘红 + 新浪三家互不相关的源，按 host 记账限额，
# 任一源挂掉自动降级并在 /boards 的 degraded 字段里明说。
#
# 回落到东财而不是直接失败：等东财解封就是免费的双保险。

DATA_SERVICE_URL = os.environ.get("SA_DATA_SERVICE_URL", "").strip()


def _load_sector_conf() -> dict:
    """config.yaml 的 sector 段；缺失用默认。"""
    try:
        import yaml
        text = (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
        return dict((yaml.safe_load(text) or {}).get("sector") or {})
    except Exception:
        return {}


def _data_service_url() -> str:
    """服务地址：环境变量优先于 config.yaml，留空则不启用。"""
    if DATA_SERVICE_URL:
        return DATA_SERVICE_URL
    return str(_load_sector_conf().get("data_service_url") or "").strip()


def _boards_via_service() -> list[dict]:
    """从 data_service 拉全量板块。字段名与 _row_to_snapshot 对齐。"""
    url = _data_service_url()
    if not url:
        raise RuntimeError("未配置 data_service_url")
    resp = requests.get(f"{url.rstrip('/')}/boards",
                        params={"refresh": "true"}, timeout=90)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"服务返回非对象: {type(payload).__name__}")
    boards = payload.get("boards") or []
    if not boards:
        raise RuntimeError("服务返回 0 个板块")

    # 服务已算好评分，这里只做字段收敛 + 记录降级状态
    degraded = bool(payload.get("degraded"))
    if degraded:
        for note in payload.get("notes", []):
            print(f"[sector] data_service degraded: {note}", flush=True)
    out = []
    for b in boards:
        out.append({
            "code": b.get("code") or "",
            "name": b.get("name") or "",
            "kind": b.get("kind") or "industry",
            "pct": b.get("pct"),
            "turnover": b.get("turnover"),
            "turnover_rate": b.get("turnover_rate"),
            "main_inflow": b.get("main_inflow"),
            "up_count": b.get("up_count"),
            "down_count": b.get("down_count"),
            "lead_stock": b.get("lead_stock") or "",
            "lead_stock_code": b.get("lead_stock_code") or "",
            "lead_stock_pct": b.get("lead_stock_pct"),
            "quote_ts": b.get("quote_ts"),
            "score": b.get("score"),
        })
    print(f"[sector] data_service: {len(out)} boards, "
          f"sources={payload.get('sources_used')}, degraded={degraded}, "
          f"data_date={payload.get('data_date')}"
          f"{' (非交易日)' if payload.get('is_trading_day') is False else ''}",
          flush=True)
    return out


def service_data_date() -> date | None:
    """问服务要真实数据日（休市日 != 今天）。

    用途：`save_snapshot` 入库日期。休市日采集到的是上一交易日的数据，
    用今天入库会在 sa_sector_snapshots 写进错标日期的行。
    拿不到就返回 None，调用方退回当天（保持原行为）。
    """
    url = _data_service_url()
    if not url:
        return None
    try:
        resp = requests.get(f"{url.rstrip('/')}/health", timeout=30)
        resp.raise_for_status()
        h = resp.json()
        # /health 不带 data_date，改从 boards 的缓存快照里取
        if not h.get("is_trading_day"):
            return None
        return date.fromisoformat(h["data_date"][:10])
    except Exception:
        return None


def fetch_all_boards() -> list[dict]:
    """全量板块快照。优先 data_service（多厂商），失败回落东财直连。"""
    if _data_service_url():
        try:
            return _boards_via_service()
        except Exception as exc:
            print(f"[sector] data_service 不可用，回落东财: "
                  f"{type(exc).__name__}: {str(exc)[:150]}", flush=True)
    return _fetch_all_boards_eastmoney()


def _fetch_all_boards_eastmoney() -> list[dict]:
    """东财直连（回落路径）。注意：这条路径已被 WAF 掐断，保留只为解封后可用。"""
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
    """板块成分股（按涨幅降序前 limit 只）。bk_code 如 BK1515 或 881142。

    优先走 data_service（东财 clist 已于 2026-10-01 实测被 WAF 全封）。
    服务端只给 code/name，没有涨跌幅 —— 涨跌幅用腾讯行情按 code 批量补，
    这样「点板块行看成分股 + 红绿标注」的功能不丢。
    """
    if _data_service_url():
        items = fetch_board_constituents_via_service(bk_code, limit=limit)
        if items:
            return _attach_quotes(items)
    # 回落东财（等解封即免费双保险）
    try:
        resp = requests.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params={"pn": 1, "pz": max(limit, 1), "po": 1, "np": 1, "fltt": 2,
                    "invt": 2, "fid": "f3", "fs": f"b:{bk_code}",
                    "fields": "f2,f3,f12,f14,f62"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        diff = (resp.json().get("data") or {}).get("diff") or []
    except Exception:
        return []
    return [{"code": x.get("f12"), "name": x.get("f14"),
             "price": x.get("f2"), "pct": x.get("f3"),
             "main_inflow": x.get("f62") if isinstance(x.get("f62"), (int, float)) else None}
            for x in diff]


def _attach_quotes(items: list[dict]) -> list[dict]:
    """给只有 code/name 的成分股补涨跌幅。

    用 `app.fetch_quotes`（腾讯 qt.gtimg.cn，**一次请求拿全部代码**）。
    不要写成「循环里逐只拉 K 线」—— 那就是 20 只 = 20 个请求打同一 host，
    正是把东财烧死的那种模式（见 _sector_intraday_loop 的注释）。

    取不到就保持 None 而不是 0：前端按「无数据」显示，填 0 会被误读成平盘。
    """
    codes = [str(x.get("code") or "") for x in items]
    codes = [c for c in codes if len(c) == 6]
    if not codes:
        return items
    try:
        from app import fetch_quotes          # 延迟导入，避免循环依赖
    except Exception as exc:
        print(f"[sector] fetch_quotes 不可用: {exc}", flush=True)
        return items
    quotes = fetch_quotes(codes)
    for it in items:
        q = quotes.get(str(it.get("code") or "")) or {}
        if q.get("error"):
            continue                          # 保持 None
        it["price"] = q.get("price")
        it["pct"] = q.get("change_pct")
        it["main_inflow"] = q.get("main_inflow")   # 腾讯快照无此字段，恒 None
    return items


def _em_secid(code: str) -> str | None:
    """腾讯式代码 → 东财 secid 市场前缀（实测映射）：沪 1.x / 深 0.x / 港股 116.x。"""
    code = code.strip()
    if re.fullmatch(r"\d{5}", code):
        return f"116.{code}"
    if re.fullmatch(r"\d{6}", code):
        return ("1." if code[0] in "5689" else "0.") + code
    return None


def fetch_board_constituents_via_service(bk_code: str,
                                        limit: int = 20) -> list[dict]:
    """经 data_service 取板块成分股（个股所属板块的反查在服务端做不了，见下）。

    注意 code 体系（实测）：
      - 行业 881xxx -> 服务端 adata 接口直通
      - 概念 3xxxxx -> 服务端要 886xxx，直喂返回 404
    """
    url = _data_service_url()
    if not url:
        return []
    try:
        resp = requests.get(
            f"{url.rstrip('/')}/boards/{bk_code}/constituents",
            params={"limit": limit}, timeout=60)
        if resp.status_code != 200:
            print(f"[sector] 成分股 {bk_code}: HTTP {resp.status_code} "
                  f"{resp.text[:120]}", flush=True)
            return []
        return resp.json().get("items") or []
    except Exception as exc:
        print(f"[sector] 成分股 {bk_code} 取数失败: "
              f"{type(exc).__name__}: {str(exc)[:100]}", flush=True)
        return []


def fetch_stock_boards(code: str, retries: int = 2) -> list[dict]:
    """个股所属板块。返回 [{code, name, pct}]。

    原实现走东财 `push2delay/slist/get`（spt=3），该 host 已于 2026-10-01
    实测被 WAF 全封（连不带参数都失败）。现在优先走 data_service ——
    但服务端目前只能按**板块**查成分股，没有「个股 -> 所属板块」的反查接口
    （需要全市场 code->board 映射，代价高）。所以这里仍走新浪
    `Market_Center.getHQNodeData` 那条路会太慢，退而求其次：
    仍尝试东财，等它解封就是免费双保险；失败返回 []，前端按「无板块数据」处理。
    """
    secid = _em_secid(code)
    if not secid:
        return []
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                "https://push2delay.eastmoney.com/api/qt/slist/get",
                params={"spt": "3", "fltt": 2, "invt": 2, "secid": secid,
                        "fields": "f12,f14,f3", "pn": 1, "pz": 40, "po": 1, "np": 1},
                headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            diff = (resp.json().get("data") or {}).get("diff") or []
            return [{"code": x.get("f12"), "name": x.get("f14"),
                     "pct": x.get("f3") if isinstance(x.get("f3"), (int, float)) else None}
                    for x in diff]
        except Exception:
            if attempt < retries:
                time.sleep(1 + attempt)
    return []


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

    # 板块代码可能为空（data_service 的新浪源不给 code，实测 969 个里 664 个有）。
    # sa_sector_snapshots 的主键是 (snap_date, code)，code 为空会让整批 upsert
    # 退化成"同一行反复覆盖"，最后只剩 1 个板块。丢掉无 code 的行并计数上报。
    usable = [r for r in rows if (r.get("code") or "").strip()]
    dropped = len(rows) - len(usable)
    if dropped:
        print(f"[sector] save_snapshot 丢弃 {dropped} 个无 code 的板块"
              f"（主键需要 code）", flush=True)
    if not usable:
        raise RuntimeError(f"全部 {len(rows)} 个板块都没有 code，无法入库")

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
              r["lead_stock"], r["lead_stock_pct"], r["quote_ts"])
             for r in usable],
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
    return {"date": d.isoformat(), "boards": len(usable),
            "dropped_no_code": dropped,
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
    """取最近 days 个快照日的板块数据（升序）。返回 [(snap_date, code, name, kind, pct, main_inflow, turnover)]。

    NUMERIC 列 psycopg2 回来是 Decimal，和轮动评分的 float 混算会 TypeError
    （2026-09-16 实炸：float + Decimal），统一转 float。
    """
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT snap_date, code, name, kind, pct, main_inflow, turnover
               FROM (SELECT DISTINCT snap_date FROM sa_sector_snapshots
                     ORDER BY snap_date DESC LIMIT %s) d
               JOIN sa_sector_snapshots s USING (snap_date)
               ORDER BY snap_date, code""",
            (days,))
        return [(d, code, name, kind,
                 float(pct) if pct is not None else None,
                 float(mi) if mi is not None else None,
                 float(tv) if tv is not None else None)
                for d, code, name, kind, pct, mi, tv in cur.fetchall()]


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
            # 分项拆开：排查「为什么这个板块排这么前」时不用去猜。
            # 字段名与 data_service 的 score_parts 对齐。
            "score_parts": {"pct": round(score_pct, 1),
                            "flow": round(score_inflow, 1),
                            "zt": score_zt, "mom": round(score_mom, 1)},
        })

    boards.sort(key=lambda b: b["score"], reverse=True)
    daily_meta = _load_daily_meta(6)

    # 数据陈旧度（2026-10-01 加）。原来只给 snap_date 这么一个普通字段，
    # 页面上和真实数据混在一起显示 —— 9 月 21 日之后采集一直失败，
    # 页面照常渲染 9 天前的板块表，没人看出那是旧的。
    # 库里存的是 date，psycopg2 回来也是 date；但如果哪条路径塞了字符串进来，
    # 这里会炸在减法上，所以统一规整一次。
    if isinstance(latest_date, str):
        latest_date = date.fromisoformat(latest_date[:10])
    stale_days = (date.today() - latest_date).days
    out = {
        "empty": False,
        "snap_date": latest_date.isoformat(),
        "stale_days": stale_days,
        "stale": stale_days >= 2,
        "stale_note": (f"⚠️ 板块数据停留在 {latest_date.isoformat()}，"
                       f"已落后 {stale_days} 个自然日，不是当天的。"
                       f"原因通常是数据源被限流/掐断，采集在静默失败。") if stale_days >= 2 else "",
        "days_collected": len(dates_sorted),
        "indexes": fetch_index_overview(),
        "zt": {k: zt_today[k] for k in ("qdate", "total", "max_lb", "sum_zbc", "by_board")},
        "breadth": (daily_meta[-1].get("breadth") if daily_meta else {}),
        "daily_history": daily_meta,
        # 全量返回，不截断（2026-09-30 起）。
        # 原来是 `boards[:200] if with_details else boards[:50]`，前端再 slice(0,60)，
        # 结果 1031 个板块只露出 60 个 —— 而「净流出板块」恰好总排在后面，
        # 于是评分榜看上去"一个流出的都没有"，其实是没显示出来。
        # 页面侧加了搜索/筛选来保证 1000+ 行还能用。
        "boards": boards,
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


# ---------------- 个股 ↔ 板块联动 ----------------

def stock_board_exposure(codes: list[str]) -> dict[str, dict]:
    """批量查个股所属板块，并标注每个板块在最新快照中的当日表现/评分排位。

    返回 {code: {"boards": [{code, name, pct, snap_pct, kind, score, zt_count}], "industry": 名称}}

    快照里查不到的板块（slist 可能有快照之外的板块，理论上不应发生）不标 snap 字段。
    """
    if not codes:
        return {}
    out: dict[str, dict] = {}
    # 最新快照日全量板块 → code: row（一次查库，所有股票共用）
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT s.code, s.name, s.kind, s.pct, s.main_inflow
               FROM sa_sector_snapshots s
               JOIN (SELECT MAX(snap_date) AS d FROM sa_sector_snapshots) m
                 ON s.snap_date = m.d""")
        snap_by_code = {r[0]: {"code": r[0], "name": r[1], "kind": r[2],
                               "snap_pct": (float(r[3]) if r[3] is not None else None),
                               "main_inflow": (float(r[4]) if r[4] is not None else None)}
                        for r in cur.fetchall()}
        # 评分需要 zt_count —— 从 overview 的口径重算太重，这里只做涨幅排位近似：
        pcts = sorted(v["snap_pct"] for v in snap_by_code.values()
                      if v["snap_pct"] is not None)
        for code in codes:
            boards = fetch_stock_boards(code)
            enriched = []
            industry = ""
            for b in boards:
                snap = snap_by_code.get(b["code"])
                row = {**b}
                if snap:
                    row.update({"kind": snap["kind"],
                                "snap_pct": snap["snap_pct"]})
                    if pcts and snap["snap_pct"] is not None:
                        below = sum(1 for x in pcts if x < snap["snap_pct"])
                        row["pct_rank"] = round(below / len(pcts) * 100)
                    if snap["kind"] == "industry" and not industry:
                        industry = snap["name"]
                enriched.append(row)
            out[code] = {"boards": enriched, "industry": industry}
    return out


# ---------------- 板块轮动预警（评分骤升 / 新高上榜 / 涨停聚集） ----------------

ALERT_STATE_FILE = BASE_DIR / "data" / "sector_alert_state.json"


def _load_alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_alert_state(state: dict) -> None:
    ALERT_STATE_FILE.parent.mkdir(exist_ok=True)
    ALERT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def check_rotation_alerts(top_n: int = 10, score_jump: float = 15.0,
                          zt_surge: int = 5) -> list[str]:
    """盘后跑一次轮动预警，返回本次触发的提醒文本列表（调用方决定是否推送）。

    触发条件（相对上次快照日）：
    - 新主线候选：评分 Top N 里出现了上轮不在 Top N 的板块
    - 涨停聚集：单板块涨停家数 ≥ zt_surge 且较上轮增加
    阈值内不打扰。状态存本地 JSON（重跑同一天不重复报）。
    """
    ov = build_overview(with_details=False)
    if ov.get("empty") or not ov.get("snap_date"):
        return []
    today = ov["snap_date"]
    boards = ov.get("boards") or []
    if not boards:
        return []
    top_now = {b["code"]: b for b in boards[:top_n]}
    zt_by_board = ov.get("zt", {}).get("by_board") or {}

    state = _load_alert_state()
    alerts: list[str] = []
    new_key = f"top_{today}"
    prev_key = f"top_{state.get('last_date')}" if state.get("last_date") else None
    prev_top = (state.get(prev_key) or {}) if prev_key else {}

    # 1) 新主线候选
    entered = [b for code, b in top_now.items() if code not in prev_top]
    if entered:
        lines = [f"• {b['name']}（{b['kind']}，{b.get('pct') or 0:+.2f}%，评分 {b['score']}）"
                 for b in entered[:5]]
        alerts.append("🧭 新进主线候选（评分 Top%d）：\n%s" % (top_n, "\n".join(lines)))

    # 2) 涨停聚集（今日 zt_by_board 里家数 ≥ 阈值；与上轮比增量）
    zt_prev = state.get(f"zt_{today}") or {}
    surged = []
    for name, cnt in zt_by_board.items():
        if cnt >= zt_surge and cnt > (zt_prev.get(name) or 0):
            surged.append((name, cnt, cnt - (zt_prev.get(name) or 0)))
    if surged:
        surged.sort(key=lambda x: -x[1])
        alerts.append("🔥 板块涨停聚集：\n" + "\n".join(
            f"• {name}：涨停 {cnt} 家（较昨日 +{inc}）" for name, cnt, inc in surged[:5]))

    # 更新状态（只保留最近 3 天，防文件膨胀）
    new_state = {"last_date": today}
    dates = sorted({k.split("_", 1)[1] for k in
                    [*(state.keys()), new_key, f"zt_{today}"] if "_" in k})
    for d in dates[-3:]:
        if f"top_{d}" in state or d == today:
            new_state[f"top_{d}"] = state.get(f"top_{d}") or {c: b.get("score") for c, b in top_now.items()}
        if f"zt_{d}" in state or d == today:
            new_state[f"zt_{d}"] = state.get(f"zt_{d}") or zt_by_board
    _save_alert_state(new_state)
    return alerts


# ---------------- 盘中实时监控（内存缓存，不落库） ----------------
# 交易时段每几分钟采样一次全量板块，供前端轮询 + 急拉/涨停骤增预警。
# 盘中数据只存内存（重启丢失无妨，下一轮采样即恢复）；正式历史以收盘快照为准。

_intraday: dict = {
    "updated_at": None,     # 本次采样完成时间
    "quote_ts": None,       # 数据源行情时间（f124 最大值，判断延迟）
    "boards": [],           # 全量板块 [{code,name,kind,pct,main_inflow,zt_count}]
    "zt": {},               # 涨停池摘要
    "indexes": [],
    "breadth": {},          # 盘中宽度（低频刷新，见 _intraday_loop）
    "prev_boards_pct": {},  # 上次采样涨幅 {code: pct}（算急拉用）
    "prev_zt_by_board": {}, # 上次采样涨停分布
}
_intraday_lock = threading.Lock()
_intraday_alert_cool: dict[str, float] = {}   # 板块名 -> 上次预警时间戳（冷却 10 分钟）


def _in_trading_session(now: datetime) -> bool:
    """A 股交易时段（含集合竞价 9:15 起、收盘 15:05 止；午休不算）。"""
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    if (9, 15) <= hm <= (11, 35) or (12, 55) <= hm <= (15, 5):
        return True
    return False


def _intraday_collect() -> dict:
    """采样一轮盘中数据写入缓存；返回摘要。异常时缓存保持上次内容。"""
    boards = fetch_all_boards()
    zt = fetch_zt_pool()
    indexes = fetch_index_overview()
    now = datetime.now()
    with _intraday_lock:
        prev_pct = {b["code"]: b.get("pct") for b in _intraday["boards"]}
        prev_zt = _intraday["zt"].get("by_board") or {}
        quote_ts = max((b["quote_ts"] for b in boards if b.get("quote_ts")), default=None)
        _intraday.update({
            "updated_at": now.isoformat(timespec="seconds"),
            "quote_ts": quote_ts,
            "boards": boards,
            "zt": {k: zt[k] for k in ("qdate", "total", "max_lb", "sum_zbc", "by_board")},
            "indexes": indexes,
            "prev_boards_pct": prev_pct,
            "prev_zt_by_board": prev_zt,
        })
    return {"boards": len(boards), "zt_total": zt.get("total")}


def _intraday_breadth_refresh() -> None:
    """盘中宽度单独低频刷新（全扫 ~20s，不该跟板块采样一个频率）。"""
    try:
        breadth = fetch_market_breadth()
        with _intraday_lock:
            _intraday["breadth"] = breadth
    except Exception as exc:
        print(f"[sector] intraday breadth failed: {exc}", flush=True)


def _intraday_check_alerts() -> list[tuple[str, str]]:
    """对比上次采样找急拉/涨停骤增。返回 [(板块名, 文本)]，带冷却。"""
    with _intraday_lock:
        boards = list(_intraday["boards"])
        prev_pct = dict(_intraday["prev_boards_pct"])
        zt_by_board = dict((_intraday["zt"] or {}).get("by_board") or {})
        prev_zt = dict(_intraday.get("prev_zt_by_board") or {})
    now_ts = time.time()
    cooldown = 600  # 同板块 10 分钟冷却
    alerts: list[tuple[str, str]] = []
    has_prev = bool(prev_pct)  # 冷启动首次采样没有基准，不产预警（防误报）
    if has_prev:
        for b in boards:
            pct, prev = b.get("pct"), prev_pct.get(b["code"])
            if pct is None or prev is None:
                continue
            delta = pct - prev
            # 急拉：采样间隔内涨幅跳升 ≥1.5 个点且当前 ≥3%
            if delta >= 1.5 and pct >= 3:
                if now_ts - _intraday_alert_cool.get(b["name"], 0) >= cooldown:
                    _intraday_alert_cool[b["name"]] = now_ts
                    alerts.append((b["name"],
                                   f"⚡ {b['name']} 急拉：{prev:+.2f}% → {pct:+.2f}%"
                                   f"（{delta:+.2f} 个点）"))
        # 涨停骤增：本采样窗口某板块涨停 +3 家以上
        for name, cnt in zt_by_board.items():
            inc = cnt - (prev_zt.get(name) or 0)
            if inc >= 3 and now_ts - _intraday_alert_cool.get(f"zt:{name}", 0) >= cooldown:
                _intraday_alert_cool[f"zt:{name}"] = now_ts
                alerts.append((name, f"🔥 {name} 涨停骤增：+{inc} 家（现 {cnt} 家）"))
    return alerts


def get_intraday() -> dict:
    """给 API 的盘中缓存视图（top50 已排好序）。"""
    with _intraday_lock:
        boards = list(_intraday["boards"])
        out = {k: v for k, v in _intraday.items() if k != "boards"}
    if boards:
        boards.sort(key=lambda b: b.get("pct") if b.get("pct") is not None else -999,
                    reverse=True)
    out["boards"] = boards[:50]
    out["total_boards"] = len(boards)
    out["in_session"] = _in_trading_session(datetime.now())
    return out


def load_intraday_conf() -> dict:
    """config.yaml 的 sector 段；缺失用默认。"""
    defaults = {"enabled": True, "interval_minutes": 5,
                "alert_notify": True, "alert_min_delta": 1.5}
    try:
        text = (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
        import yaml
        data = yaml.safe_load(text) or {}
        conf = dict(data.get("sector") or {})
    except Exception:
        conf = {}
    defaults.update({k: v for k, v in conf.items() if v is not None})
    return defaults


def _intraday_loop():
    """盘中监控线程：交易时段每 interval_minutes 采样一次 + 急拉/涨停骤增预警。

    预警通过 notifier 推送（可配 alert_notify=False 关）。宽度低频（每 30 分钟）刷新。

    失败退避（2026-10-01 加）：源被掐时不能照原节奏捶 —— 每 5 分钟 12 个请求
    会让封禁永久续期，而且噪音盖掉真实错误。连续失败按 5/10/20/40… 分钟退避。
    """
    last_breadth = 0.0
    while True:
        try:
            conf = load_intraday_conf()
            interval = max(int(conf.get("interval_minutes") or 5), 1)
            if conf.get("enabled", True) and _in_trading_session(datetime.now()):
                # 退避未到就跳过这一轮（不发起任何请求）
                with _state_lock:
                    until = _state["backoff_until"]
                if until:
                    try:
                        if datetime.now() < datetime.fromisoformat(until):
                            time.sleep(60)
                            continue
                    except ValueError:
                        pass
                try:
                    result = _intraday_collect()
                    _note_collect_success()
                    print(f"[sector] intraday: {result}", flush=True)
                except Exception as exc:
                    wait = _note_collect_failure(exc)
                    time.sleep(min(wait, 300))
                    continue
                if conf.get("alert_notify", True):
                    try:
                        alerts = _intraday_check_alerts()
                        if alerts:
                            import notifier
                            notifier.notify(
                                "⚡ 板块盘中异动",
                                "\n\n".join(a[1] for a in alerts[:6])
                                + f"\n\n（{datetime.now().strftime('%H:%M')} 采样，"
                                  "来自 stock-advisor 盘中板块监控）",
                                event="sector_intraday")
                    except Exception as exc:
                        print(f"[sector] intraday alert failed: {exc}", flush=True)
                if time.time() - last_breadth >= 1800:
                    last_breadth = time.time()
                    _intraday_breadth_refresh()
                time.sleep(interval * 60)
            else:
                time.sleep(300)  # 非交易时段 5 分钟一查（等开盘）
        except Exception as exc:
            print(f"[sector] intraday loop error: {exc}", flush=True)
            time.sleep(120)


# ---------------- 自动采集线程 ----------------

def _snapshot_time() -> tuple[int, int]:
    """当日快照时间（默认 15:10），来自 config.yaml 的 schedule.sector.snapshot_time
    （网页「⚙️ 调度设置」可改）。缺失/坏值回落默认。"""
    try:
        text = (BASE_DIR / "config.yaml").read_text(encoding="utf-8")
        import re
        import yaml
        data = yaml.safe_load(text) or {}
        raw = ((data.get("schedule") or {}).get("sector") or {}).get("snapshot_time")
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(raw or "").strip())
        if m:
            return (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return (15, 10)


def _is_after_close(now: datetime) -> bool:
    """收盘后（默认 15:10 之后，可网页改）才算当日快照时间。"""
    if now.weekday() >= 5:
        return False
    sh, sm = _snapshot_time()
    return (now.hour, now.minute) >= (sh, sm)


def _sector_auto_loop():
    """交易日收盘后（15:10-23:59 间每小时检查一次）自动采集当日快照；已采过则跳过。

    判重：sa_sector_daily 当日已有记录即跳过（整日覆盖 upsert，手动重跑也安全）。

    失败退避（2026-10-01 加）：和 _intraday_loop 共用 _state 的失败计数。
    收盘后这段时间没有行情压力，源被掐时也没必要反复试 —— 退避到最多 1 小时一轮，
    并在连续失败到阈值时推一次告警（原来只 print，9 天没人发现）。
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
                    # 退避未到就跳过本轮（不发起任何请求）
                    with _state_lock:
                        until = _state["backoff_until"]
                    skip = False
                    if until:
                        try:
                            skip = datetime.now() < datetime.fromisoformat(until)
                        except ValueError:
                            skip = False
                    if skip:
                        time.sleep(300)
                        continue
                    try:
                        result = collect_once()
                        _note_collect_success()
                        print(f"[sector] auto snapshot: {result}", flush=True)
                        # 盘后预警：新主线候选 / 涨停聚集 → 微信/邮件
                        try:
                            alerts = check_rotation_alerts()
                            if alerts:
                                import notifier
                                notifier.notify(
                                    "🧭 板块轮动预警",
                                    "\n\n".join(alerts) + f"\n\n（快照 {result.get('date')}，"
                                    "来自 stock-advisor 板块监控）",
                                    event="sector_rotation")
                                print(f"[sector] alerts sent: {len(alerts)}", flush=True)
                        except Exception as exc:
                            print(f"[sector] alert failed: {exc}", flush=True)
                        time.sleep(600)  # 采完歇 10 分钟再回主循环
                    except Exception as exc:
                        wait = _note_collect_failure(exc)
                        time.sleep(min(wait, 600))
                        continue
            time.sleep(1800)  # 半小时检查一次
        except Exception as exc:
            print(f"[sector] loop error: {exc}", flush=True)
            time.sleep(600)
