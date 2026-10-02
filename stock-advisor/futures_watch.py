# -*- coding: utf-8 -*-
"""国际期货盯盘：用 akshare 的 futures_foreign_commodity_realtime 获取黄金/原油等实时行情。"""

import json
import pathlib
import time
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "futures_watch_state.json"

# 关心的国际期货品种：akshare futures_foreign_commodity_realtime 支持的代码
SEED_SYMBOLS = ["XAU", "CL"]

DEFAULT_FUTURES_CONF: dict[str, Any] = {
    "enabled": True,
    "interval_minutes": 15,
    "alert_threshold_pct": 3,
    "alert_cooldown_hours": 6,
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


def _ensure_tables(deps: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('sa_futures_watch')")
        fresh = cur.fetchone()[0] is None
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
        if fresh:
            for sym, name in (
                ("XAU", "黄金"),
                ("CL", "原油"),
            ):
                cur.execute("INSERT INTO sa_futures_watch (symbol, name) "
                            "VALUES (%s, %s) ON CONFLICT (symbol) DO NOTHING", (sym, name))


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
    """用 akshare 拉取国际期货实时行情，返回 {symbol: quote_dict}。"""
    if not symbols:
        return {}
    import pandas as pd
    import akshare as ak

    found: dict[str, dict] = {}
    import pandas as pd
    import akshare as ak

    for sym in symbols:
        try:
            df = ak.futures_foreign_commodity_realtime(symbol=sym)
        except Exception:
            continue
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        row = df.iloc[0]
        found[sym] = {
            "symbol": sym,
            "last": row.get("最新价"),
            "change_pct": row.get("涨跌幅"),
            "high": row.get("最高价"),
            "low": row.get("最低价"),
            "vol": row.get("持仓量"),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    return found


def _save_quotes(deps: dict, quotes: dict) -> None:
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        for q in quotes.values():
            cur.execute(
                "INSERT INTO sa_futures_quotes (symbol, last, change_pct, high, low, vol, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, now())",
                (q.get("symbol"), q.get("last"), q.get("change_pct"), q.get("high"), q.get("low"), q.get("vol")),
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
            f"""SELECT DISTINCT ON (symbol)
                 symbol, last, change_pct, high, low, vol, ts
             FROM sa_futures_quotes
             WHERE symbol IN ({','.join(['%s']*len(syms))})
             ORDER BY symbol, ts DESC""",
            tuple(syms),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
