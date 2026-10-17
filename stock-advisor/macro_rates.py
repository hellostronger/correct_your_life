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
from datetime import date, timedelta
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


# ==========================================================================
# 全球国债收益率（2026-10-17 加）—— FRED 序列
# ==========================================================================
# 为什么走 FRED：akshare 的新浪全球债只覆盖「美国」与「中国」两个国别
# （bond_gb_us_sina / bond_gb_zh_sina 的 symbol 表 2026-10-17 实测全是单一
# 国家），日/德/英/韩没有免费可用的 akshare 源。FRED 是圣路易斯联储官方
# CSV 接口，无 key、直出，实测稳定：
#   https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>
#
# 实测口径（2026-10-17，别重测）：
# - DGS10 / DGS2 是日频；IRLTLT01{Jp,De,Gb,Kr}M156N 是 OECD **月频**，
#   末值都停在 2026-09-01 —— 天然滞后约 1 个月，展示/注入必须带数据日，
#   不能当「今日值」引用（文件头 pitfall #1 的同一个坑）。
# - IRLTLT01CNM156N（中国）**不存在（404）**：中国 10Y 走本项目已有的
#   sa_macro_rates.cn10y（akshare 日频），不重复引入第二个源。
# - CSV 缺失值写作 "."，必须跳过而不是编 0。
GLOBAL_SERIES = {
    "US10Y": ("DGS10", "daily", "美国10年"),
    "US2Y": ("DGS2", "daily", "美国2年"),
    "JP10Y": ("IRLTLT01JPM156N", "monthly", "日本10年（月度）"),
    "DE10Y": ("IRLTLT01DEM156N", "monthly", "德国10年（月度）"),
    "GB10Y": ("IRLTLT01GBM156N", "monthly", "英国10年（月度）"),
    "KR10Y": ("IRLTLT01KRM156N", "monthly", "韩国10年（月度）"),
}

GLOBAL_DDL = """
CREATE TABLE IF NOT EXISTS sa_macro_rates_global (
    stat_date   DATE NOT NULL,
    code        VARCHAR(12) NOT NULL,
    value       NUMERIC(8,4),
    src         VARCHAR(12) NOT NULL DEFAULT '',
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stat_date, code)
);
"""

# 决策注入默认值（paper.macro_inject）。默认关 —— 它改的是决策上下文口径，
# 按 AGENTS.md §0 必须先问；用户点头后打开即可。
INJECT_DEFAULTS = {"enabled": False, "max_chars": 1500}


def _d10(v):
    """任意日期/字符串 → 'YYYY-MM-DD'；不可解析返回 None。"""
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


def _rowvals(row, *keys):
    """一行 → 值元组；**tuple 游标与 dict 游标都支持**（按 keys 顺序取列）。

    与 institution_signals._row 同一个理由：/api 端点用默认 tuple 游标，
    `paper_trading._decide_one` 用 RealDictCursor —— 只认一种会在某条路径上
    静默变成「读取失败」。
    """
    if isinstance(row, dict):
        return tuple(row.get(k) for k in keys)
    return tuple(row[i] for i in range(len(keys)))


def fetch_fred_csv(series_id: str) -> list:
    """FRED CSV → [(date10, value)]（升序）。缺值（"."）跳过；非 200 抛异常。"""
    import requests
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": series_id}, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"FRED {series_id} HTTP {r.status_code}")
    out = []
    for line in r.text.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        d10 = _d10(parts[0])
        vs = parts[1].strip()
        if not d10 or vs in ("", "."):
            continue
        try:
            v = float(vs)
        except ValueError:
            continue
        if v != v:                       # NaN
            continue
        out.append((d10, v))
    if not out:
        raise RuntimeError(f"FRED {series_id} 解析后为空")
    return out


def sync_global(conn, days: int = 400) -> dict:
    """同步 6 条 FRED 序列到 sa_macro_rates_global（按 (stat_date, code) upsert）。"""
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in GLOBAL_DDL.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()
    cutoff = (date.today() - timedelta(days=max(30, days))).isoformat()
    all_rows, newest, errors = [], {}, {}
    for code, (sid, _freq, _label) in GLOBAL_SERIES.items():
        try:
            pts = fetch_fred_csv(sid)
        except Exception as exc:
            errors[code] = str(exc)
            continue
        pts = [(d, v) for d, v in pts if d >= cutoff]
        if not pts:
            errors[code] = "窗口内无数据"
            continue
        newest[code] = f"{pts[-1][0]} {pts[-1][1]}"
        all_rows.extend((d, code, v, "fred") for d, v in pts)
    if all_rows:
        from psycopg2.extras import execute_values
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO sa_macro_rates_global (stat_date, code, value, src) "
                "VALUES %s ON CONFLICT (stat_date, code) DO UPDATE SET "
                "value = EXCLUDED.value, src = EXCLUDED.src, fetched_at = now()",
                all_rows, page_size=500)
        conn.commit()
    return {"ok": not errors, "rows": len(all_rows), "newest": newest,
            "errors": errors}


def global_snapshot(conn) -> dict:
    """各国 10Y（含中国，从 sa_macro_rates 合并）+ 与上一期的变动 + 滞后天数。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, stat_date, value FROM ("
                "  SELECT code, stat_date, value,"
                "         row_number() OVER (PARTITION BY code "
                "                            ORDER BY stat_date DESC) AS rn"
                "  FROM sa_macro_rates_global WHERE stat_date >= %s) t "
                "WHERE rn <= 2 ORDER BY code, stat_date DESC",
                (date.today() - timedelta(days=500),))
            rows = cur.fetchall()
    except Exception as exc:
        return {"ok": False, "why": f"读取失败（表可能还没建，先同步一次）: {exc}"}
    by = {}
    for r in rows:
        code, sd, v = _rowvals(r, "code", "stat_date", "value")
        by.setdefault(code, []).append((sd, _num(v)))
    items = []
    for code in ("US10Y", "US2Y", "JP10Y", "DE10Y", "GB10Y", "KR10Y"):
        hist = by.get(code) or []
        if not hist:
            continue
        sd, v = hist[0]
        prev = hist[1][1] if len(hist) > 1 else None
        _sid, freq, label = GLOBAL_SERIES[code]
        items.append({"code": code, "label": label, "freq": freq,
                      "value": v, "stat_date": sd.isoformat(),
                      "lag_days": (date.today() - sd).days,
                      "chg": _chg(v, prev), "src": "fred"})
    # 中国 10Y：并入（sa_macro_rates 是 akshare 日频，不重复引入源）
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT stat_date, cn10y FROM sa_macro_rates "
                        "ORDER BY stat_date DESC LIMIT 2")
            crows = cur.fetchall()
        if crows:
            sd, v = _rowvals(crows[0], "stat_date", "cn10y")
            v = _num(v)
            if sd is not None and v is not None:
                prev = _num(_rowvals(crows[1], "stat_date", "cn10y")[1]) \
                    if len(crows) > 1 else None
                items.append({"code": "CN10Y", "label": "中国10年", "freq": "daily",
                              "value": v, "stat_date": sd.isoformat(),
                              "lag_days": (date.today() - sd).days,
                              "chg": _chg(v, prev), "src": "macro_rates"})
    except Exception:
        pass
    if not items:
        return {"ok": False, "why": "库里还没有全球利率数据，先同步一次"}
    order = ["US10Y", "US2Y", "CN10Y", "JP10Y", "DE10Y", "GB10Y", "KR10Y"]
    items.sort(key=lambda x: order.index(x["code"]) if x["code"] in order else 99)
    return {"ok": True, "items": items}


def build_injection(conn, max_chars: int = 1500) -> str:
    """给模拟盘决策的「全球利率」块。无数据返回 ""（调用方跳过）。

    默认不注入（paper.macro_inject.enabled 控制）。月度序列（日/德/英/韩）
    在文本里明确标「月频」：它们天然滞后约一个月，与日频值混读等于
    拿 9 月初的利率当今天的信号。
    """
    try:
        snap = global_snapshot(conn)
    except Exception:
        return ""
    if not snap.get("ok"):
        return ""
    items = snap.get("items") or []
    if not items:
        return ""

    def fmt(i):
        chg = i.get("chg")
        s = f"{i['label']} {i['value']}%（{i['stat_date']}"
        if chg is not None:
            s += f"，变动 {chg * 100:+.1f}bp"
        s += "）"
        return s

    daily = [i for i in items if i.get("freq") == "daily"]
    monthly = [i for i in items if i.get("freq") != "daily"]
    lines = ["【全球利率（各国 10Y 国债；另含美国2Y）】"]
    if daily:
        lines.append("· 日频：" + "；".join(fmt(i) for i in daily))
    if monthly:
        lines.append("· 月频（官方口径发布晚，只当背景）："
                     + "；".join(fmt(i) for i in monthly))
    us10 = next((i for i in items if i.get("code") == "US10Y"), None)
    us2 = next((i for i in items if i.get("code") == "US2Y"), None)
    if us10 and us2 and us10.get("value") is not None \
            and us2.get("value") is not None:
        lines.append(f"· 美债 10Y-2Y 利差 "
                     f"{round(us10['value'] - us2['value'], 3)}%")
    lines.append("⚠️ 利率上行压制成长股估值、下行相反；月度序列（日本/德国/"
                 "英国/韩国）更新比市场晚约 1 个月，引用必须带数据日。")
    text = "\n".join(lines)
    cap = max(200, int(max_chars or 1500))
    if len(text) > cap:
        n = len(text)
        text = text[:cap] + f"\n[利率块超长已截断，全文 {n} 字符]"
    return text


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
