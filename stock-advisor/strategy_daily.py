"""策略每日结论：跑策略 → 给「今天该不该动」的建议（**绝不下单**）。

和「复现策略收益」是两件事：
  - 这里只回答「按这套策略，最后一个交易日实际怎么调仓、折到你的账户是多少」；
  - 回测指标只用来判断结论可信度（strategy_gate），**不参与下单判断**；
  - 全模块没有任何成交代码路径，输出全是建议。

数据口径（三条硬约束）：
  1. 只用**最后一个交易日**的持仓变化，不预测、不补零；
  2. 权重按**实际成交**还原（当日 trades），没有成交记录时才退回持仓差；
  3. 数据到哪天就标哪天（as_of / stale_days），不把 9 月的数据说成今天的。

契约：策略源码必须先过 strategy_contract.check_contract；前置条件用
strategy_implement 的 vocabulary（always_true / above_ma / below_ma），
评估在这里做，生成在 strategy_implement 里。
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import shutil
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import jq_sandbox as JS
import strategy_contract as SC
import strategy_gate as SG
import strategy_implement as SI

BASE_DIR = Path(__file__).resolve().parent

# 与 918273645（模拟盘轮次）/ 918273646（结算）错开
DAILY_LOCK_KEY = 918273647

DEFAULT_DAILY_CONF = {
    "enabled": True,
    "min_score": 4,
    "max_strategies": 20,
    "max_codes": 1200,
    "universe_top_n": 600,
    "universe_sample_days": 12,
    "lookback_days": 730,
    "min_bars": 250,
    "cash": 1000000.0,
    "timeout": 600,
    "max_stale_days": 45,
    "run_time": "15:30",
    "interval_minutes": 1440,
}

STATUS_LABEL = {
    "no_code": "无可用源码",
    "syntax_error": "语法错误",
    "contract_failed": "不符合契约",
    "infeasible": "沙箱不可行",
    "precond_failed": "前置条件未满足",
    "precond_unknown": "前置条件无法判定",
    "no_universe": "无可回测标的",
    "insufficient_data": "行情数据不足",
    "data_stale": "数据过期",
    "run_failed": "沙箱运行失败",
    "ok": "已出结论",
}

# 这些状态算「跑失败了」；no_code / precond_* 是诚实结论，不算故障
FAIL_STATUS = ("syntax_error", "contract_failed", "infeasible", "run_failed",
               "no_universe", "insufficient_data", "data_stale")


# ---------------------------------------------------------------- 基础工具

def load_daily_conf() -> dict:
    """读 config.yaml 的 strategy_daily 段；读不到就用默认值（不抛异常）。"""
    conf = dict(DEFAULT_DAILY_CONF)
    try:
        import yaml
        p = BASE_DIR / "config.yaml"
        if p.exists():
            got = (yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            got = got.get("strategy_daily") if isinstance(got, dict) else None
            if isinstance(got, dict):
                conf.update({k: v for k, v in got.items() if v is not None})
    except Exception as exc:                     # noqa: BLE001
        print("[sd] 读 strategy_daily 配置失败，用默认值: %s" % str(exc)[:150],
              flush=True)
    return conf


def _first(row):
    """兼容 tuple 游标与 RealDictCursor —— 只取第一列的值。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values())) if row else None
    try:
        return row[0]
    except (TypeError, IndexError, KeyError):
        return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _d(v):
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v or "").strip()
    return s[:10] if s else ""


def _plain_code(c):
    """000905.XSHG / sh600519 -> 000905 / 600519（查 K 线要裸码）。"""
    s = str(c or "").strip().upper()
    for suf in (".XSHG", ".XSHE", ".SH", ".SZ"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    for pre in ("SH", "SZ", "BJ", "HK"):
        if s.startswith(pre) and s[len(pre):].isdigit():
            s = s[len(pre):]
            break
    return s


def _clean_json(v):
    """递归清洗成 PG jsonb 能接受的形状。

    Python 的 json.dumps 会原样输出 NaN/Infinity，PostgreSQL 的 jsonb 直接拒收
    （invalid input syntax for type json），而这一条会让整行写失败、整轮报废。
    """
    if isinstance(v, dict):
        return {str(k): _clean_json(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean_json(x) for x in v]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return v
    return str(v)


class _ConnTracker:
    """jq_sandbox 里用的是 `with get_conn() as conn`，psycopg2 的 with 只提交
    事务、**不关连接**。一轮 20 个策略会漏出几十条连接，这里统一回收。"""

    def __init__(self, base):
        self.base = base
        self.conns = []

    def __call__(self):
        c = self.base()
        self.conns.append(c)
        return c

    def close_all(self):
        for c in self.conns:
            try:
                c.close()
            except Exception:                    # noqa: BLE001
                pass
        self.conns = []


def _fetch(deps, sql, args=(), one=False):
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        cols = [c[0] for c in (cur.description or [])]
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass
    out = []
    for r in rows:
        out.append(dict(r) if isinstance(r, dict) else dict(zip(cols, r)))
    if one:
        return out[0] if out else None
    return out


# ---------------------------------------------------------------- 表与锁

DDL = [
    """CREATE TABLE IF NOT EXISTS sa_strategy_daily_run (
         run_id VARCHAR(40) PRIMARY KEY,
         started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         finished_at TIMESTAMPTZ,
         trade_date DATE,
         n_total SMALLINT DEFAULT 0,
         n_ok SMALLINT DEFAULT 0,
         n_failed SMALLINT DEFAULT 0,
         n_actionable SMALLINT DEFAULT 0,
         elapsed_s NUMERIC(10,1) DEFAULT 0,
         error TEXT DEFAULT '',
         note TEXT DEFAULT ''
       )""",
    """CREATE TABLE IF NOT EXISTS sa_strategy_daily (
         id BIGSERIAL PRIMARY KEY,
         run_id VARCHAR(40) NOT NULL
           REFERENCES sa_strategy_daily_run(run_id) ON DELETE CASCADE,
         trade_date DATE,
         article_id VARCHAR(64) NOT NULL,
         title VARCHAR(300) DEFAULT '',
         origin VARCHAR(8) DEFAULT 'llm',
         portable_score SMALLINT DEFAULT 0,
         status VARCHAR(24) NOT NULL,
         reason TEXT DEFAULT '',
         summary TEXT DEFAULT '',
         contract JSONB DEFAULT '{}'::jsonb,
         precond JSONB DEFAULT '{}'::jsonb,
         data_quality JSONB DEFAULT '{}'::jsonb,
         suggestions JSONB DEFAULT '[]'::jsonb,
         positions JSONB DEFAULT '[]'::jsonb,
         rejected JSONB DEFAULT '[]'::jsonb,
         metrics JSONB DEFAULT '{}'::jsonb,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (run_id, article_id)
       )""",
    """CREATE INDEX IF NOT EXISTS idx_sa_sd_article
         ON sa_strategy_daily (article_id, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_sa_sd_date
         ON sa_strategy_daily (trade_date DESC, status)""",
]


def _ensure_tables(deps):
    """只新增两张表，不动任何现有表。"""
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        for sql in DDL:
            cur.execute(sql)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass


def _advisory_lock(deps):
    """会话级 PG 咨询锁（照抄 paper_trading._settle_lock 的写法）。

    必须用 with —— 只有 __exit__ 才会 unlock + close。写成
    `if not lock.__enter__(): ...` 会把连接和锁一起泄漏，之后每轮都被自己拒掉。
    """
    conn = None
    holder = {}

    class _Lock:
        def __enter__(self):
            nonlocal conn
            try:
                conn = deps["get_conn"]()
                cur = conn.cursor()
                cur.execute("SELECT pg_try_advisory_lock(%s)", (DAILY_LOCK_KEY,))
                holder["got"] = bool(_first(cur.fetchone()))
                conn.commit()          # 结束事务但保留会话锁
            except Exception as exc:             # noqa: BLE001
                print("[sd] 取每日结论锁失败: %s" % str(exc)[:150], flush=True)
                holder["got"] = False
            return holder["got"]

        def __exit__(self, *a):
            if conn is not None:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT pg_advisory_unlock(%s)", (DAILY_LOCK_KEY,))
                    conn.commit()
                except Exception:                # noqa: BLE001
                    pass
                try:
                    conn.close()
                except Exception:                # noqa: BLE001
                    pass

    return _Lock()


# ---------------------------------------------------------------- 前置条件

def _last_closes(deps, code, n, as_of=None):
    """取截至 as_of 的最近 n 根收盘价（升序）。查不到返回 []。"""
    sql = ("SELECT close FROM sa_market_kline WHERE code = %s")
    args = [code]
    if as_of:
        sql += " AND trade_date <= %s"
        args.append(as_of)
    sql += " ORDER BY trade_date DESC LIMIT %s"
    args.append(int(n))
    rows = _fetch(deps, sql, tuple(args))
    out = [_num(_first(r)) for r in rows]
    out = [x for x in out if x is not None]
    out.reverse()
    return out


def eval_preconditions(rules, deps, as_of=None):
    """三态评估：pass / fail / unknown，组合优先级 fail > unknown > pass。

    K 线不足 n 根必须判 unknown —— **不能当 pass**（那是「没满足条件」而不是
    「条件满足」；当 pass 会让一个根本没校验过的策略给出建议）。
    """
    if not isinstance(rules, list) or not rules:
        return {"state": "pass", "items": [], "note": "无前置条件（恒真）",
                "as_of": _d(as_of)}
    items = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        t = str(r.get("type") or "")
        desc = str(r.get("desc") or "")[:120]
        if t == "always_true":
            items.append({"type": t, "desc": desc, "state": "pass",
                          "note": "恒真"})
            continue
        if t not in ("above_ma", "below_ma"):
            items.append({"type": t, "desc": desc, "state": "unknown",
                          "note": "未知规则类型"})
            continue
        code = _plain_code(r.get("code"))
        try:
            n = int(r.get("n") or 20)
        except (TypeError, ValueError):
            n = 20
        n = max(2, min(500, n))
        bars = _last_closes(deps, code, n, as_of)
        if len(bars) < n:
            items.append({"type": t, "code": code, "n": n, "state": "unknown",
                          "note": "K线不足 %d 根（实际 %d）" % (n, len(bars))})
            continue
        ma = sum(bars) / len(bars)
        last = bars[-1]
        ok = (last > ma) if t == "above_ma" else (last < ma)
        items.append({
            "type": t, "code": code, "n": n, "state": "pass" if ok else "fail",
            "close": round(last, 4), "ma": round(ma, 4),
            "note": "收盘 %.4f %s MA%d %.4f"
                    % (last, ">" if t == "above_ma" else "<", n, ma),
        })
    states = [i["state"] for i in items]
    state = "fail" if "fail" in states else (
        "unknown" if "unknown" in states else "pass")
    bad = [i["note"] for i in items if i["state"] != "pass"]
    return {"state": state, "items": items, "as_of": _d(as_of),
            "note": "" if not bad else "；".join(bad)[:300]}


def _load_preconds(deps, d):
    """前置条件来源：sa_strategy_impl（若已建表且有行）→ digest.applicable。"""
    aid = str(d.get("article_id") or "")
    row = None
    try:
        ex = _fetch(deps, "SELECT to_regclass('public.sa_strategy_impl') IS NOT NULL",
                    one=True)
        if bool(_first(ex)):
            row = _fetch(deps,
                         "SELECT preconditions FROM sa_strategy_impl "
                         "WHERE article_id = %s LIMIT 1", (aid,), one=True)
    except Exception:                            # noqa: BLE001
        row = None
    raw = (row or {}).get("preconditions") if isinstance(row, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:                        # noqa: BLE001
            raw = None
    if isinstance(raw, dict) and raw:
        return raw
    return SI.default_preconditions(d)


# ---------------------------------------------------------------- 数据窗口

def _data_window(deps, api_calls, conf):
    """最后一个可用交易日。

    用了 get_fundamentals 就必须把 end 截到估值覆盖日 —— 否则切片里没有估值，
    沙箱里 get_fundamentals 会抛「需要估值数据」，整轮白跑。
    """
    kmax = vmax = None
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute("SELECT max(trade_date) FROM sa_market_kline")
        kmax = _first(cur.fetchone())
        if "get_fundamentals" in (api_calls or []):
            cur.execute(
                "SELECT to_regclass('public.sa_stock_valuation') IS NOT NULL")
            if bool(_first(cur.fetchone())):
                cur.execute("SELECT max(trade_date) FROM sa_stock_valuation")
                vmax = _first(cur.fetchone())
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass
    end = kmax
    if vmax and end and vmax < end:
        end = vmax
    if not end:
        return {"end": None, "start": None, "stale_days": 0,
                "kline_max": kmax, "valuation_max": vmax}
    start = end - timedelta(days=int(conf["lookback_days"]))
    return {"end": end, "start": start,
            "stale_days": max(0, (date.today() - end).days),
            "kline_max": kmax, "valuation_max": vmax}


def _bars_count(cur, codes, start, end, min_bars):
    if not codes:
        return {}
    cur.execute(
        """SELECT code, count(*) FROM sa_market_kline
           WHERE code = ANY(%s) AND trade_date BETWEEN %s AND %s
           GROUP BY code HAVING count(*) >= %s""",
        (list(codes), start, end, int(min_bars)))
    return {str(_first_row_value(r, 0)): int(_first_row_value(r, 1))
            for r in cur.fetchall()}


def _first_row_value(row, idx):
    if isinstance(row, dict):
        return list(row.values())[idx]
    return row[idx]


def pick_universe(deps, code, start, end, conf, api_calls):
    """决定往沙箱里放哪些票。

    全市场策略（__ALL_MARKET__）：按估值日抽样，每天取市值最小的
    universe_top_n 只，取并集 → 过滤「窗口内日线够 min_bars」→ 按最新市值升序
    → **在 build_slice_csv 之前**截到 max_codes（沙箱 512MB，超了会被 OOM）。
    """
    refs = JS.referenced_codes(code) or []
    all_market = "__ALL_MARKET__" in refs
    explicit = [c for c in refs if c != "__ALL_MARKET__"]
    max_codes = max(1, int(conf["max_codes"]))
    min_bars = max(60, int(conf["min_bars"]))

    if not all_market:
        conn = deps["get_conn"]()
        try:
            cur = conn.cursor()
            got = _bars_count(cur, explicit, start, end, min_bars)
        finally:
            try:
                conn.close()
            except Exception:                    # noqa: BLE001
                pass
        codes = [c for c in explicit if got.get(c)]
        if not codes:
            return {"codes": [], "mode": "explicit", "truncated": False,
                    "reason": "源码里写死的 %d 个标的在 %s~%s 都没有 %d 根以上日线"
                              % (len(explicit), _d(start), _d(end), min_bars)}
        return {"codes": codes[:max_codes], "mode": "explicit",
                "truncated": len(codes) > max_codes, "reason": ""}

    top_n = max(50, int(conf["universe_top_n"]))
    sample_n = max(1, int(conf["universe_sample_days"]))
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT trade_date FROM sa_stock_valuation
               WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date""",
            (start, end))
        days = [_first_row_value(r, 0) for r in cur.fetchall()]
        days = [d for d in days if isinstance(d, (date, datetime))]
        if not days:
            if "get_fundamentals" in (api_calls or []):
                return {"codes": [], "mode": "all", "truncated": False,
                        "reason": "估值表在 %s~%s 没有数据，全市场策略无法按市值选股"
                                  % (_d(start), _d(end))}
            cur.execute(
                """SELECT DISTINCT code FROM sa_market_kline
                   WHERE trade_date BETWEEN %s AND %s ORDER BY code""",
                (start, end))
            codes = [str(_first_row_value(r, 0)) for r in cur.fetchall()]
            return {"codes": codes[:max_codes], "mode": "all",
                    "truncated": len(codes) > max_codes,
                    "reason": "估值表无数据，退化为全市场按代码取前 %d 只" % max_codes}

        if len(days) <= sample_n:
            picks = list(days)
        else:
            step = max(1, len(days) // sample_n)
            picks = days[::step][:sample_n]
            if days[-1] not in picks:
                picks[-1] = days[-1]

        pool = set()
        for d in picks:
            cur.execute(
                """SELECT code FROM sa_stock_valuation
                   WHERE trade_date = %s AND total_market_cap IS NOT NULL
                     AND total_market_cap > 0
                   ORDER BY total_market_cap ASC LIMIT %s""",
                (d, top_n))
            pool.update(str(_first_row_value(r, 0)) for r in cur.fetchall())
        if not pool:
            return {"codes": [], "mode": "all", "truncated": False,
                    "reason": "抽样估值日里没有可用的总市值数据"}

        got = _bars_count(cur, sorted(pool), start, end, min_bars)
        codes = [c for c in pool if got.get(c)]

        cap = {}
        last_day = days[-1]
        if codes:
            cur.execute(
                """SELECT code, total_market_cap FROM sa_stock_valuation
                   WHERE trade_date = %s AND code = ANY(%s)""",
                (last_day, codes))
            for r in cur.fetchall():
                cap[str(_first_row_value(r, 0))] = _num(_first_row_value(r, 1)) or 0.0
        codes.sort(key=lambda c: (cap.get(c, float("inf")), c))
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass

    if not codes:
        return {"codes": [], "mode": "all", "truncated": False,
                "reason": "抽样出的 %d 只票在窗口内都没有 %d 根以上日线"
                          % (len(pool), min_bars)}
    truncated = len(codes) > max_codes
    return {"codes": codes[:max_codes], "mode": "all", "truncated": truncated,
            "reason": ("全市场策略，按市值抽样后截到 %d 只（原始候选 %d 只）"
                       % (max_codes, len(codes))) if truncated else ""}


# ---------------------------------------------------------------- 结论推导

def _holdings_map(day):
    out = {}
    for h in (day.get("holdings") or []):
        c = str(h.get("code") or "")
        if c:
            out[c] = h
    return out


def _to_shares(code, value, price, side):
    """把建议金额折成股数。返回 (shares|None, why)。

    lot：科创板 1 股递增（买入至少 200 股），其余 100 股整手。
    港股要「每手股数」（各票不同），这里只给金额/权重，不硬猜。
    """
    if value is None or value <= 0:
        return None, "没有账户市值基准，只给权重"
    if price is None or price <= 0:
        return None, "没有成交价，只给权重"
    if len(code) == 5 and code.isdigit():
        return None, "港股每手股数各不相同，只给金额/权重"
    star = code.startswith(("688", "689"))
    raw = value / price
    if star:
        sh = int(raw)
        if side == "buy" and sh < 200:
            return 0, "科创板买入最少 200 股，按金额只折出 %d 股" % sh
        return sh, ""
    sh = int(raw / 100) * 100
    if sh < 100:
        if side == "sell":
            return 0, ("按 100 股整手折算不足 1 手（%.0f 股）；零股需要一次性"
                       "全部卖出，请自行按持仓处理" % raw)
        return 0, "按 100 股整手折算不足 1 手（%.0f 股）" % raw
    return sh, ""


def _derive(run, base_equity, names):
    """从沙箱结果推导「建议 + 目标持仓」。只用最后两个交易日的真实数据。"""
    daily = run.get("daily") or []
    if not daily:
        return [], [], {}
    last = daily[-1]
    prev = daily[-2] if len(daily) > 1 else {"holdings": [], "equity": last.get("equity")}
    lh, ph = _holdings_map(last), _holdings_map(prev)
    eq_prev = _num(prev.get("equity")) or _num(last.get("equity")) or 0.0
    eq_last = _num(last.get("equity")) or eq_prev
    day = str(last.get("date") or "")

    tmap = {}
    for t in (run.get("trades") or []):
        if str(t.get("date") or "") != day:
            continue
        tmap[(str(t.get("code") or ""), str(t.get("side") or "").lower())] = t

    base = _num(base_equity) or 0.0
    sugg = []
    for c in sorted(set(lh) | set(ph)):
        a = _num((lh.get(c) or {}).get("amount")) or 0.0
        b = _num((ph.get(c) or {}).get("amount")) or 0.0
        delta = a - b
        if abs(delta) < 1e-9:
            continue
        side = "buy" if delta > 0 else "sell"
        tr = tmap.get((c, side)) or {}
        px = (_num(tr.get("price")) or _num((lh.get(c) or {}).get("close"))
              or _num((ph.get(c) or {}).get("close")) or 0.0)
        w = (delta * px / eq_prev) if eq_prev > 0 else 0.0
        value = (w * base) if base > 0 else None
        shares, why = _to_shares(c, abs(value) if value is not None else None,
                                 px, side)
        src = "当日成交" if tr else "持仓变化推算（未找到当日成交记录）"
        reason = ("%s：%s %+d 股（策略权重 %+.2f%%），%s"
                  % (src, "买入" if side == "buy" else "卖出", int(delta),
                     w * 100, "按策略当日实际成交还原，非人工解释"))
        if why:
            reason += "；" + why
        sugg.append({
            "code": c, "name": names.get(c, "") or "",
            "side": side, "w_change": round(w, 6),
            "suggest_value": None if value is None else round(value, 2),
            "suggest_shares": shares, "price": round(px, 4) if px else None,
            "reason": reason,
        })
    sugg.sort(key=lambda x: -abs(x.get("w_change") or 0))

    pos = []
    for c, h in lh.items():
        amt = _num(h.get("amount")) or 0.0
        px = _num(h.get("close")) or 0.0
        if amt <= 0:
            continue
        pos.append({"code": c, "name": names.get(c, "") or "",
                    "shares": amt, "close": round(px, 4),
                    "weight": (round(amt * px / eq_last, 6) if eq_last > 0 else None)})
    pos.sort(key=lambda x: -(x.get("weight") or 0))

    last_reb = {"date": "", "n_changes": 0}
    for i in range(len(daily) - 1, 0, -1):
        x, y = _holdings_map(daily[i]), _holdings_map(daily[i - 1])
        n = 0
        for c in set(x) | set(y):
            aa = _num((x.get(c) or {}).get("amount")) or 0.0
            bb = _num((y.get(c) or {}).get("amount")) or 0.0
            if abs(aa - bb) > 1e-9:
                n += 1
        if n:
            last_reb = {"date": str(daily[i].get("date") or ""), "n_changes": n}
            break

    meta = {"last_rebalance": last_reb, "days": len(daily),
            "equity": round(eq_last, 2),
            "equity_prev": round(eq_prev, 2),
            "n_positions": len(pos)}
    return sugg, pos, meta


def _name_map(deps, codes):
    codes = [c for c in {str(x) for x in codes if x} if c]
    if not codes:
        return {}
    try:
        rows = _fetch(
            deps, "SELECT code, name FROM sa_stock_roster WHERE code = ANY(%s)",
            (codes,))
    except Exception:                            # noqa: BLE001
        return {}
    return {str(r.get("code")): str(r.get("name") or "") for r in rows}


def _base_equity(deps):
    try:
        row = _fetch(deps,
                     "SELECT total_value FROM sa_paper_account ORDER BY id LIMIT 1",
                     one=True)
        return _num((row or {}).get("total_value")) or 0.0
    except Exception:                            # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------- 单条策略

def _result(d, status, reason="", **kw):
    return {
        "article_id": str(d.get("article_id") or ""),
        "title": str(d.get("title_zh") or "")[:300],
        "origin": "llm",
        "portable_score": int(d.get("portable_score") or 0),
        "status": status,
        "reason": str(reason or "")[:2000],
        "summary": str(kw.get("summary") or "")[:1000],
        "contract": kw.get("contract") or {},
        "precond": kw.get("precond") or {},
        "data_quality": kw.get("data_quality") or {},
        "suggestions": kw.get("suggestions") or [],
        "positions": kw.get("positions") or [],
        "rejected": kw.get("rejected") or [],
        "metrics": kw.get("metrics") or {},
        "trade_date": kw.get("trade_date"),
    }


def _mk_summary(res_row):
    if res_row.get("status") != "ok":
        return "%s：%s" % (STATUS_LABEL.get(res_row.get("status"), res_row.get("status")),
                           str(res_row.get("reason") or "")[:160])
    dq = res_row.get("data_quality") or {}
    meta = dq.get("run_meta") or {}
    sugg = res_row.get("suggestions") or []
    nb = sum(1 for s in sugg if s.get("side") == "buy")
    ns = len(sugg) - nb
    bits = []
    if res_row.get("trade_date"):
        bits.append(str(res_row["trade_date"]))
    bits.append("持仓 %d 只" % len(res_row.get("positions") or []))
    if sugg:
        bits.append("建议 %d 条（买 %d / 卖 %d）" % (len(sugg), nb, ns))
    else:
        rb = meta.get("last_rebalance") or {}
        if rb.get("date"):
            bits.append("今日无调仓（最近一次 %s，%s 笔）"
                        % (rb["date"], rb.get("n_changes")))
        else:
            bits.append("今日无调仓")
    if dq.get("as_of"):
        bits.append("数据截至 %s（滞后 %s 天）"
                    % (dq["as_of"], dq.get("stale_days")))
    return "；".join(bits)[:1000]


def run_one(deps, d, conf):
    """跑一条策略并给出结论。任何失败都返回可读 status/reason，不抛异常。"""
    code = str(d.get("src_code") or "")
    if not code.strip():
        return _result(d, "no_code",
                       "sa_strategy_source 里没有可用源码（这篇只抽到摘要）")

    res = SC.check_contract(code)
    if not res.get("ok"):
        st = str(res.get("status") or "contract_failed")
        if st not in ("syntax_error", "contract_failed", "infeasible"):
            st = "contract_failed"
        return _result(d, st, SC.summary(res), contract=res)

    # jq_sandbox 不会自己剥离 import jqdata / import datetime（白名单内的
    # datetime 会被换成模拟时钟），必须在这里改写入参。
    code2 = SC.strip_unsupported_imports(code, res.get("strip_lines") or {})
    api_calls = list(res.get("api_calls") or [])

    try:
        win = _data_window(deps, api_calls, conf)
    except Exception as exc:                     # noqa: BLE001
        return _result(d, "insufficient_data",
                       "读数据窗口失败：%s" % str(exc)[:300], contract=res)
    if not win.get("end"):
        return _result(d, "insufficient_data",
                       "sa_market_kline 里没有任何日线数据，先补数据",
                       contract=res)

    dq = {"as_of": _d(win["end"]), "stale_days": win["stale_days"],
          "window_start": _d(win["start"]),
          "kline_max": _d(win.get("kline_max")),
          "valuation_max": _d(win.get("valuation_max"))}
    if win["stale_days"] > int(conf["max_stale_days"]):
        return _result(
            d, "data_stale",
            "数据截至 %s，已滞后 %d 天（上限 %d 天）—— 不能当作今天的结论"
            % (_d(win["end"]), win["stale_days"], int(conf["max_stale_days"])),
            contract=res, data_quality=dq)

    pre_raw = _load_preconds(deps, d)
    pre_norm, pre_errs = SI.check_preconditions(pre_raw)
    if pre_errs:
        pre_norm = SI.default_preconditions(d)
        dq["precond_errors"] = pre_errs[:5]
    ev = eval_preconditions(pre_norm.get("rules") or [], deps,
                            as_of=win["end"])
    pre_obj = {"text": pre_norm.get("precond_text") or "",
               "rules": pre_norm.get("rules") or [],
               "state": ev["state"], "items": ev["items"],
               "note": ev["note"], "as_of": ev["as_of"]}
    if ev["state"] == "fail":
        return _result(d, "precond_failed",
                       "前置条件未满足：" + (ev.get("note") or ""),
                       contract=res, precond=pre_obj, data_quality=dq)
    if ev["state"] == "unknown":
        return _result(d, "precond_unknown",
                       "前置条件无法判定：" + (ev.get("note") or ""),
                       contract=res, precond=pre_obj, data_quality=dq)

    uni = pick_universe(deps, code, win["start"], win["end"], conf, api_calls)
    dq.update({"universe_size": len(uni.get("codes") or []),
               "universe_truncated": bool(uni.get("truncated")),
               "universe_mode": uni.get("mode"),
               "universe_reason": uni.get("reason") or ""})
    codes = list(uni.get("codes") or [])
    if not codes:
        return _result(d, "no_universe",
                       uni.get("reason") or "策略没有可回测的标的",
                       contract=res, precond=pre_obj, data_quality=dq)

    tracker = _ConnTracker(deps["get_conn"])
    tdir = tempfile.mkdtemp(prefix="sa_sd_")
    try:
        k_csv = Path(tdir) / "data.csv"
        try:
            ki = JS.build_slice_csv(
                tracker, codes, _d(win["start"]), _d(win["end"]), k_csv,
                benchmark="", max_codes=int(conf["max_codes"]))
        except Exception as exc:                 # noqa: BLE001
            msg = str(exc)
            st = "insufficient_data" if "没有数据" in msg else "no_universe"
            return _result(d, st, msg[:600], contract=res, precond=pre_obj,
                           data_quality=dq)
        dq["slice"] = {"codes": len(ki.get("codes") or []),
                       "days": ki.get("days"), "rows": ki.get("rows"),
                       "bytes": ki.get("bytes")}

        val_csv = None
        if "get_fundamentals" in api_calls:
            val_csv = Path(tdir) / "val.csv"
            try:
                vi = JS.build_valuation_csv(
                    tracker, codes, _d(win["start"]), _d(win["end"]), val_csv)
                dq["valuation_days"] = vi.get("days")
                dq["valuation_rows"] = vi.get("rows")
            except Exception as exc:             # noqa: BLE001
                return _result(d, "insufficient_data",
                               "估值切片失败：%s" % str(exc)[:400],
                               contract=res, precond=pre_obj, data_quality=dq)

        try:
            run = JS.run_in_docker(
                code2, k_csv, _d(win["start"]), _d(win["end"]),
                float(conf["cash"]), timeout=int(conf["timeout"]),
                val_csv=val_csv)
        except Exception as exc:                 # noqa: BLE001
            return _result(d, "run_failed",
                           "沙箱调用失败：%s: %s" % (type(exc).__name__, str(exc)[:400]),
                           contract=res, precond=pre_obj, data_quality=dq)
    finally:
        tracker.close_all()
        shutil.rmtree(tdir, ignore_errors=True)

    if not run.get("ok"):
        why = str(run.get("error") or "沙箱没有返回结果")[:600]
        tb = str(run.get("callback_traceback") or "").strip()
        if tb:
            dq["callback_traceback"] = tb[-1500:]
        return _result(d, "run_failed", why, contract=res, precond=pre_obj,
                       data_quality=dq)

    try:
        tree = ast.parse(code2)
        n_funcs = sum(1 for n in tree.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    except SyntaxError:
        n_funcs = 0
    source = {
        "title": str(d.get("title_zh") or ""),
        "syntax_ok": True,
        "syntax_state": "ok",
        "redacted": bool(d.get("redacted")),
        "n_blocks": int(d.get("n_blocks") or 0),
        "n_lines": res.get("n_lines"),
        "n_funcs": n_funcs,
        "needs_all_market": uni.get("mode") == "all",
        "universe_size": len(ki.get("codes") or []) if uni.get("mode") == "all" else 0,
        "has_initialize": bool(res.get("has_initialize")),
        "looks_redacted": bool(d.get("redacted")),
        "risks": res.get("risks") or [],
    }
    try:
        j = SG.judge(source, run)
    except Exception as exc:                     # noqa: BLE001
        j = {"verdict": "partial", "score": 0, "flags": [],
             "blockers": ["结论闸门异常：%s" % str(exc)[:200]], "penalties": []}
    dq["gate"] = {"verdict": j.get("verdict"), "score": j.get("score"),
                  "flags": j.get("flags") or [],
                  "blockers": j.get("blockers") or [],
                  "penalties": j.get("penalties") or []}

    names = _name_map(deps, list(_holdings_map(run["daily"][-1]).keys())
                      if run.get("daily") else [])
    sugg, pos, meta = _derive(run, _base_equity(deps), names)
    if not names:
        names = _name_map(deps, [s["code"] for s in sugg] + [p["code"] for p in pos])
        for item in sugg + pos:
            item["name"] = names.get(item["code"], "") or ""
    dq["run_meta"] = meta

    rejected = []
    day = str((run.get("daily") or [{}])[-1].get("date") or "")
    for r in (run.get("rejected") or []):
        if str(r.get("date") or "") != day:
            continue
        rejected.append({"code": str(r.get("code") or ""),
                         "side": str(r.get("side") or ""),
                         "reason": str(r.get("reason") or "")[:200]})
    dq["warnings"] = (run.get("warnings") or [])[:20]
    dq["notes"] = (res.get("notes") or [])[:10]

    metrics = dict(run.get("metrics") or {})
    metrics["trade_stats"] = run.get("trade_stats") or {}
    metrics["n_trades"] = run.get("n_trades_total")
    metrics["n_rejected"] = run.get("n_rejected")
    metrics["n_days"] = run.get("n_days")
    metrics["elapsed"] = run.get("elapsed")

    out = _result(d, "ok", "", contract=res, precond=pre_obj, data_quality=dq,
                  suggestions=sugg, positions=pos, rejected=rejected,
                  metrics=metrics, trade_date=win["end"])
    out["summary"] = _mk_summary(out)
    return out


# ---------------------------------------------------------------- 落库

def _load_candidates(deps, conf, only=None):
    sql = """
        SELECT d.article_id, d.title_zh, d.summary, d.strategy_type, d.steps,
               d.universe, d.params, d.applicable, d.unsuitable, d.risk_notes,
               d.dependencies, d.portable_score, d.perf_claimed,
               s.code AS src_code, s.syntax_ok AS src_syntax_ok,
               s.syntax_state, s.redacted, s.n_blocks, s.lines AS n_lines
          FROM sa_strategy_digest d
          LEFT JOIN LATERAL (
                SELECT code, syntax_ok, syntax_state, redacted, n_blocks, lines
                  FROM sa_strategy_source x
                 WHERE x.post_id = d.article_id
                 ORDER BY length(x.code) DESC
                 LIMIT 1
          ) s ON TRUE
         WHERE d.portable_score >= %s
    """
    args = [int(conf["min_score"])]
    if only:
        sql += " AND d.article_id = ANY(%s)"
        args.append(list(only))
    sql += " ORDER BY d.portable_score DESC, d.title_zh LIMIT %s"
    args.append(int(conf["max_strategies"]))
    return _fetch(deps, sql, tuple(args))


def _start_run(deps, run_id):
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO sa_strategy_daily_run (run_id) VALUES (%s) "
                    "ON CONFLICT (run_id) DO NOTHING", (run_id,))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass


def _save_one(deps, run_id, r):
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sa_strategy_daily
                 (run_id, trade_date, article_id, title, origin, portable_score,
                  status, reason, summary, contract, precond, data_quality,
                  suggestions, positions, rejected, metrics)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,
                       %s::jsonb,%s::jsonb)
               ON CONFLICT (run_id, article_id) DO UPDATE SET
                 status = EXCLUDED.status, reason = EXCLUDED.reason,
                 summary = EXCLUDED.summary, contract = EXCLUDED.contract,
                 precond = EXCLUDED.precond, data_quality = EXCLUDED.data_quality,
                 suggestions = EXCLUDED.suggestions, positions = EXCLUDED.positions,
                 rejected = EXCLUDED.rejected, metrics = EXCLUDED.metrics,
                 trade_date = EXCLUDED.trade_date""",
            (run_id, r.get("trade_date"), r["article_id"], r["title"],
             r.get("origin") or "llm", r.get("portable_score") or 0,
             r["status"], r.get("reason") or "", r.get("summary") or "",
             json.dumps(_clean_json(r.get("contract") or {}), ensure_ascii=False),
             json.dumps(_clean_json(r.get("precond") or {}), ensure_ascii=False),
             json.dumps(_clean_json(r.get("data_quality") or {}), ensure_ascii=False),
             json.dumps(_clean_json(r.get("suggestions") or []), ensure_ascii=False),
             json.dumps(_clean_json(r.get("positions") or []), ensure_ascii=False),
             json.dumps(_clean_json(r.get("rejected") or []), ensure_ascii=False),
             json.dumps(_clean_json(r.get("metrics") or {}), ensure_ascii=False)))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass


def _finish_run(deps, run_id, trade_date, n_total, n_ok, n_failed, n_act,
                elapsed, error=""):
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE sa_strategy_daily_run
                  SET finished_at = now(), trade_date = %s, n_total = %s,
                      n_ok = %s, n_failed = %s, n_actionable = %s,
                      elapsed_s = %s, error = %s, note = ''
                WHERE run_id = %s""",
            (trade_date, n_total, n_ok, n_failed, n_act, round(elapsed, 1),
             str(error or "")[:1000], run_id))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass


def run_all(deps, conf=None, only=None):
    """跑一轮全量。PG 会话锁保证多进程只有一个在跑，单条失败不中断整轮。"""
    conf = dict(conf or load_daily_conf())
    if not conf.get("enabled", True):
        return {"ok": False, "skipped": True,
                "reason": "strategy_daily.enabled=false"}
    t0 = time.time()
    run_id = "sd-" + datetime.now().strftime("%Y%m%d%H%M%S")
    with _advisory_lock(deps) as got:
        if not got:
            return {"ok": False, "skipped": True,
                    "reason": "另一轮正在跑（PG 咨询锁 %d 被占用）" % DAILY_LOCK_KEY}
        _ensure_tables(deps)
        cand = _load_candidates(deps, conf, only)
        if not cand:
            return {"ok": False, "skipped": False, "run_id": run_id,
                    "reason": "没有候选策略（portable_score >= %s）"
                              % conf["min_score"]}
        _start_run(deps, run_id)
        rows = []
        n_ok = n_failed = n_act = 0
        err = ""
        for d in cand:
            try:
                r = run_one(deps, d, conf)
            except Exception as exc:             # noqa: BLE001
                traceback.print_exc()
                r = _result(d, "run_failed",
                            "%s: %s" % (type(exc).__name__, str(exc)[:400]))
            try:
                _save_one(deps, run_id, r)
            except Exception as exc:             # noqa: BLE001
                err = "写库失败：%s" % str(exc)[:300]
                print("[sd] " + err, flush=True)
            rows.append(r)
            if r["status"] == "ok":
                n_ok += 1
            elif r["status"] in FAIL_STATUS:
                n_failed += 1
            if r.get("suggestions"):
                n_act += 1
        tds = [r["trade_date"] for r in rows if r.get("trade_date")]
        td = max(tds) if tds else None
        elapsed = time.time() - t0
        try:
            _finish_run(deps, run_id, td, len(rows), n_ok, n_failed, n_act,
                        elapsed, err)
        except Exception as exc:                 # noqa: BLE001
            err = err or ("收尾写库失败：%s" % str(exc)[:300])
            print("[sd] " + err, flush=True)
        return {"ok": True, "run_id": run_id, "trade_date": _d(td) if td else "",
                "n_total": len(rows), "n_ok": n_ok, "n_failed": n_failed,
                "n_actionable": n_act, "elapsed_s": round(elapsed, 1),
                "error": err,
                "rows": [{"article_id": r["article_id"], "title": r["title"],
                          "status": r["status"], "reason": r["reason"][:300],
                          "summary": r["summary"],
                          "n_suggestions": len(r.get("suggestions") or []),
                          "suggestions": r.get("suggestions") or [],
                          # 回测闸门只影响「结论可信度」，**不改变 status**
                          # （见模块 docstring）。所以必须单独带出来，
                          # 否则界面/通知会把 reject 的结论当成可信结论展示。
                          "gate": (r.get("data_quality") or {}).get("gate") or {}}
                         for r in rows]}


def should_run(deps, conf=None):
    """守护线程用：到点了吗、今天跑过没有。

    去重看 `started_at::date`，**不能看 trade_date** —— 行情数据可能停在
    几天前，按 trade_date 判会每个循环都重跑一遍。
    """
    conf = conf or load_daily_conf()
    if not conf.get("enabled", True):
        return {"due": False, "reason": "未启用（strategy_daily.enabled=false）"}
    now = datetime.now()
    if now.weekday() >= 5:
        return {"due": False, "reason": "周末"}
    rt = str(conf.get("run_time") or "15:30")
    try:
        hh, mm = [int(x) for x in rt.split(":")[:2]]
    except (TypeError, ValueError):
        hh, mm = 15, 30
    if (now.hour, now.minute) < (hh, mm):
        return {"due": False, "reason": "未到运行时间 %s" % rt}
    try:
        ex = _fetch(deps, "SELECT to_regclass('public.sa_strategy_daily_run') IS NOT NULL",
                    one=True)
        if not bool(_first(ex)):
            return {"due": True, "reason": "表未建，首次运行"}
        row = _fetch(deps,
                     "SELECT run_id FROM sa_strategy_daily_run "
                     "WHERE started_at::date = current_date LIMIT 1", one=True)
    except Exception as exc:                     # noqa: BLE001
        return {"due": False, "reason": "检查失败：%s" % str(exc)[:150]}
    if row:
        return {"due": False, "reason": "今天已跑过（%s）" % row.get("run_id")}
    return {"due": True, "reason": ""}


# ---------------------------------------------------------------- 查询

_LATEST_COLS = ("id, run_id, trade_date, article_id, title, origin, "
                "portable_score, status, reason, summary, precond, "
                "data_quality, suggestions, positions, rejected, metrics, "
                "created_at")


def _rows_json(cur):
    cols = [c[0] for c in (cur.description or [])]
    out = []
    for r in cur.fetchall():
        d = dict(r) if isinstance(r, dict) else dict(zip(cols, r))
        out.append(_clean_json(d))
    return out


def load_latest(cur, limit=200):
    cur.execute("SELECT %s FROM sa_strategy_daily "
                "ORDER BY created_at DESC, id DESC LIMIT %%s" % _LATEST_COLS,
                (int(limit),))
    return _rows_json(cur)


def load_runs(cur, limit=20):
    cur.execute(
        """SELECT run_id, started_at, finished_at, trade_date, n_total,
                  n_ok, n_failed, n_actionable, elapsed_s, error, note
             FROM sa_strategy_daily_run
            ORDER BY started_at DESC LIMIT %s""", (int(limit),))
    return _rows_json(cur)


def load_universe(cur, min_score):
    """库里策略摘要总数 / 达门槛条数 —— 页面据此说明「为什么只列了 N 条」。

    只读聚合。失败不抛：这段只影响页面上的一句话说明，不该让整个面板 500。
    """
    ms = int(min_score)
    try:
        cur.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE portable_score >= %s) AS qualified
                 FROM sa_strategy_digest""", (ms,))
        row = cur.fetchone()
        if row is None:
            raise ValueError("no row")
        if isinstance(row, dict):
            total, qualified = row.get("total"), row.get("qualified")
        else:
            total, qualified = row[0], row[1]
        return {"total": int(total or 0), "qualified": int(qualified or 0),
                "min_score": ms}
    except Exception as exc:                     # noqa: BLE001
        print("[sd] 读策略总数失败：%s" % str(exc)[:200], flush=True)
        return {"total": None, "qualified": None, "min_score": ms}


# ---------------------------------------------------------------- CLI

def _main_conn():
    env = {}
    p = BASE_DIR.parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    import psycopg2
    return psycopg2.connect(
        host=env.get("DB_HOST", "101.43.25.101"),
        port=int(env.get("DB_PORT", "5432")),
        user=env.get("DB_USERNAME", "jqeval"),
        password=env.get("DB_PASSWORD", ""),
        dbname=env.get("DB_DATABASE", "dify"),
        connect_timeout=30)


def main(argv=None):
    ap = argparse.ArgumentParser(description="策略每日结论（只建议，不下单）")
    ap.add_argument("--list", action="store_true", help="只列候选，不跑")
    ap.add_argument("--run", nargs="*", default=None,
                    help="只跑这些 article_id（不给则跑全部候选）")
    ap.add_argument("--min-score", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="忽略 should_run")
    args = ap.parse_args(argv)

    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                            # noqa: BLE001
        pass

    deps = {"get_conn": _main_conn}
    conf = load_daily_conf()
    if args.min_score is not None:
        conf["min_score"] = args.min_score

    if args.list:
        cand = _load_candidates(deps, conf, args.run or None)
        print("候选 %d 条（portable_score >= %s）" % (len(cand), conf["min_score"]))
        for d in cand:
            print("  %-24s %s  score=%s src=%s"
                  % (d.get("article_id"), str(d.get("title_zh") or "")[:40],
                     d.get("portable_score"),
                     "有" if str(d.get("src_code") or "").strip() else "无"))
        return 0

    if not args.force:
        due = should_run(deps, conf)
        if not due["due"]:
            print("跳过：%s" % due["reason"])
            return 0

    r = run_all(deps, conf, args.run or None)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
