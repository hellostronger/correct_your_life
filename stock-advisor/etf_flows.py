# -*- coding: utf-8 -*-
"""核心宽基 ETF 资金流（2026-10-17 新增）。

目的：跟踪「国家队常买的那批宽基 ETF」（15 只，见 FAMILY）的
「份额变化 × 单位净值 ≈ 净申赎估算」，给行情页与模拟盘决策一个
「大资金在往哪个方向走」的背景参考。

数据源（全部 akshare，2026-10-17 逐项实测）：
- 沪市（SSE）：ak.fund_etf_scale_sse(date=YYYYMMDD)
    · 按日可取历史；「统计日期」== 请求日（实测 06-30 / 09-30 / 10-16 都对）
    · 份额单位 = 份（akshare 内部已把 TOT_VOL 万份 ×10000）
    · 休市日：服务端 result=None → akshare 内部抛 KeyError（信息含
      "are in the [columns]"）—— 视为「无此日数据」，不是故障
    · 防御：任一行「统计日期」≠ 请求日 → 整批丢弃。防的是「未出数时返回
      上一交易日数据」——push2ex 涨停池就是这种行为，不校验会写进错标日期的行
- 深市（SZSE）：ak.fund_etf_scale_szse()
    · **只有最新快照**：无日期字段、无历史，页面就叫「当前规模」
    · 数据日靠「净值匹配」推断：快照净值 == 东财历史净值里某一日的值
      （实测与东财 T-1 的单位净值逐位一致）→ 取**最近的匹配日**；匹配不到
      就不写（写错日期的行比不写更毒）
    · 所以深市序列从启用日起积累，天然比沪市晚一天起步
- 净值：ak.fund_etf_fund_info_em(fund=code, start_date, end_date)（东财天天基金）
    · 只用来算金额；取「单位净值」列

口径与诚实边界（页面 notes 与 LLM 注入文本里都必须带上）：
- 「份额变化 × 净值」是**净申赎估算**，含申赎套利/做市行为，
  **不等于「国家队净买入」**；
- 数据滞后 T+1（净值还可能更晚），盘中看不到当日数据；
- 深市历史短 → 头几天「近5日/近20日」不完整，页面上明示。

存储 sa_etf_flows(stat_date, code, ...) PK(stat_date, code)；份额差额一律
读侧按相邻记录日计算，不存差额 —— 改口径/修数据后历史自动重算。

写路径一律 execute_values 单条批量：远端 PG 每次往返中位 ~1.9s，
数百行逐行 INSERT 要十几分钟（机构信号层 249 行实测 81.8s，同款教训）。
"""
from __future__ import annotations

import math
import threading
from datetime import date, datetime, timedelta

# 家族 15 只：沪深300/上证50/中证500/中证1000/科创50/创业板/科创创业/A500。
# 选票依据（2026-10-17 定）：① 对应指数的头部 ETF（份额/规模前列）；
# ② 覆盖「大盘-中盘-小盘-双创」四象限，国家队护盘偏好大盘宽基。
# 名称一律以上游返回的「基金简称」为准，这里只是取不到时的兜底。
FAMILY = [
    ("510300", "SH", "沪深300ETF"),        # 华泰柏瑞
    ("510330", "SH", "沪深300ETF华夏"),
    ("159919", "SZ", "沪深300ETF嘉实"),
    ("510050", "SH", "上证50ETF"),         # 华夏
    ("510500", "SH", "中证500ETF"),        # 南方
    ("159922", "SZ", "中证500ETF嘉实"),
    ("512100", "SH", "中证1000ETF"),       # 南方
    ("159845", "SZ", "中证1000ETF华夏"),
    ("588000", "SH", "科创50ETF华夏"),
    ("588080", "SH", "科创板50ETF易方达"),
    ("159915", "SZ", "创业板ETF易方达"),
    ("159781", "SZ", "科创创业ETF易方达"),
    ("563360", "SH", "A500ETF华夏"),       # SSE 简称「A500基金」
    ("159352", "SZ", "中证A500ETF南方"),
    ("159361", "SZ", "中证A500ETF易方达"),
]
FAMILY_MAP = {c: (ex, nm) for c, ex, nm in FAMILY}

DDL = """
CREATE TABLE IF NOT EXISTS sa_etf_flows (
    stat_date   DATE NOT NULL,                 -- 份额数据的统计日
    code        VARCHAR(10) NOT NULL,
    name        VARCHAR(64) NOT NULL DEFAULT '',
    exchange    VARCHAR(4) NOT NULL DEFAULT '',-- SH / SZ
    shares      NUMERIC(24,2),                 -- 基金份额（份）
    nav         NUMERIC(12,4),                 -- 单位净值（元）
    src         VARCHAR(8) NOT NULL DEFAULT '',-- sse / szse
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stat_date, code)
);
"""

_UPSERT_SQL = (
    "INSERT INTO sa_etf_flows (stat_date, code, name, exchange, shares, nav, src) "
    "VALUES %s "
    "ON CONFLICT (stat_date, code) DO UPDATE SET "
    "name = EXCLUDED.name, exchange = EXCLUDED.exchange, "
    "shares = EXCLUDED.shares, "
    "nav = COALESCE(EXCLUDED.nav, sa_etf_flows.nav), "
    "src = EXCLUDED.src, fetched_at = now()"
)

# 同一时刻只允许一个同步在跑（守护线程 16:15 起每 30 分钟一轮，用户也可能
# 手点「同步」；并发跑没有正确性问题 —— ON CONFLICT 幂等 —— 只是白烧请求）
_SYNC_LOCK = threading.Lock()


# ==========================================================================
# 小工具
# ==========================================================================

def _d10(v) -> str | None:
    """任意日期值 → 'YYYY-MM-DD' 字符串。不可解析返回 None。"""
    if v is None:
        return None
    s = str(v).strip()[:10].replace("/", "-")
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return None
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return None


def _num(v) -> float | None:
    """任意值 → float，NaN/Inf/'' 一律 None（与 institution_signals._num 同款）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _row_dict(r) -> dict:
    """统一 tuple / RealDictCursor(dict) 两种行。

    ⚠️ 调用方两种游标都有：/api 端点用默认 tuple 游标，而
    `paper_trading._decide_one` 用 RealDictCursor —— 只支持一种的话，
    注入块在某些路径上会静默变成「读取失败」（机构信号 layer 踩过）。
    ⚠️ 列清单必须与 `_stored` 的 SELECT 一字不差（2026-10-17 测试逮到：
    补净值路径读 r["src"]，而 SELECT 少一列 → KeyError 'src'）。
    """
    cols = ("stat_date", "code", "name", "exchange", "shares", "nav", "src")
    if isinstance(r, dict):
        return {k: r.get(k) for k in cols}
    return dict(zip(cols, r))


_TRADE_DATES_CACHE: dict = {"day": None, "set": None}


def _trade_dates() -> set | None:
    """A 股交易日集合（sina 日历，按天缓存）；拿不到返回 None。

    有它才能把法定假日从「候选日」里干净剔除 —— 不然每个假日每轮同步都
    白打一次 SSE 请求（返回 KeyError 当「无数据」处理，但那是每天固定浪费）。
    """
    today = date.today()
    if _TRADE_DATES_CACHE["day"] == today and _TRADE_DATES_CACHE["set"]:
        return _TRADE_DATES_CACHE["set"]
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        s = set()
        for v in df.iloc[:, 0].tolist():
            d10 = _d10(v)
            if d10:
                s.add(d10)
        if s:
            _TRADE_DATES_CACHE["day"] = today
            _TRADE_DATES_CACHE["set"] = s
            return s
    except Exception:
        pass
    return None


def _match_nav_date(hist: dict, nav: float | None, tol: float = 1e-4) -> str | None:
    """在 {date10: nav} 里找与快照值匹配的**最近**日期；匹配不到返回 None。

    容差 1e-4：两边都只保留 4 位小数，最坏 5e-5 的舍入差。相邻交易日净值
    相同时取较晚的那天（快照语义就是「最新一期」）。
    """
    if not hist or nav is None:
        return None
    hits = [d for d, v in hist.items() if abs(v - nav) <= tol]
    return max(hits) if hits else None


# ==========================================================================
# 取数（模块级函数，测试里直接 monkeypatch 这几个）
# ==========================================================================

def fetch_sse_day(date_str: str) -> dict:
    """沪市某日全部 ETF 份额 → {code: {"name", "shares", "stat_date"}}（只留家族）。

    休市日 akshare 因 result=None 抛 KeyError（信息含 "are in the [columns]"）
    → 识别为「无此日数据」返回 {}；其它异常照抛，由调用方计数上报。
    """
    import akshare as ak
    try:
        df = ak.fund_etf_scale_sse(date=date_str)
    except KeyError as exc:
        if "are in the [columns]" in str(exc):
            return {}
        raise
    out: dict = {}
    if df is None or len(df) == 0:
        return out
    for code, name, sd, shares in zip(
            df["基金代码"], df["基金简称"], df["统计日期"], df["基金份额"]):
        c = str(code).strip().zfill(6)
        if c not in FAMILY_MAP:
            continue
        d10 = _d10(sd)
        if d10 is None or d10.replace("-", "") != str(date_str).strip():
            # 数据日与请求日不符（未出数时可能回退给上一交易日）→ 整批不要
            return {}
        out[c] = {"name": str(name).strip(), "shares": _num(shares), "stat_date": d10}
    return out


def fetch_szse_snapshot() -> dict:
    """深市 ETF 最新快照 → {code: {"name", "shares", "nav"}}（只留家族）。

    ⚠️ 这个表**没有日期字段**（深交所页面就叫「当前规模」）。数据日只能在
    调用方用净值匹配推断 —— 绝不假定「就是今天」。
    """
    import akshare as ak
    df = ak.fund_etf_scale_szse()
    out: dict = {}
    if df is None or len(df) == 0:
        return out
    for code, name, shares, nav in zip(
            df["基金代码"], df["基金简称"], df["基金份额"], df["净值"]):
        c = str(code).strip().zfill(6)
        if c not in FAMILY_MAP:
            continue
        out[c] = {"name": str(name).strip(), "shares": _num(shares), "nav": _num(nav)}
    return out


def fetch_nav_map(code: str, start_str: str, end_str: str) -> dict:
    """某只基金 [start, end] 的东财历史净值 → {date10: 单位净值}。"""
    import akshare as ak
    df = ak.fund_etf_fund_info_em(fund=code, start_date=start_str, end_date=end_str)
    out: dict = {}
    if df is None or len(df) == 0:
        return out
    for d, v in zip(df["净值日期"], df["单位净值"]):
        d10 = _d10(d)
        f = _num(v)
        if d10 and f is not None:
            out[d10] = f
    return out


# ==========================================================================
# 库表读写
# ==========================================================================

def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()


def _stored(conn, since: date) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stat_date, code, name, exchange, shares, nav, src "
            "FROM sa_etf_flows WHERE stat_date >= %s ORDER BY stat_date, code",
            (since,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        d = _row_dict(r)
        d["shares"] = _num(d["shares"])
        d["nav"] = _num(d["nav"])
        out.append(d)
    return out


# ==========================================================================
# 同步
# ==========================================================================

def sync(conn, days: int = 30, today: date | None = None) -> dict:
    """同步份额/净值到 sa_etf_flows（幂等，可反复跑）。

    days = 回补窗口（候选交易日个数）。已存过的沪市日期不重复请求；
    深市快照每次都会拉（只有最新值）。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": False, "why": "已有同步在运行中"}
    try:
        return _sync_locked(conn, days=days, today=today)
    finally:
        _SYNC_LOCK.release()


def _sync_locked(conn, days: int = 30, today: date | None = None) -> dict:
    today = today or date.today()
    days = max(5, min(120, int(days or 30)))
    ensure_table(conn)
    window_start = today - timedelta(days=days * 2 + 20)
    stored = _stored(conn, window_start)
    sh_dates = {r["stat_date"].isoformat() for r in stored if r["exchange"] == "SH"}

    # 候选交易日（最近 days 个）：优先用交易日历剔假日，拿不到才退回周一~周五。
    # 日历只覆盖到它自己的最后一天；超出范围的日期（极少）退回粗筛。
    cal = _trade_dates()
    cal_max = max(cal) if cal else None

    def _is_cand(d: date) -> bool:
        # ⚠️ cal 是 ISO 字符串集合（_trade_dates 返回 "YYYY-MM-DD"）→ 必须比较
        # 字符串形态：date <= str 直接 TypeError（2026-10-17 写测试前自查逮到，
        # 该分支在真实运行时几乎必走 —— 日历可用时 cal/cal_max 都非空）。
        if cal is not None and (cal_max is None or d.isoformat() <= cal_max):
            return d.isoformat() in cal
        return d.weekday() < 5

    want, d = [], today
    while len(want) < days and d >= window_start:
        if _is_cand(d):
            want.append(d)
        d -= timedelta(days=1)
    missing = sorted(x for x in want if x.isoformat() not in sh_dates)

    errors: list[str] = []
    new_rows: list[dict] = []
    sse_fetched = sse_nodata = 0
    for x in missing:
        try:
            got = fetch_sse_day(x.strftime("%Y%m%d"))
        except Exception as exc:
            errors.append(f"SSE {x.isoformat()}: {exc}")
            continue
        sse_fetched += 1
        if not got:
            sse_nodata += 1
            continue
        for code, r in got.items():
            ex, fallback = FAMILY_MAP[code]
            new_rows.append({"stat_date": x, "code": code,
                             "name": r.get("name") or fallback,
                             "exchange": ex, "shares": r.get("shares"),
                             "src": "sse"})

    # 深市快照（每次拉；失败不阻断沪市部分 —— 单源失败沿用旧数据比整体失败好）
    try:
        szse = fetch_szse_snapshot()
    except Exception as exc:
        szse = {}
        errors.append(f"SZSE: {exc}")

    # —— 净值：需要「有新行 / 有已存但净值缺的行 / 深市快照值在已存净值里匹配不到」
    # 时才拉。最后一条是稳态重复跑的优化：第一次跑已把深市快照值对应的日期匹配入库，
    # 之后同一批数据的每 30 分钟重复跑直接命中存量、不再打 15 次净值接口
    #（守护线程一天要跑十几轮，不优化就是纯浪费）。
    # 窗口至少往回 12 天：深市快照的数据日常常是 T-1，必须覆盖到。
    stored_nav_by_code: dict = {}
    for r in stored:
        if r["nav"] is not None:
            stored_nav_by_code.setdefault(r["code"], {})[r["stat_date"].isoformat()] = r["nav"]
    null_rows = [(r["stat_date"], r["code"]) for r in stored if r["nav"] is None]

    need_nav = bool(new_rows or null_rows)
    if not need_nav and szse:
        need_nav = any(
            _match_nav_date(stored_nav_by_code.get(code) or {}, r.get("nav")) is None
            for code, r in szse.items())
    nav_map: dict = {}
    if need_nav:
        cand = [r["stat_date"] for r in new_rows] + [x for x, _c in null_rows]
        nav_start = min(cand) if cand else (today - timedelta(days=12))
        nav_start = min(nav_start, today - timedelta(days=12))
        for code, _ex, _nm in FAMILY:
            try:
                nav_map[code] = fetch_nav_map(
                    code, nav_start.strftime("%Y%m%d"), today.strftime("%Y%m%d"))
            except Exception as exc:
                errors.append(f"NAV {code}: {exc}")

    # —— 组装写入行（一次 execute_values 搞定三类：
    #   ① 沪市新行 ② 已存但净值缺的行补净值 ③ 深市快照行）
    up_rows = []
    for r in new_rows:
        nav = (nav_map.get(r["code"]) or {}).get(r["stat_date"].isoformat())
        up_rows.append((r["stat_date"], r["code"], r["name"], r["exchange"],
                        r["shares"], nav, r["src"]))

    nav_fills = 0
    for x, code in null_rows:
        nav = (nav_map.get(code) or {}).get(x.isoformat())
        if nav is None:
            continue
        src = next((r["src"] for r in stored
                    if r["stat_date"] == x and r["code"] == code), "") or "sse"
        nm = next((r["name"] for r in stored
                   if r["stat_date"] == x and r["code"] == code), "") or FAMILY_MAP.get(code, ("", ""))[1]
        sh = next((r["shares"] for r in stored
                   if r["stat_date"] == x and r["code"] == code), None)
        up_rows.append((x, code, nm, FAMILY_MAP.get(code, ("", ""))[0], sh, nav, src))
        nav_fills += 1

    szse_matched, szse_skipped = {}, []
    for code, r in szse.items():
        # 存量 + 新拉合并：两条路径（拉到净值 / 命中存量跳过）得到同一天的匹配，
        # 否则「同一批数据重复跑」会在两种口径间抖动。
        hist = dict(stored_nav_by_code.get(code) or {})
        hist.update(nav_map.get(code) or {})
        sd = _match_nav_date(hist, r.get("nav"))
        if sd is None:
            szse_skipped.append(code)
            continue
        szse_matched[code] = sd
        up_rows.append((date.fromisoformat(sd), code,
                        r.get("name") or FAMILY_MAP[code][1], "SZ",
                        r.get("shares"), r.get("nav"), "szse"))

    if up_rows:
        from psycopg2.extras import execute_values
        with conn.cursor() as cur:
            execute_values(cur, _UPSERT_SQL, up_rows, page_size=200)
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT max(stat_date) FROM sa_etf_flows")
        row = cur.fetchone()
    latest = row[0] if row else None
    latest_iso = latest.isoformat() if latest is not None else None
    return {
        "ok": True,
        "stat_date": latest_iso,
        "latest_is_today": latest_iso == today.isoformat(),
        "sse_fetched": sse_fetched, "sse_nodata": sse_nodata,
        "days": days,
        "new_rows": len(new_rows),
        "nav_codes": len(nav_map), "nav_filled": nav_fills,
        "szse_matched": szse_matched,
        "szse_skipped": szse_skipped,
        "upserted": len(up_rows),
        "errors": errors[:8],
    }


# ==========================================================================
# 快照（读侧算差额 —— 表里只存原始值）
# ==========================================================================

def snapshot(conn, days: int = 90) -> dict:
    """家族 15 只的最新份额/净值 + 日/5日/20日估算净申赎。"""
    today = date.today()
    try:
        rows = _stored(conn, today - timedelta(days=max(days, 40) + 30))
    except Exception as exc:
        return {"ok": False, "why": f"读取失败（表可能还没建：先点「同步」）: {exc}"}
    if not rows:
        return {"ok": False, "why": "还没有数据 —— 先点一次「同步」或等守护线程采集"}

    by_code: dict = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)   # 已按 stat_date 升序
    last = max(r["stat_date"] for r in rows)

    items, fam_series = [], {}
    for code, ex, nm in FAMILY:
        lst = by_code.get(code) or []
        item = {"code": code, "exchange": ex, "name": nm, "stat_date": None,
                "shares": None, "d_shares": None, "nav": None,
                "d_value": None, "d_value5": None}
        if lst:
            cur = lst[-1]
            item.update({"name": cur["name"] or nm, "shares": cur["shares"],
                         "nav": cur["nav"], "stat_date": cur["stat_date"].isoformat()})
            trans = []       # [(date, d_shares, d_value)]；d_value 缺净值时为 None
            for a, b in zip(lst, lst[1:]):
                if a["shares"] is None or b["shares"] is None:
                    continue
                ds = b["shares"] - a["shares"]
                dv = ds * b["nav"] if b["nav"] is not None else None
                trans.append((b["stat_date"], ds, dv))
            if trans:
                _dt, ds, dv = trans[-1]
                item["d_shares"], item["d_value"] = ds, dv
                vals5 = [t[2] for t in trans[-5:] if t[2] is not None]
                item["d_value5"] = sum(vals5) if vals5 else None
                for (_d, _ds, tdv) in trans[-20:]:
                    if tdv is None:
                        continue
                    fam_series[_d] = fam_series.get(_d, 0.0) + tdv
        items.append(item)

    def _sum_last(n: int):
        ds = sorted(fam_series)[-n:]
        return sum(fam_series[d] for d in ds) if ds else None

    have = sum(1 for i in items if i["shares"] is not None)
    at_date = sum(1 for i in items if i["stat_date"] == last.isoformat()
                  and i["d_shares"] is not None)
    sz_dates = [i["stat_date"] for i in items if i["exchange"] == "SZ" and i["stat_date"]]
    coverage = {"n": len(FAMILY), "have": have, "at_date": at_date,
                "szse_since": min(sz_dates) if sz_dates else None}

    notes = []
    if have < len(FAMILY) or at_date < len(FAMILY):
        lag = [i["code"] for i in items if i["stat_date"] != last.isoformat()]
        if lag:
            notes.append("这些代码最近数据日落后于 %s：%s（深市只有最新快照，"
                         "数据日常为 T-1；沪市未出数时会先缺）"
                         % (last.isoformat(), "、".join(lag[:8])
                            + ("…" if len(lag) > 8 else "")))
        if coverage["szse_since"]:
            notes.append("深市份额历史从 %s 起积累（深交所接口不提供历史），"
                         "头几天「近5日/近20日」不完整" % coverage["szse_since"])
    notes.append("口径：份额变化×单位净值 ≈ 净申赎估算（含套利/做市行为，"
                 "≠「国家队净买入」）；数据滞后 T+1")
    return {
        "ok": True,
        "stat_date": last.isoformat(),
        "coverage": coverage,
        "total": {
            "value": fam_series.get(last) if last in fam_series else None,
            "value5": _sum_last(5),
            "value20": _sum_last(20),
        },
        "items": items,
        "notes": notes,
    }


# ==========================================================================
# 决策注入（默认不注入；由 paper.macro_inject.enabled 控制，见 paper_trading）
# ==========================================================================

def build_injection(conn, max_chars: int = 1500) -> str:
    """给模拟盘决策的「宽基 ETF 资金流」块。无数据返回 ""（调用方会跳过）。

    与 macro_rates.build_injection 一样，**必须带数据日**：这类数据滞后 T+1，
    不写日期就是在拿昨天的钱当今天的信号。
    """
    try:
        snap = snapshot(conn, days=60)
    except Exception:
        return ""
    if not snap.get("ok"):
        return ""

    def yi(v):
        return f"{v / 1e8:+.2f} 亿" if v is not None else "—"

    items = [i for i in (snap.get("items") or []) if i.get("d_value") is not None]
    t = snap.get("total") or {}
    cov = snap.get("coverage") or {}
    lines = ["【核心宽基 ETF 资金流（国家队观察窗口；估算口径）】"]
    lines.append(
        f"· {snap.get('stat_date')} 家族 {len(FAMILY)} 只合计估算净申赎 {yi(t.get('value'))}"
        f"（近5日 {yi(t.get('value5'))}，近20日 {yi(t.get('value20'))}；"
        f"当日覆盖 {cov.get('at_date')}/{cov.get('n')} 只）")
    buys = [i for i in sorted(items, key=lambda x: x["d_value"], reverse=True)
            if i["d_value"] > 0][:3]
    sells = [i for i in sorted(items, key=lambda x: x["d_value"])
             if i["d_value"] < 0][:3]
    if buys:
        lines.append("· 净申购前三：" + "；".join(
            f"{i['name']} {yi(i['d_value'])}" for i in buys))
    if sells:
        lines.append("· 净赎回前三：" + "；".join(
            f"{i['name']} {yi(i['d_value'])}" for i in sells))
    lines.append("⚠️ 口径：份额变化×单位净值推算，含申赎套利/做市行为，"
                 "不等于「国家队净买入」；份额数据滞后 T+1，深市可能再滞后一日。"
                 "只作背景参考，不能当交易指令。")
    text = "\n".join(lines)
    cap = max(200, int(max_chars or 1500))
    if len(text) > cap:
        n = len(text)
        text = text[:cap] + f"\n[资金流块超长已截断，全文 {n} 字符]"
    return text
