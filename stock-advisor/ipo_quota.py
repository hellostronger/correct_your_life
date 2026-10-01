"""20 交易日日均市值（打新额度的正确基数）。

规则（交易所原文，见 ipo_strategy.py 头部）：
    T-2 日前 **20 个交易日日均市值** >= 1 万元，每 5000 元配 1 个申购单位。

为什么不能用「当前市值」
----------------------
`current_market_cap()` 只算 现价 × 当前持股。它在两处会给出错误结论：

1. **涨跌直接改变额度判断**：持仓上周跌 50% 今天反弹，现市值翻倍，
   但额度看的是 20 日**均值**，两者可能差一倍。
2. **补仓的时滞被完全忽略**：刚补的仓只占 20 日窗口的 1/20，
   日均市值几乎没动。现市值算法会说"补到了"，实际额度还没爬上去。

数据来源
--------
`sa_market_kline`（334 万行，含全市场日线 + turnover_rate）。
不依赖外部接口 —— 打新额度是个必须准的数字，不能让外部源抖动影响它。
缺 K 线的代码会显式记进 `missing_codes`，不静默当成 0 市值
（当成 0 会低估额度，导致「以为够其实不够」的致命误判）。

关于「非限售 A 股普通股」
------------------------
交易所口径是「按市值计算」，实际含：非限售 A 股普通股、优先股；
不含：基金、债券、ETF、存托凭证（BDR/CDR）、回购专户。
本模块只统计能识别为 A 股普通股的代码，其余进 `excluded`。
"""
from __future__ import annotations

import io
import os
from datetime import date, timedelta

# 规则常量（与 ipo_strategy 共用同一套值，避免两处漂移）
AVG_WINDOW = 20            # 日均市值窗口：20 个交易日
LOT_VALUE = 5000           # 每 5000 元市值 = 1 个申购单位
MIN_MARKET_CAP = 10000     # 门槛：20 日日均市值 >= 1 万


def _load_db_cfg() -> dict:
    """读仓库根 .env（注意不是 stock-advisor/.env）。

    路径：ipo_quota.py 在 stock-advisor/ 下，所以往上一级就是仓库根。
    """
    cfg: dict[str, str] = {}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, ".env")
    if not os.path.exists(p):
        return cfg
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def market_of(code: str) -> str:
    """按代码判市场。无法识别返回 'unknown'（会被排除，不计入市值）。"""
    c = str(code or "").strip()
    if len(c) != 6 or not c.isdigit():
        return "unknown"
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "sh"
    if c.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "sz"
    if c.startswith(("8", "4", "920")):
        return "bj"
    return "unknown"


def trading_days_before(end: date, n: int) -> list[date]:
    """从库里取截至 end 的最近 n 个交易日（降序）。取不到就退回日历推算。"""
    days = _trade_days_from_db(end, n)
    if len(days) >= n:
        return days[:n]
    return _trade_days_calendar(end, n)


def _trade_days_from_db(end: date, n: int) -> list[date]:
    """从 sa_market_kline 取最近 n 个交易日。

    该表是**实际有行情的日期**，比外部日历更可靠（有数据 = 真的交易了）。
    """
    try:
        # ::date 转型是必须的：trade_date 是 date 列，
        # psycopg2 传 Python date 能对上，但传 ISO 字符串会报 date = text。
        rows = _query(
            "SELECT DISTINCT trade_date FROM sa_market_kline "
            "WHERE trade_date <= %s::date ORDER BY trade_date DESC LIMIT %s",
            (end.isoformat(), n))
        return [r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])[:10])
                for r in rows]
    except Exception:
        return []


def _trade_days_calendar(end: date, n: int) -> list[date]:
    """兜底：用 akshare 交易日历。"""
    out: list[date] = []
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        days = sorted({str(x)[:10] for x in ak.tool_trade_date_hist_sina()["trade_date"]})
        ds = [date.fromisoformat(x) for x in days]
        ds = [x for x in ds if x <= end]
        return list(reversed(ds[-n:]))
    except Exception:
        out = []
    # 日历也不可用：按工作日粗推（宁可粗推也不要静默返回空）
    cur = end
    while len(out) < n and (end - cur).days < n * 3 + 20:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
    return out


def _query(sql: str, args: tuple = ()) -> list[tuple]:
    cfg = _load_db_cfg()
    import psycopg2
    conn = psycopg2.connect(
        host=cfg.get("DB_HOST"), port=int(cfg.get("DB_PORT", 5432)),
        user=cfg.get("DB_USERNAME"), password=cfg.get("DB_PASSWORD"),
        dbname=cfg.get("DB_DATABASE"), connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def avg_market_cap(holdings: list[dict], *, as_of: date | None = None,
                   window: int = AVG_WINDOW) -> dict:
    """算近 `window` 个交易日的日均市值（按市场分）。

    `holdings` 用 app 的 /api/holdings 形态：
        [{code, shares, avg_cost, quote:{price}}]
    只用到 `code` 和 `shares`（股数）。

    返回:
        {
          "as_of":         窗口最后一天,
          "window":        窗口天数（实际用到的）,
          "markets":       {"sh": {"avg": 日均市值, "min":..., "max":...,
                                   "lots": 日均/5000, "positions": [...]},
                            "sz": ..., "bj": ...},
          "lots":          {"sh": 日均配号数, ...},
          "coverage":      {"codes": 有K线的, "missing": 缺K线的},
          "degraded":      True 表示有代码缺 K 线（额度可能被低估）,
          "notes":         [...],
        }
    """
    as_of = as_of or date.today()
    days = trading_days_before(as_of, window)
    out = {
        "as_of": as_of.isoformat(),
        "window": len(days),
        "markets": {},
        "lots": {},
        "coverage": {"codes": [], "missing": []},
        "degraded": False,
        "notes": [],
    }
    if not days:
        out["degraded"] = True
        out["notes"].append("拿不到交易日历，无法计算日均市值")
        return out

    # 归集各市场的 (code, shares)
    per_mkt: dict[str, list[tuple[str, float]]] = {}
    excluded: list[dict] = []
    unknown_codes: list[str] = []
    for h in holdings or []:
        code = str(h.get("code") or "").strip()
        shares = h.get("shares") or h.get("net_shares") or 0
        try:
            shares = float(shares)
        except (TypeError, ValueError):
            shares = 0.0
        if shares <= 0:
            continue
        mkt = market_of(code)
        if mkt == "unknown":
            excluded.append({"code": code,
                             "reason": "非 A 股普通股/代码不识别，不计入市值",
                             "shares": shares})
            # 关键：一个**存在但我判不出市场**的 6 位代码（测试数据脏、代码规则
            # 变了、券商用了非标准码）如果静默丢掉，日均市值会低估，
            # 额度算少，而用户以为「我明明有底仓」。必须记下来让人核对。
            if len(code) == 6 and code.isdigit():
                unknown_codes.append(code)
            continue
        per_mkt.setdefault(mkt, []).append((code, shares))

    if unknown_codes:
        # 不降级（ETF/基金本就不该计入，判不出市场也可能是合法的），
        # 但要显式报出来 —— 额度有可能被低估。
        out["notes"].append(
            f"{len(unknown_codes)} 个 6 位数字代码无法判定市场，已按不计入处理"
            f"（{', '.join(unknown_codes[:6])}"
            f"{'...' if len(unknown_codes) > 6 else ''}）。"
            f"若它们其实是 A 股，额度被低估，请核对代码")

    # 一次查出所有代码在这些交易日的收盘价
    all_codes = sorted({c for v in per_mkt.values() for c, _ in v})
    closes: dict[tuple[str, str], float] = {}
    if all_codes:
        try:
            # 坑：trade_date 是 date 列，但 ANY(%s) 传 ISO 字符串数组时
            # psycopg2 推成 text，`date = text` 没有运算符，会报
            # `operator does not exist: date = text`。必须显式 ::date 转型。
            rows = _query(
                "SELECT code, trade_date, close FROM sa_market_kline "
                "WHERE code = ANY(%s::varchar[]) "
                "  AND trade_date = ANY(%s::date[])",
                (all_codes, [d.isoformat() for d in days]))
            for code, d, close in rows:
                ds = d.isoformat() if isinstance(d, date) else str(d)[:10]
                if close is not None:
                    closes[(str(code), ds)] = float(close)
        except Exception as exc:                          # noqa: BLE001
            out["degraded"] = True
            out["notes"].append(f"查 sa_market_kline 失败: {exc}")

    missing = [c for c in all_codes if not any((c, d.isoformat()) in closes
                                               for d in days)]
    out["coverage"] = {"codes": all_codes, "missing": missing,
                       "expected_days": len(days)}
    if missing:
        # 关键：不能把缺 K 线当 0 市值（会低估额度 -> 以为够其实不够）
        out["degraded"] = True
        out["notes"].append(
            f"{len(missing)} 个代码缺 K 线（{', '.join(missing[:6])}"
            f"{'...' if len(missing) > 6 else ''}），"
            f"其市值未计入，日均被低估，额度只会偏少不会偏多")

    for mkt, pos in per_mkt.items():
        daily_totals = []
        per_code_avg = {}
        for d in days:
            ds = d.isoformat()
            total = 0.0
            for code, shares in pos:
                c = closes.get((code, ds))
                if c is not None:
                    total += c * shares
            daily_totals.append(total)
        for code, shares in pos:
            vals = [closes[(code, d.isoformat())] for d in days
                    if (code, d.isoformat()) in closes]
            per_code_avg[code] = round(sum(vals) / len(vals) * shares, 2) if vals else 0.0

        avg = sum(daily_totals) / len(daily_totals) if daily_totals else 0.0
        lots = int(avg // LOT_VALUE)
        out["markets"][mkt] = {
            "avg": round(avg, 2),
            "min": round(min(daily_totals), 2) if daily_totals else 0.0,
            "max": round(max(daily_totals), 2) if daily_totals else 0.0,
            "lots": lots,
            "avg_wan": round(avg / 10000, 2),
            "positions": sorted(
                [{"code": c, "shares": s, "avg_value": per_code_avg.get(c, 0.0)}
                 for c, s in pos],
                key=lambda x: -x["avg_value"]),
            "excluded": excluded,
        }
        out["lots"][mkt] = lots

    return out


def quota_from_cap(avg: dict, mkt: str) -> dict:
    """从 avg_market_cap 的结果取某市场的额度结论。"""
    info = (avg.get("markets") or {}).get(mkt) or {}
    lots = int(info.get("lots") or 0)
    cap = float(info.get("avg") or 0.0)
    return {
        "market": mkt,
        "avg_market_cap": cap,
        "avg_wan": round(cap / 10000, 2),
        "lots": lots,
        "meets_minimum": lots >= 1,
        "window": avg.get("window"),
        "as_of": avg.get("as_of"),
        "degraded": avg.get("degraded"),
    }


def projected_avg_at(lead_trading_days: int, current_avg: float, target: float,
                     *, window: int = AVG_WINDOW) -> dict:
    """若**现在**补仓到 `target`，经过 `lead_trading_days` 个交易日后的日均市值。

    推导：窗口 `window` 天里，前 `window - n` 天固定为历史值，
    后 n 天是新仓位 target：
        new_avg = ((window - n) × current_avg + n × target) / window

    **注意 lead 必须是「交易日」不是自然日** —— 窗口只数交易日，
    拿自然日当分母会把结果高估约 40%（一周 5 个交易日 vs 7 天）。
    传自然日进来会得到偏乐观的额度，宁可保守。
    """
    n = max(0, min(int(lead_trading_days), window))
    new_avg = ((window - n) * current_avg + n * target) / window
    return {
        "avg": new_avg,
        "lots": int(new_avg // LOT_VALUE),
        "n_days_counted": n,
        "window": window,
        "clamped": int(lead_trading_days) != n,
    }


def days_needed_to_reach(target_cap: float, current_avg: float, target: float,
                         *, window: int = AVG_WINDOW) -> dict:
    """现在补仓到 `target` 元，几天后 20 日日均市值能爬到 `target_cap`？

    推导（重要，别用直觉）：
      窗口 20 天里，前 k 天的市值已经固定为「历史值」（= current_avg × 20），
      后 n 天是新仓位（target）。新日均：
        new_avg = (20 × current_avg + n × target) / 20
                = current_avg + n × (target - current_avg) / 20
      令 new_avg >= target_cap：
        n >= (target_cap - current_avg) × 20 / (target - current_avg)

    三个参数缺一不可：
      target_cap   想达到的**日均**市值（新股顶格市值门槛）
      current_avg  当前 20 日日均市值
      target       补仓后**每天**的市值（不是目标日均！）

    常见误用：把 target_cap 当 target 传进去，会算出「1 天达成」这种假结论。
    """
    import math
    if target_cap <= current_avg:
        return {"days": 0, "reachable": True,
                "note": f"当前日均 {current_avg/10000:.1f} 万已达标",
                "window": window}
    if target <= current_avg:
        return {"days": None, "reachable": False, "window": window,
                "note": (f"补仓后每天市值 {target/10000:.1f} 万不高于当前日均 "
                         f"{current_avg/10000:.1f} 万 —— 补仓只会拉低日均，"
                         f"永远达不到 {target_cap/10000:.1f} 万")}
    need = (target_cap - current_avg) * window / (target - current_avg)
    n = math.ceil(need)
    # 超过窗口长度说明再等也补不进来（低值会被完全挤出去）
    reachable = n <= window
    return {
        "days": n if reachable else None,
        "reachable": reachable,
        "window": window,
        "note": (f"补到每天 {target/10000:.0f} 万后，20 日日均爬到 "
                 f"{target_cap/10000:.0f} 万需 {n} 个交易日"
                 + ("" if reachable else
                    f"；{n} > {window} 日窗口上限，"
                    f"补仓无效（历史低值挤不出窗口）")),
    }
