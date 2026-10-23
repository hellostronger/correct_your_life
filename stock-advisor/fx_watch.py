# -*- coding: utf-8 -*-
"""汇率盯盘：经 101 data_service 取新浪外汇实时报价，供页面展示与决策注入。

数据源实测（2026-10-22）：腾讯 qt.gtimg.cn 的 fx_* 符号无效；akshare 的
fx_spot_quote / fx_pair_quote 只有买卖价、没有时间戳，currency_boc_sina 停在
2023 年。新浪一次请求可拿多组 18 字段报价，且带真实日期与时分秒。

抓取位置（2026-10-22 用户要求）：**在 101 服务端做**（`GET /fx`），
本机只发一个 HTTP 请求，不直连新浪。
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# (展示代码, 新浪符号, 中文名)。顺序即页面/注入顺序。
PAIRS: list[tuple[str, str, str]] = [
    ("USD/CNY", "fx_susdcny", "美元/人民币(在岸)"),
    ("USD/CNH", "fx_susdcnh", "美元/人民币(离岸)"),
    ("USD/JPY", "fx_susdjpy", "美元/日元"),
    ("EUR/CNY", "fx_seurcny", "欧元/人民币"),
    ("USD/HKD", "fx_susdhkd", "美元/港币"),
    ("GBP/CNY", "fx_sgbpcny", "英镑/人民币"),
]

PAIR_ORDER = {p: i for i, (p, _s, _n) in enumerate(PAIRS)}

DEFAULT_FX_CONF: dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 15,
}

def load_fx_conf() -> dict:
    conf = dict(DEFAULT_FX_CONF)
    try:
        import yaml
        p = BASE_DIR / "config.yaml"
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            got = data.get("fx") or {}
            conf.update({k: v for k, v in got.items() if v is not None})
    except Exception:
        pass
    return conf


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ensure_tables(deps: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sa_fx_quotes (
                id          BIGSERIAL PRIMARY KEY,
                pair        VARCHAR(16) NOT NULL,
                name        VARCHAR(64) NOT NULL DEFAULT '',
                last        NUMERIC(18,6),
                bid         NUMERIC(18,6),
                ask         NUMERIC(18,6),
                prev_close  NUMERIC(18,6),
                change_pct  NUMERIC(10,4),
                ts          TIMESTAMPTZ NOT NULL,
                fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_fx_quotes "
                    "ON sa_fx_quotes (pair, ts DESC)")


def _parse_iso(v) -> datetime | None:
    """服务端给的是带 +08:00 的 ISO；无时区的按北京时间兜底。"""
    try:
        dt = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone(timedelta(hours=8)))


def fetch_quotes(pairs: list[tuple[str, str, str]] | None = None) -> dict:
    """经 101 data_service 取汇率，返回 {pair: quote_dict}。

    本机不直连新浪（AGENTS.md：本机不要直接调用任何数据源）。服务端 `/fx`
    一次请求取全部货币对，并带回**报价时间与时区**；失败时没有本地兜底。
    """
    pairs = pairs if pairs is not None else PAIRS
    if not pairs:
        return {}
    from market_data import service_get

    symbols = [s for _p, s, _n in pairs]
    try:
        got = service_get("/fx", {"symbols": ",".join(symbols)}, timeout=30)
    except Exception as exc:                # noqa: BLE001
        print(f"[fx] 101 行情服务失败: {exc}", flush=True)
        return {}
    for note in (got or {}).get("notes") or []:
        print(f"[fx] 服务端提示: {note}", flush=True)

    quotes = (got or {}).get("quotes") or {}
    now = datetime.now(timezone.utc)
    found: dict[str, dict] = {}
    for pair, sym, name in pairs:
        q = quotes.get(sym) or {}
        last = _num(q.get("last"))
        if last is None or last <= 0:
            print(f"[fx] 跳过 {pair}: 最新价无效 {q.get('last')!r}", flush=True)
            continue
        ts = _parse_iso(q.get("ts"))
        if ts is None:
            print(f"[fx] 跳过 {pair}: 时间不可解析 {q.get('ts')!r}", flush=True)
            continue
        if ts > now + timedelta(days=1):
            print(f"[fx] 跳过 {pair}: 报价时间在未来 {ts.isoformat()}", flush=True)
            continue
        found[pair] = {
            "pair": pair,
            "name": name,
            "last": last,
            "bid": _num(q.get("bid")),
            "ask": _num(q.get("ask")),
            "prev_close": _num(q.get("prev_close")),
            "change_pct": _num(q.get("change_pct")),
            "change": _num(q.get("change")),
            "ts": ts,
        }
    return found


def _save_quotes(deps: dict, quotes: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        for q in quotes.values():
            cur.execute(
                "INSERT INTO sa_fx_quotes (pair, name, last, bid, ask, prev_close, "
                "change_pct, ts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (q["pair"], q["name"], _num(q.get("last")), _num(q.get("bid")),
                 _num(q.get("ask")), _num(q.get("prev_close")),
                 _num(q.get("change_pct")), q["ts"]),
            )
        cur.execute("DELETE FROM sa_fx_quotes WHERE ts < now() - interval '30 days'")


def run_once(deps: dict) -> dict:
    quotes = fetch_quotes()
    _save_quotes(deps, quotes)
    return {"total": len(quotes), "pairs": list(quotes.keys())}


def quotes_summary(deps: dict) -> list[dict]:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (pair)
                   pair, name, last, bid, ask, prev_close, change_pct, ts
              FROM sa_fx_quotes
             ORDER BY pair, ts DESC
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows.sort(key=lambda r: (PAIR_ORDER.get(r.get("pair"), 999), str(r.get("pair"))))
    for row in rows:
        if row.get("ts") is not None:
            row["ts"] = row["ts"].isoformat()
        for k in ("last", "bid", "ask", "prev_close", "change_pct"):
            if row.get(k) is not None and not isinstance(row.get(k), (int, float)):
                row[k] = float(row[k])
    return rows


def _row_dict(cur, row) -> dict:
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in (cur.description or [])]
    return dict(zip(cols, row))


def _fmt_pct(v) -> str:
    f = _num(v)
    return "—" if f is None else f"{f:+.2f}%"


def _fmt_px(v) -> str:
    f = _num(v)
    if f is None:
        return "—"
    return f"{f:.4f}".rstrip("0").rstrip(".")


def build_injection(conn, stale_minutes: int = 45) -> str:
    """最近一轮汇率 → 决策上下文。无数据返回 ""（上下文逐字节不变）。"""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT ON (pair)
                   pair, name, last, change_pct, ts
              FROM sa_fx_quotes
             ORDER BY pair, ts DESC
        """)
        rows = [_row_dict(cur, r) for r in cur.fetchall()]
    finally:
        cur.close()
    if not rows:
        return ""

    rows.sort(key=lambda r: (PAIR_ORDER.get(r.get("pair"), 999), str(r.get("pair"))))

    now = datetime.now(timezone.utc)
    newest = None
    lines = []
    for r in rows:
        pair = str(r.get("pair") or "")
        name = str(r.get("name") or pair)
        lines.append(f"  · {name} {pair} {_fmt_px(r.get('last'))}"
                     f"（{_fmt_pct(r.get('change_pct'))}）")
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
        stale = (f"；⚠ 报价已滞后约 {age_min} 分钟"
                 if age_min > max(1, int(stale_minutes)) else "")

    head = (
        "【汇率（宏观背景：用于判断汇率变动对成本/收入/资金面的间接影响，"
        f"不能当作个股买卖的直接触发条件）】\n报价时间 {stamp}（北京时间）{stale}："
    )
    return head + "\n" + "\n".join(lines)
