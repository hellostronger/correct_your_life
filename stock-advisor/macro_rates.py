"""macro_rates.py —— 中美国债收益率（宏观定价锚）。

为什么要有：美债 10Y 是全球风险资产的定价锚，A股/港股对它的敏感度很高 ——
10Y 单日跳 5~10bp 就足以带动外资流向与成长/价值风格切换。手里没有这条曲线，
盘后复盘就只能写「美股怎么样」，写不出「因为美债 10Y 上行 8bp 所以…」这种
有因果的判断。

数据源：`akshare.bond_zh_us_rate()`（同花顺族）—— 一次调用同时给中美两国
2Y/5Y/10Y/30Y 与期限利差，实测 9355 行、末行 2026-09-30（美债 2Y 4.88 /
10Y 5.29 / 10Y-2Y 0.41），零成本无需 key。

⚠️ 两条踩过的坑，写在这里免得重犯：

1. **数据是「交易日」的，不是今天的**。美债收益率按美国交易日更新，A 股开市
   的早上往往还停在昨天甚至前天的值。所以 `latest()` 一律返回 `stat_date`，
   调用方展示时必须带上这个日期，不能当「今日」用。
2. **假期/周末不更新**。入库时按 stat_date 去重覆盖（upsert），不按「今天」，
   否则连续几天会把同一个值重复插成多行。

入库日期用 stat_date，不是今天 —— 与 data_service 的 data_date 同一个道理。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "macro_rate_state.json"

# akshare 的中文列名 -> 本地表列（只用收益率，GDP 那两列不入库）
COL_MAP = {
    "美国国债收益率2年": "us2y",
    "美国国债收益率5年": "us5y",
    "美国国债收益率10年": "us10y",
    "美国国债收益率30年": "us30y",
    "美国国债收益率10年-2年": "us10_2y",
    "中国国债收益率2年": "cn2y",
    "中国国债收益率5年": "cn5y",
    "中国国债收益率10年": "cn10y",
    "中国国债收益率30年": "cn30y",
}

DDL = """
CREATE TABLE IF NOT EXISTS sa_macro_rates (
    stat_date   DATE PRIMARY KEY,
    us2y        NUMERIC(6,3), us5y     NUMERIC(6,3),
    us10y       NUMERIC(6,3), us30y    NUMERIC(6,3),
    us10_2y     NUMERIC(6,3),
    cn2y        NUMERIC(6,3), cn5y     NUMERIC(6,3),
    cn10y       NUMERIC(6,3), cn30y    NUMERIC(6,3),
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_lock = threading.Lock()


# ==========================================================================
# 取数
# ==========================================================================

def fetch_frame(days: int = 400) -> Any:
    """拉 akshare 的收益率表（DataFrame）。失败直接抛，不静默返回空。"""
    import akshare as ak
    df = ak.bond_zh_us_rate()
    if df is None or len(df) == 0:
        raise RuntimeError("bond_zh_us_rate 返回空")
    import pandas as pd
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"]).dt.date
    df = df.sort_values("日期").tail(days)
    return df


def sync(conn, days: int = 400) -> dict:
    """同步最近 days 天到 sa_macro_rates（按 stat_date upsert，幂等）。"""
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()
    df = fetch_frame(days)
    rows = []
    for _, r in df.iterrows():
        vals = [r["日期"]]
        for src in COL_MAP:
            v = r.get(src)
            try:
                v = None if v != v else float(v)      # NaN -> None
            except (TypeError, ValueError):
                v = None
            vals.append(v)
        rows.append(tuple(vals))
    cols = ["stat_date"] + list(COL_MAP.values())
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO sa_macro_rates ({','.join(cols)}) "
            f"VALUES ({','.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (stat_date) DO UPDATE SET "
            + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols[1:]) + ", "
            + "fetched_at=now()",
            rows)
    conn.commit()
    return {"inserted": len(rows), "newest": rows[-1][0].isoformat(),
            "oldest": rows[0][0].isoformat()}


# ==========================================================================
# 查询
# ==========================================================================

def latest(conn) -> dict | None:
    """最新一期（按 stat_date，不按今天 —— 见文件头 pitfall #1）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT stat_date, us2y, us5y, us10y, us30y, us10_2y, "
                    "cn2y, cn5y, cn10y, cn30y, fetched_at "
                    "FROM sa_macro_rates ORDER BY stat_date DESC LIMIT 1")
        row = cur.fetchone()
    if not row:
        return None
    keys = ["stat_date", "us2y", "us5y", "us10y", "us30y", "us10_2y",
            "cn2y", "cn5y", "cn10y", "cn30y", "fetched_at"]
    d = dict(zip(keys, row))
    d["stat_date"] = d["stat_date"].isoformat()
    d["fetched_at"] = d["fetched_at"].isoformat()
    for k in keys[1:-1]:
        d[k] = _num(d[k])
    return d


def _num(v) -> float | None:
    """Decimal/None -> float/None。psycopg2 的 NUMERIC 回来是 Decimal，
    空值是 None。**不能**用 `v == v` 判空：None == None 是 True，会走进 float(None)。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN 兜底


HIST_COLS = ["us2y", "us5y", "us10y", "us30y", "us10_2y",
             "cn2y", "cn5y", "cn10y", "cn30y"]


def history(conn, days: int = 30) -> list[dict]:
    """最近 days 期（升序，便于画曲线/算变动）。**必须返回全部期限列**：
    `snapshot()` 拿倒数第二期当基准算日变动，少查一列就等于那一列的变动永远是 None
    （第一版只查 5 列，页面上 5Y 的日变动整列空白）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT stat_date, " + ", ".join(HIST_COLS) +
                    " FROM sa_macro_rates ORDER BY stat_date DESC LIMIT %s", (days,))
        rows = cur.fetchall()
    out = [dict({"stat_date": r[0].isoformat()},
                **{k: _num(r[i + 1]) for i, k in enumerate(HIST_COLS)})
           for r in rows]
    return list(reversed(out))


def _chg(a: float | None, b: float | None) -> float | None:
    """变动（百分点）。收益率以 % 计价，0.05 = 5bp。"""
    if a is None or b is None:
        return None
    return round(a - b, 4)


def snapshot(conn, compare: int = 1) -> dict:
    """最新值 + 与前 compare 期的变动（给页面/报告用）。"""
    cur = latest(conn)
    if not cur:
        return {"ok": False, "why": "库里没有数据，先跑 sync"}
    hist = history(conn, days=compare + 1)
    prev = hist[-2] if len(hist) >= 2 else None
    chg = {}
    if prev:
        for k in ("us2y", "us5y", "us10y", "us30y", "us10_2y", "cn2y",
                  "cn5y", "cn10y", "cn30y"):
            chg[k] = _chg(cur.get(k), prev.get(k))
    return {"ok": True, "latest": cur, "prev_stat_date": prev["stat_date"] if prev else None,
            "change": chg, "history": history(conn, days=60)}


# ==========================================================================
# 异动提醒
# ==========================================================================

def check_alert(conn, notify_fn=None, threshold: float = 0.05,
                cooldown_hours: int = 12) -> list[dict]:
    """美债 10Y 日变动超阈值（默认 5bp）推微信；同向冷却，避免连推。

    为什么要冷却：非农/FOMC 当天收益率能来回跳，一晚上推 6 条等于没推。
    """
    snap = snapshot(conn, compare=1)
    if not snap.get("ok"):
        return []
    d = (snap.get("change") or {}).get("us10y")
    if d is None or abs(d) < threshold:
        return []

    state = {"sent": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    sent = state.setdefault("sent", {})

    latest_d = snap["latest"]
    lp = latest_d.get("us10y")
    lp2 = snap["latest"].get("us2y")
    curve = snap["latest"].get("us10_2y")
    direction = "上行" if d > 0 else "下行"
    # 经验判断：利率上行对成长股估值压制更直接；曲线陡峭化常伴随后续降息预期
    hint = {
        "上行": "压制成长股估值，外资流出压力上升；黄金承压",
        "下行": "利好成长股估值与黄金；通常伴随后续宽松预期",
    }[direction]
    key = f"{latest_d['stat_date']}:us10y:{'up' if d > 0 else 'down'}"
    now_ts = time.time()
    prev_ts = float(sent.get(key) or 0)
    if prev_ts and (now_ts - prev_ts) < cooldown_hours * 3600:
        return []

    bp = round(d * 100)
    content = (f"数据日 {latest_d['stat_date']}（美债按美国交易日更新）\n"
               f"10Y {lp}%　单日{direction} {abs(bp)}bp\n"
               + (f"2Y {lp2}%　期限利差 10Y-2Y {curve}%\n" if lp2 and curve else "")
               + f"\n{hint}")
    sent[key] = now_ts
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    alert = {"key": key, "title": "📉 美债收益率异动", "content": content}
    if notify_fn:
        try:
            notify_fn(alert["title"], alert["content"], event="macro_rate")
        except Exception as exc:
            print(f"[macro_rates] 通知失败: {exc}", flush=True)
    return [alert]


# ==========================================================================
# 报告素材（markdown）
# ==========================================================================

def digest(conn) -> str:
    """给 LLM 报告用的 markdown 摘要（带 stat_date 与变动，避免当成今日值）。"""
    snap = snapshot(conn, compare=5)
    if not snap.get("ok"):
        return ""
    d, chg = snap["latest"], (snap.get("change") or {})
    hist = snap.get("history") or []

    def f(v, unit=""):
        return f"{v}{unit}" if v is not None else "—"

    lines = [f"### 美债/中债收益率（数据日 {d['stat_date']}，按美国交易日更新）", ""]
    lines.append("| 期限 | 美债 | 日变动 | 中债 |")
    lines.append("|---|---|---|---|")
    for k, label in (("2y", "2Y"), ("5y", "5Y"), ("10y", "10Y"), ("30y", "30Y")):
        lines.append(f"| {label} | {f(d.get('us'+k))}% | "
                     f"{f(chg.get('us'+k) * 100 if chg.get('us'+k) is not None else None, 'bp')} | "
                     f"{f(d.get('cn'+k))}% |")
    lines.append("")
    curve = d.get("us10_2y")
    if curve is not None:
        shape = "陡峭化" if (chg.get("us10_2y") or 0) > 0 else (
            "平坦化" if (chg.get("us10_2y") or 0) < 0 else "基本走平")
        lines.append(f"- 期限利差 10Y-2Y = **{curve}%**（{shape}）")
    if hist:
        hi = max(hist, key=lambda x: x.get("us10y") or -99)
        lo = min(hist, key=lambda x: x.get("us10y") or 99)
        lines.append(f"- 近 {len(hist)} 个数据日区间："
                     f"最低 {lo.get('us10y')}%（{lo['stat_date']}）／"
                     f"最高 {hi.get('us10y')}%（{hi['stat_date']}）")
    lines.append("")
    lines.append("> 提示：美债利率上行压制成长股估值与黄金；下行则相反。"
                 "这是判断风格切换与外资流向的常用锚，引用时务必带数据日。")
    return "\n".join(lines)


if __name__ == "__main__":        # python macro_rates.py  -> 手动同步 + 打印
    import psycopg2
    from pathlib import Path as _P

    env = {}
    for line in (_P(r"D:\correct_your_life\.env").read_text(encoding="utf-8")
                 .splitlines()):
        if line.startswith("DB_") and "=" in line:
            k, v = line.strip().split("=", 1)
            env[k] = v
    c = psycopg2.connect(host=env["DB_HOST"], port=env["DB_PORT"],
                         user=env["DB_USERNAME"], password=env["DB_PASSWORD"],
                         dbname=env["DB_DATABASE"], connect_timeout=20)
    print(json.dumps(sync(c), ensure_ascii=False))
    print(digest(c))
