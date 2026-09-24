"""生成定时分析任务用的股票/持仓快照文本（数据源：云上 PostgreSQL）。

用法（Claude 定时任务触发时执行）：
    python snapshot.py
输出 JSON：{"stocks": [...含 recent_news], "holdings": [...], "news_stale": bool}
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import _query_holdings, get_conn  # noqa: E402

import psycopg2.extras  # noqa: E402


def load_watchlist():
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT code, name, note, keywords, keywords_pos, keywords_neg "
                    "FROM sa_watchlist ORDER BY added_at")
        return [dict(r) for r in cur.fetchall()]


def load_recent_news(code: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT title, source, media, publish_time, fetched_at FROM sa_news "
            "WHERE code = %s ORDER BY COALESCE(publish_time, fetched_at) DESC LIMIT %s",
            (code, limit))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for key in ("publish_time", "fetched_at"):
            r[key] = r[key].isoformat(timespec="minutes") if r[key] else None
    return rows


def main():
    stocks = load_watchlist()
    for s in stocks:
        s["recent_news"] = load_recent_news(s["code"])
    holdings = _query_holdings()
    # 提示定时任务：新闻库是否已过期（超过 2 小时未抓），过期则先触发 /api/news/fetch
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(fetched_at) FROM sa_news")
        row = cur.fetchone()
    last = row[0] if row and row[0] else None
    news_stale = (last is None or
                  datetime.now(last.tzinfo) - last > timedelta(hours=2))
    # 提示定时任务：有自定义搜索词的股票需要做竞品动态专项调研
    # （snapshot 的调用方是 Claude，keywords 直接给它当调研线索）
    comp_watch = [{k: s[k] for k in ("code", "name",
                                     "keywords", "keywords_pos", "keywords_neg")}
                  for s in stocks
                  if s.get("keywords") or s.get("keywords_pos") or s.get("keywords_neg")]
    print(json.dumps({"stocks": stocks, "holdings": holdings,
                      "news_stale": news_stale, "competitor_watch": comp_watch},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
