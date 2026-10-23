# -*- coding: utf-8 -*-
"""国际期货盯盘：用 akshare 的 futures_foreign_commodity_realtime 获取黄金/原油等实时行情。"""

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "futures_watch_state.json"

# 关心的国际期货品种：akshare futures_foreign_commodity_realtime 支持的代码。
# 2026-10-22 逐个实测：OIL = 布伦特原油、CL = WTI 原油、HG 单位是美分/磅。
SEED_SYMBOLS = ["XAU", "XAG", "CL", "OIL", "NG", "HG"]

SEED_NAMES = {
    "XAU": "黄金",
    "XAG": "白银",
    "CL": "WTI原油",
    "OIL": "布伦特原油",
    "NG": "天然气",
    "HG": "铜",
}

# 报价单位只用于决策文本（免得 LLM 把铜的 651 当成美元/吨）。
UNITS = {
    "XAU": "美元/盎司",
    "XAG": "美元/盎司",
    "XPT": "美元/盎司",
    "XPD": "美元/盎司",
    "GC": "美元/盎司",
    "SI": "美元/盎司",
    "CL": "美元/桶",
    "OIL": "美元/桶",
    "NG": "美元/百万英热",
    "HG": "美分/磅",
    "CT": "美分/磅",
}

DEFAULT_FUTURES_CONF: dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 15,
    "alert_threshold_pct": 3,
    "alert_cooldown_hours": 6,
}

# 决策注入（由 paper.futures_fx_inject 覆盖）。默认开：用户 2026-10-22 要求
# 「主要期货加上布油 OIL，和汇率这些，并将这些数据加入决策参考中」。
INJECT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_chars": 1200,
    "stale_minutes": 45,
}


def load_futures_conf() -> dict:
    conf = dict(DEFAULT_FUTURES_CONF)
    try:
        import yaml
        p = BASE_DIR / "config.yaml"
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            got = data.get("futures") or {}
            conf.update({k: v for k, v in got.items() if v is not None})
    except Exception:
        pass
    return conf


def _num(v) -> float | None:
    """NaN/Inf 一律当缺失。PG numeric 能存 NaN，读侧做减法会直接抛异常。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ensure_tables(deps: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_futures_watch (
                id          BIGSERIAL PRIMARY KEY,
                symbol      VARCHAR(32) NOT NULL UNIQUE,
                name        VARCHAR(64) NOT NULL DEFAULT '',
                enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_futures_quotes (
                id          BIGSERIAL PRIMARY KEY,
                symbol      VARCHAR(32) NOT NULL,
                last        NUMERIC(18,6),
                change_pct  NUMERIC(10,4),
                high        NUMERIC(18,6),
                low         NUMERIC(18,6),
                vol         NUMERIC(20,2),
                ts          TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_futures_quotes "
                    "ON sa_futures_quotes (symbol, ts DESC)")
        # 补种不能只在建表那一刻做：老库里已有 XAU/CL，直接 `if fresh` 会让
        # OIL/白银/天然气/铜永远进不来。
        for sym in SEED_SYMBOLS:
            cur.execute("INSERT INTO sa_futures_watch (symbol, name) "
                        "VALUES (%s, %s) ON CONFLICT (symbol) DO NOTHING",
                        (sym, SEED_NAMES[sym]))
        # 老库里 CL 叫「原油」，与 OIL「布伦特原油」并列时读不出哪个是 WTI。
        cur.execute("UPDATE sa_futures_watch SET name = %s "
                    "WHERE symbol = 'CL' AND name = '原油'", ("WTI原油",))


def list_watch(deps: dict) -> list[dict]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, symbol, name, enabled, created_at FROM sa_futures_watch ORDER BY id")
        return [
            {"id": r[0], "symbol": r[1], "name": r[2], "enabled": r[3], "created_at": r[4]}
            for r in cur.fetchall()
        ]


def enabled_symbols(deps: dict) -> list[str]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol FROM sa_futures_watch WHERE enabled ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def fetch_quotes(symbols: list[str]) -> dict:
    """经 101 data_service 取国际期货行情，返回 {symbol: quote_dict}。

    本机不直连新浪/akshare（AGENTS.md：本机不要直接调用任何数据源）；
    服务端 `/futures` 一次批量取回，失败时**没有本地兜底** —— 有兜底就会在
    101 故障时悄悄走另一条路，口径不同反而更难发现。
    """
    if not symbols:
        return {}
    from market_data import service_get

    try:
        got = service_get("/futures", {"symbols": ",".join(symbols)}, timeout=45)
    except Exception as exc:                # noqa: BLE001
        print(f"[futures] 101 行情服务失败: {exc}", flush=True)
        return {}
    for note in (got or {}).get("notes") or []:
        print(f"[futures] 服务端提示: {note}", flush=True)

    quotes = (got or {}).get("quotes") or {}
    found: dict[str, dict] = {}
    for sym in symbols:
        q = quotes.get(sym) or {}
        last = _num(q.get("last"))
        if last is None:
            # 只有名字没有报价的行不要入库，否则界面与决策块会出现一排「—」。
            continue
        found[sym] = {
            "symbol": sym,
            "last": last,
            "change_pct": _num(q.get("change_pct")),
            "high": _num(q.get("high")),
            "low": _num(q.get("low")),
            "vol": _num(q.get("vol")),
            # ts 是**抓取时刻**（注入块头「数据抓取于」按它算滞后），
            # 报价时间在服务端返回的 q["ts"] 里，不进库。
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    return found


def _save_quotes(deps: dict, quotes: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        for q in quotes.values():
            cur.execute(
                "INSERT INTO sa_futures_quotes (symbol, last, change_pct, high, low, vol, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, now())",
                (q.get("symbol"), _num(q.get("last")), _num(q.get("change_pct")),
                 _num(q.get("high")), _num(q.get("low")), _num(q.get("vol"))),
            )
        cur.execute("DELETE FROM sa_futures_quotes WHERE ts < now() - interval '30 days'")


def run_once(deps: dict) -> dict:
    syms = enabled_symbols(deps)
    quotes = fetch_quotes(syms)
    _save_quotes(deps, quotes)
    return {"total": len(quotes), "symbols": list(quotes.keys())}


def quotes_summary(deps: dict) -> list[dict]:
    syms = enabled_symbols(deps)
    if not syms:
        return []
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT ON (q.symbol)
                 q.symbol, w.name, q.last, q.change_pct, q.high, q.low, q.vol, q.ts
             FROM sa_futures_quotes q
             LEFT JOIN sa_futures_watch w ON w.symbol = q.symbol
             WHERE q.symbol IN ({','.join(['%s']*len(syms))})
             ORDER BY q.symbol, q.ts DESC""",
            tuple(syms),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for row in rows:
            if row.get("ts") is not None:
                row["ts"] = row["ts"].isoformat()
            for k in ("last", "change_pct", "high", "low", "vol"):
                if isinstance(row.get(k), (int, float)):
                    pass
                elif row.get(k) is not None:
                    row[k] = float(row[k])
        return rows


def _row_dict(cur, row) -> dict:
    """生产里 conn.cursor() 是 tuple 游标，测试有时给 dict 游标。"""
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in (cur.description or [])]
    return dict(zip(cols, row))


def _fmt_pct(v) -> str:
    f = _num(v)
    if f is None:
        return "—"
    return f"{f:+.2f}%"


def _fmt_px(v) -> str:
    f = _num(v)
    if f is None:
        return "—"
    return f"{f:.4f}".rstrip("0").rstrip(".")


def build_injection(conn, stale_minutes: int = 45) -> str:
    """最近一轮期货报价 → 决策上下文。无数据返回 ""（上下文逐字节不变）。"""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT ON (q.symbol)
                   q.symbol, w.name, q.last, q.change_pct, q.ts
              FROM sa_futures_quotes q
              JOIN sa_futures_watch w ON w.symbol = q.symbol
             WHERE w.enabled
             ORDER BY q.symbol, q.ts DESC
        """)
        rows = [_row_dict(cur, r) for r in cur.fetchall()]
    finally:
        cur.close()
    if not rows:
        return ""

    order = {s: i for i, s in enumerate(SEED_SYMBOLS)}
    rows.sort(key=lambda r: (order.get(r.get("symbol"), 999), str(r.get("symbol"))))

    now = datetime.now(timezone.utc)
    newest = None
    lines = []
    for r in rows:
        sym = str(r.get("symbol") or "")
        name = str(r.get("name") or sym)
        unit = UNITS.get(sym, "")
        tail = f"（{unit}）" if unit else ""
        lines.append(
            f"  · {name} {sym} {_fmt_px(r.get('last'))}"
            f"（{_fmt_pct(r.get('change_pct'))}）{tail}"
        )
        ts = r.get("ts")
        if isinstance(ts, datetime):
            ts_utc = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if newest is None or ts_utc > newest:
                newest = ts_utc

    if newest is None:
        stamp, stale = "时间未知", ""
    else:
        stamp = (newest + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
        age_min = max(0, int((now - newest).total_seconds() // 60))
        stale = (f"；⚠ 数据已滞后约 {age_min} 分钟（刷新可能失败）"
                 if age_min > max(1, int(stale_minutes)) else "")

    head = (
        "【国际期货（宏观背景：大宗商品价格只用于判断成本/收入/资金面的间接传导，"
        f"不能当作个股买卖的直接触发条件）】\n数据抓取于 {stamp}（北京时间）{stale}："
    )
    return head + "\n" + "\n".join(lines)
