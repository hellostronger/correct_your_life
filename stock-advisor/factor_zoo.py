"""已验证因子算子 → 每日决策（因子桥）。

为什么单独一层：
- 算子来自 Vibe-Trading 的 registry（上游持续更新，只通过 worker 进程调用）；
- 主应用**不 import** 上游，只认 ``tools/factor_worker.py`` 的 JSON 契约；
- 因子值不是「预测」，而是「这批票在已验证算子上的横截面暴露 + 样本内 IC 方向」。
  页面/日报必须同时给出 as_of 与「样本内」字样，不能包装成明日涨跌。

数据面：
- A 股：``sa_market_kline``（库内既有日线，COPY 成面板）；
- 港股：``sa_hk_kline``（2026-10-26 新增，经 101 ``/kline`` 增量同步 100 只
  流动性样本）。港股 volume 单位是**股**、amount 是 HKD，worker 的
  ``amount_multiplier`` 必须为 1，不能沿用 A 股的 ×100。
- 目录 ``integrations/factor_catalog.json`` 同时包含两个市场的算子。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKER = BASE_DIR / "tools" / "factor_worker.py"
CATALOG = BASE_DIR / "integrations" / "factor_catalog.json"
HK_UNIVERSE = BASE_DIR / "integrations" / "hk_universe.json"

MARKET_LABEL = {"a": "A股", "hk": "港股"}
UNIVERSE_MARKET = {"equity_cn": "a", "equity_hk": "hk"}

DEFAULT_CONF = {
    "enabled": True,
    "universe": "equity_cn",
    "pool_size": 600,
    "lookback_days": 460,
    "min_bars": 200,
    "ic_horizon": 1,
    "ic_min_days": 120,
    "ic_min_abs": 0.015,
    "ic_min_ir": 0.15,
    "z_clip": 3.0,
    "align_threshold": 0.25,
    "timeout": 900,
    "cache_minutes": 30,
    "keep_days": 90,
    "worker_python": "",
    # 港股：样本与阈值都和 A 股不同。A 股 350+ 算子时 80/40 个才算强/中；
    # 港股只有约 160 个可算、通过 IC 门槛的更少，用绝对数会把所有票都打成弱。
    # 所以港股按「有效算子 / 已算出算子」的比例定置信度（20% 强、10% 中）。
    "hk_enabled": True,
    "hk_pool_size": 100,
    "hk_lookback_days": 365,
    "hk_min_bars": 120,
    "hk_sync_minutes": 30,
    "hk_strong_ratio": 0.20,
    "hk_medium_ratio": 0.10,
}

THEME_LABEL = {
    "momentum": "动量", "reversal": "反转", "volume": "量能",
    "volatility": "波动", "quality": "质量", "value": "估值",
    "liquidity": "流动性", "microstructure": "微观结构",
    "sentiment": "情绪", "growth": "成长", "leverage": "杠杆",
}

CONF_LABEL = {"strong": "强", "medium": "中", "weak": "弱", "none": "无"}

_CACHES = {
    "equity_cn": {"key": None, "ts": 0.0, "data": None},
    "equity_hk": {"key": None, "ts": 0.0, "data": None},
}
_CACHE_LOCK = threading.Lock()
_HK_SYNC = {"ts": 0.0, "running": False, "result": None}
_HK_SYNC_LOCK = threading.Lock()


# ---------------------------------------------------------------- 配置/环境

def load_conf() -> dict:
    conf = dict(DEFAULT_CONF)
    try:
        import yaml
        p = BASE_DIR / "config.yaml"
        if p.exists():
            got = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            sec = got.get("factor") if isinstance(got, dict) else None
            if isinstance(sec, dict):
                conf.update({k: v for k, v in sec.items() if v is not None})
    except Exception as exc:                       # noqa: BLE001
        print("[factor] 读 factor 配置失败，用默认值: %s" % str(exc)[:150], flush=True)
    if os.environ.get("FACTOR_DISABLED") in ("1", "true", "yes"):
        conf["enabled"] = False
    if os.environ.get("FACTOR_WORKER_PYTHON"):
        conf["worker_python"] = os.environ["FACTOR_WORKER_PYTHON"]
    return conf


def worker_python(conf: dict | None = None) -> str:
    """找装有上游包的 Python。只认显式配置/固定候选，不猜 PATH。"""
    conf = conf or load_conf()
    candidates = [
        str(conf.get("worker_python") or "").strip(),
        os.environ.get("VIBE_PYTHON", ""),
        r"D:\vibe-trading\.venv\Scripts\python.exe",
        "/opt/vibe-trading/.venv/bin/python",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    return ""


def catalog() -> dict:
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001
        return {"alphas": [], "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def load_hk_universe(path: Path | None = None) -> list[str]:
    """港股样本代码（5 位）。文件缺失/格式坏都返回 []，由调用方报错。"""
    p = Path(path or HK_UNIVERSE)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                              # noqa: BLE001
        return []
    rows = doc.get("codes") if isinstance(doc, dict) else doc
    codes = set()
    for row in rows or []:
        raw = row.get("code") if isinstance(row, dict) else row
        c = _code(raw)
        if len(c) == 5 and c.isdigit():
            codes.add(c)
    return sorted(codes)


# ---------------------------------------------------------------- 表

DDL = [
    """CREATE TABLE IF NOT EXISTS sa_factor_run (
         run_id VARCHAR(48) PRIMARY KEY,
         as_of DATE,
         universe VARCHAR(16) NOT NULL DEFAULT 'equity_cn',
         n_codes INT DEFAULT 0,
         n_days INT DEFAULT 0,
         n_alphas INT DEFAULT 0,
         n_used INT DEFAULT 0,
         n_errors INT DEFAULT 0,
         elapsed_s NUMERIC(10,1) DEFAULT 0,
         upstream_version VARCHAR(32) DEFAULT '',
         panel JSONB DEFAULT '{}'::jsonb,
         params JSONB DEFAULT '{}'::jsonb,
         status VARCHAR(16) NOT NULL DEFAULT 'ok',
         error TEXT DEFAULT '',
         created_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
    # 老库的 sa_factor_run 是单市场版本，没有 universe；补列后才能分市场取最新。
    """ALTER TABLE sa_factor_run
         ADD COLUMN IF NOT EXISTS universe VARCHAR(16) NOT NULL DEFAULT 'equity_cn'""",
    """CREATE INDEX IF NOT EXISTS idx_sa_factor_run_universe
         ON sa_factor_run (universe, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS sa_factor_alpha (
         run_id VARCHAR(48) NOT NULL
           REFERENCES sa_factor_run(run_id) ON DELETE CASCADE,
         alpha_id VARCHAR(64) NOT NULL,
         zoo VARCHAR(24) DEFAULT '',
         nickname VARCHAR(200) DEFAULT '',
         themes JSONB DEFAULT '[]'::jsonb,
         ic_mean NUMERIC(12,6),
         ic_ir NUMERIC(12,4),
         ic_days INT DEFAULT 0,
         coverage NUMERIC(8,4) DEFAULT 0,
         error TEXT DEFAULT '',
         PRIMARY KEY (run_id, alpha_id)
       )""",
    """CREATE TABLE IF NOT EXISTS sa_factor_code (
         run_id VARCHAR(48) NOT NULL
           REFERENCES sa_factor_run(run_id) ON DELETE CASCADE,
         code VARCHAR(16) NOT NULL,
         composite NUMERIC(12,4),
         confidence VARCHAR(12) DEFAULT 'none',
         n_used INT DEFAULT 0,
         n_alphas INT DEFAULT 0,
         themes JSONB DEFAULT '{}'::jsonb,
         top JSONB DEFAULT '[]'::jsonb,
         missing TEXT DEFAULT '',
         PRIMARY KEY (run_id, code)
       )""",
    """CREATE INDEX IF NOT EXISTS idx_sa_factor_run_asof
         ON sa_factor_run (as_of DESC, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_sa_factor_code
         ON sa_factor_code (code, run_id)""",
    # 港股日线单独一张表：**绝不能混进 sa_market_kline**，否则 A 股回测/因子池
    # 会把 5 位港股代码当成 A 股标的选进来，而且 volume 单位（股 vs 手）不同。
    """CREATE TABLE IF NOT EXISTS sa_hk_kline (
         code VARCHAR(8) NOT NULL,
         trade_date DATE NOT NULL,
         open NUMERIC(14,4),
         high NUMERIC(14,4),
         low NUMERIC(14,4),
         close NUMERIC(14,4),
         volume NUMERIC(20,2),
         amount NUMERIC(20,2),
         pct NUMERIC(8,4),
         turnover_rate NUMERIC(8,4),
         source VARCHAR(8) NOT NULL DEFAULT '',
         fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         PRIMARY KEY (code, trade_date)
       )""",
    """CREATE INDEX IF NOT EXISTS idx_sa_hk_kline_date
         ON sa_hk_kline (trade_date)""",
]


def ensure_tables(deps) -> None:
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        for sql in DDL:
            cur.execute(sql)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001
            pass


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
        except Exception:                          # noqa: BLE001
            pass
    out = [dict(r) if isinstance(r, dict) else dict(zip(cols, r)) for r in rows]
    return (out[0] if out else None) if one else out


def _first(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values())) if row else None
    return row[0]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _code(c) -> str:
    s = str(c or "").strip().upper()
    for suf in (".XSHG", ".XSHE", ".SH", ".SZ", ".HK"):
        if s.endswith(suf):
            s = s[:-len(suf)]
            break
    for pre in ("SH", "SZ", "BJ", "HK"):
        if s.startswith(pre) and s[len(pre):].isdigit():
            s = s[len(pre):]
            break
    return s


def normalize_code(c) -> str:
    """统一代码口径：剥前缀/后缀，港股补齐 5 位，A 股保留 6 位。

    ``2513`` / ``HK2513`` / ``02513.HK`` 都会落成 ``02513``；非数字返回 ""。
    5 位与 6 位不看首位数字 —— 港股确实存在 ``0xxxx`` 也有 ``1xxxx``/``2xxxx``，
    用长度分流是本系统唯一判据（与 market_data.is_hk 的 5 位判断一致）。
    """
    s = _code(c)
    if not s.isdigit():
        return ""
    if len(s) == 5:
        return s
    if len(s) == 4 and s[:1] != "0":
        # 用户手打「2513」这种省前导零的港股；A 股没有 4 位代码
        return s.zfill(5)
    return s


def universe_of(code) -> str:
    """代码 -> 因子面板（``equity_cn`` / ``equity_hk``）。"""
    return "equity_hk" if len(normalize_code(code)) == 5 else "equity_cn"


def default_focus(deps) -> list[str]:
    """自选 + 持仓 + 上一轮策略建议/持仓（截断，避免把 1200 只微盘全拉进来）。"""
    codes = set()
    try:
        for r in _fetch(deps, "SELECT code FROM sa_watchlist"):
            codes.add(_code(r.get("code")))
        for r in _fetch(deps, "SELECT code FROM sa_holdings"):
            codes.add(_code(r.get("code")))
    except Exception:                              # noqa: BLE001
        pass
    try:
        rows = _fetch(
            deps,
            """SELECT suggestions, positions FROM sa_strategy_daily
                WHERE created_at > now() - interval '7 days'
                ORDER BY created_at DESC LIMIT 10""")
        n_sug = 0
        for r in rows:
            sug = r.get("suggestions") or []
            if isinstance(sug, str):
                sug = json.loads(sug or "[]")
            for s in (sug or []):
                if n_sug < 80 and s.get("code"):
                    codes.add(_code(s["code"]))
                    n_sug += 1
            pos = r.get("positions") or []
            if isinstance(pos, str):
                pos = json.loads(pos or "[]")
            for p in (pos or [])[:60]:
                if p.get("code"):
                    codes.add(_code(p["code"]))
    except Exception:                              # noqa: BLE001
        pass
    return sorted(c for c in codes if c.isdigit())


# ---------------------------------------------------------------- 港股日线

def _sync_hk(deps, conf: dict, force: bool = False,
             extra_codes: list[str] | None = None) -> dict:
    """把港股样本的日线增量同步到 ``sa_hk_kline``。

    只走 101 ``/kline``（``market_data.fetch_kline``），并发 6 路；单个代码
    失败只记 warn，不清空已有数据。同步失败时快照仍可用旧数据，页面会显示
    ``as_of`` 滞后天数。

    ``extra_codes`` 是自选/持仓/策略里的港股关注票 —— 它们可能不在
    ``hk_universe.json`` 的流动性样本里，也必须同步，否则查单票永远是「缺日线」。
    """
    if not conf.get("hk_enabled", True):
        return {"ok": False, "disabled": True, "error": "factor.hk_enabled=false"}
    universe = load_hk_universe()
    extra = sorted({_code(c) for c in (extra_codes or [])
                    if len(_code(c)) == 5 and _code(c).isdigit()} - set(universe))
    codes = sorted(set(universe) | set(extra))
    if not codes:
        return {"ok": False, "error": "港股样本为空：integrations/hk_universe.json"}
    ensure_tables(deps)

    now = time.time()
    fresh = int(conf.get("hk_sync_minutes") or 30) * 60
    with _HK_SYNC_LOCK:
        if _HK_SYNC["running"]:
            return {"ok": True, "busy": True, "skipped": True,
                    "note": "另一轮港股同步正在进行"}
        if not force and _HK_SYNC["ts"] and now - _HK_SYNC["ts"] < fresh:
            return {"ok": True, "skipped": True,
                    "note": "%.0f 分钟内已同步" % (conf.get("hk_sync_minutes") or 30)}
        _HK_SYNC["running"] = True

    _last_ok = False
    try:
        try:
            maxd = _first(_fetch(deps, "SELECT max(trade_date) FROM sa_hk_kline",
                                 one=True))
            maxd = (maxd if isinstance(maxd, date)
                    else datetime.fromisoformat(str(maxd)).date()) if maxd else None
        except Exception as exc:                   # noqa: BLE001
            return {"ok": False, "error": "读 sa_hk_kline 失败：%s" % str(exc)[:200]}

        target = date.today()
        if maxd and maxd >= target:
            _last_ok = True
            return {"ok": True, "skipped": True, "fetched": 0,
                    "last": maxd.isoformat(), "note": "已是最新"}
        start = (maxd + timedelta(days=1) if maxd
                 else target - timedelta(days=int(conf.get("hk_lookback_days") or 365)))
        try:
            import market_data
        except Exception as exc:                   # noqa: BLE001
            return {"ok": False, "error": "import market_data 失败：%s" % str(exc)[:200]}

        def _one(code: str):
            try:
                rows = market_data.fetch_kline(code, start.isoformat(),
                                               target.isoformat(), timeout=15)
                return code, rows, ""
            except Exception as exc:               # noqa: BLE001
                return code, [], "%s: %s" % (type(exc).__name__, str(exc)[:140])

        got, failed = [], []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_one, c) for c in codes]
            for fu in as_completed(futs):
                code, rows, err = fu.result()
                if err:
                    failed.append((code, err))
                got.extend(rows)
        if got:
            from psycopg2.extras import execute_values
            conn = deps["get_conn"]()
            try:
                cur = conn.cursor()
                execute_values(
                    cur,
                    """INSERT INTO sa_hk_kline
                         (code, trade_date, open, high, low, close, volume, amount,
                          pct, turnover_rate, source, fetched_at)
                       VALUES %s
                       ON CONFLICT (code, trade_date) DO UPDATE SET
                         open=EXCLUDED.open, high=EXCLUDED.high,
                         low=EXCLUDED.low, close=EXCLUDED.close,
                         volume=EXCLUDED.volume, amount=EXCLUDED.amount,
                         pct=EXCLUDED.pct, turnover_rate=EXCLUDED.turnover_rate,
                         source=EXCLUDED.source, fetched_at=EXCLUDED.fetched_at""",
                    [(r["code"], r["trade_date"], r.get("open"), r.get("high"),
                      r.get("low"), r.get("close"), r.get("volume"), r.get("amount"),
                      r.get("pct"), r.get("turnover_rate"), r.get("source", ""),
                      datetime.now()) for r in got],
                    page_size=1000)
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:                  # noqa: BLE001
                    pass
        out = {"ok": True, "codes": len(codes), "universe": len(universe),
               "extra": len(extra), "fetched": len(got),
               "failed": len(failed), "degraded": bool(failed),
               "from": start.isoformat(),
               "to": target.isoformat(),
               "last": (max(str(r["trade_date"]) for r in got)
                        if got else (maxd.isoformat() if maxd else None)),
               "elapsed_s": round(time.time() - t0, 1)}
        if failed:
            out["notes"] = ["%s %s" % (c, e) for c, e in failed[:3]]
        if got:
            print("[factor] 港股日线同步 %s 行（%s 只，失败 %s） %.1fs"
                  % (len(got), len(codes), len(failed), out["elapsed_s"]), flush=True)
        elif not maxd:
            out = {"ok": False, "error": "港股日线为空：101 /kline 全部失败",
                   "failed": len(failed), "notes": [e for _, e in failed[:3]]}
        _last_ok = bool(out.get("ok"))
        return out
    finally:
        with _HK_SYNC_LOCK:
            _HK_SYNC["running"] = False
            # 成功（含已最新/只跳过部分票）按 hk_sync_minutes 复用；失败只冷却
            # 120 秒就允许重试 —— 否则 101 抖一下会把港股快照锁死在半小时外。
            _HK_SYNC["ts"] = (time.time() if _last_ok
                              else time.time() - fresh + 120)
            _HK_SYNC["result"] = None


# ---------------------------------------------------------------- 面板

def _copy_panel(deps, path: Path, as_of: date, start: date, codes: list[str],
                table: str = "sa_market_kline") -> int:
    """把 K 线直接 COPY 成 CSV（比逐行 fetch 快 ~30%，且不在本地拼 DataFrame）。"""
    if table not in ("sa_market_kline", "sa_hk_kline"):
        raise ValueError("非法行情表 %s" % table)
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        lit = ",".join("'" + str(c).replace("'", "''") + "'" for c in codes)
        sql = (
            "COPY (SELECT code, trade_date, open, high, low, close, volume, amount "
            "FROM %s WHERE code IN (%s) "
            "AND trade_date BETWEEN '%s' AND '%s' "
            "ORDER BY code, trade_date) TO STDOUT WITH CSV HEADER"
            % (table, lit, start.isoformat(), as_of.isoformat()))
        with path.open("w", encoding="utf-8", newline="") as fh:
            cur.copy_expert(sql, fh)
        return path.stat().st_size
    finally:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001
            pass


def _build_panel(deps, conf: dict, focus: list[str],
                 universe: str = "equity_cn") -> dict:
    hk = universe == "equity_hk"
    table = "sa_hk_kline" if hk else "sa_market_kline"
    as_of = _first(_fetch(deps, "SELECT max(trade_date) FROM %s" % table, one=True))
    if not as_of:
        raise RuntimeError("%s 没有数据" % table)
    as_of = as_of if isinstance(as_of, date) else datetime.fromisoformat(str(as_of)).date()
    lookback = int(conf.get("hk_lookback_days" if hk else "lookback_days")
                   or conf["lookback_days"])
    start = as_of - timedelta(days=lookback)
    pool = int(conf.get("hk_pool_size" if hk else "pool_size") or 100)
    min_bars = int(conf.get("hk_min_bars" if hk else "min_bars") or 120)
    rows = _fetch(
        deps,
        """SELECT code FROM %s
            WHERE trade_date BETWEEN %%s AND %%s AND volume IS NOT NULL AND volume > 0
            GROUP BY code HAVING count(*) >= %%s
            ORDER BY avg(COALESCE(amount, close * volume)) DESC NULLS LAST LIMIT %%s"""
        % table,
        (start, as_of, min_bars, pool))
    want = 5 if hk else 6
    codes = {_code(r.get("code")) for r in rows}
    codes.update(c for c in focus if c and len(c) == want)
    codes = sorted(c for c in codes if c.isdigit() and len(c) == want)

    tdir = Path(tempfile.mkdtemp(prefix="sa_factor_"))
    panel_csv = tdir / "panel.csv"
    nbytes = _copy_panel(deps, panel_csv, as_of, start, codes, table=table)
    return {"as_of": as_of, "start": start, "codes": codes,
            "panel_csv": panel_csv, "tmpdir": tdir, "bytes": nbytes,
            "table": table, "universe": universe}


# ---------------------------------------------------------------- 运行

def _validate(res: dict) -> None:
    for k in ("ok", "as_of", "panel", "alphas", "codes"):
        if k not in res:
            raise ValueError("worker 输出缺字段 %s" % k)
    if res.get("ok") is not True:
        raise ValueError(str(res.get("error") or "worker 返回 ok=false")[:500])
    if not isinstance(res.get("codes"), dict) or not isinstance(res.get("alphas"), list):
        raise ValueError("worker 输出类型不对")


def _run_worker(conf: dict, panel: dict, focus: list[str],
                universe: str = "equity_cn") -> dict:
    py = worker_python(conf)
    if not py:
        raise RuntimeError(
            "找不到装上游包的 Python：在 config.yaml factor.worker_python 或 "
            "环境变量 FACTOR_WORKER_PYTHON 指定（如 D:\\vibe-trading\\.venv\\Scripts\\python.exe）")
    job = {
        "panel": str(panel["panel_csv"]),
        "out": str(panel["tmpdir"] / "out.json"),
        "universe": universe,
        "as_of": panel["as_of"].isoformat(),
        "focus_codes": sorted(set(focus)),
        # A 股 volume=手 → 股；港股 volume=股，且 amount 已是 HKD，不能再乘 100。
        "amount_multiplier": 100 if universe == "equity_cn" else 1,
        "ic_horizon": int(conf["ic_horizon"]),
        "ic_min_days": int(conf["ic_min_days"]),
        "ic_min_abs": float(conf["ic_min_abs"]),
        "ic_min_ir": float(conf["ic_min_ir"]),
        "z_clip": float(conf["z_clip"]),
        "strong_ratio": (float(conf.get("hk_strong_ratio") or 0)
                         if universe == "equity_hk" else 0),
        "medium_ratio": (float(conf.get("hk_medium_ratio") or 0)
                         if universe == "equity_hk" else 0),
    }
    job_path = panel["tmpdir"] / "job.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("NO_PROXY", "*")
    env.setdefault("no_proxy", "*")
    t0 = time.time()
    proc = subprocess.run(
        [py, str(WORKER), str(job_path)],
        cwd=str(panel["tmpdir"]), env=env, timeout=int(conf["timeout"]),
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = round(time.time() - t0, 1)
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
        raise RuntimeError("factor worker 退出码 %s（%.0fs）：%s"
                           % (proc.returncode, elapsed, tail))
    out = Path(job["out"])
    if not out.exists():
        raise RuntimeError("factor worker 没有写输出文件")
    res = json.loads(out.read_text(encoding="utf-8"))
    _validate(res)
    res["_wall_s"] = elapsed
    return res


def _save(deps, res: dict, focus: list[str], conf: dict,
          universe: str = "equity_cn") -> str:
    prefix = "fxh-" if universe == "equity_hk" else "fx-"
    run_id = prefix + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + os.urandom(2).hex()
    panel = res.get("panel") or {}
    alphas = res.get("alphas") or []
    codes = res.get("codes") or {}
    n_used = sum(1 for a in alphas if a.get("ic_mean") is not None)
    conn = deps["get_conn"]()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sa_factor_run
                 (run_id, as_of, universe, n_codes, n_days, n_alphas, n_used,
                  n_errors, elapsed_s, upstream_version, panel, params, status, error)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,'ok','')
               ON CONFLICT (run_id) DO NOTHING""",
            (run_id, res.get("as_of"), res.get("universe") or "equity_cn",
             int(panel.get("codes") or 0), int(panel.get("days") or 0),
             len(alphas), n_used, len(res.get("errors") or []),
             _num(res.get("elapsed_s") or res.get("_wall_s")) or 0,
             str(res.get("upstream_version") or "")[:32],
             json.dumps(panel, ensure_ascii=False),
             json.dumps({**(res.get("params") or {}), "focus": len(focus),
                         "wall_s": res.get("_wall_s")}, ensure_ascii=False)))
        cur.executemany(
            """INSERT INTO sa_factor_alpha
                 (run_id, alpha_id, zoo, nickname, themes, ic_mean, ic_ir,
                  ic_days, coverage, error)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id, alpha_id) DO NOTHING""",
            [(run_id, a.get("id"), str(a.get("zoo") or "")[:24],
              str(a.get("nickname") or "")[:200],
              json.dumps(a.get("themes") or [], ensure_ascii=False),
              _num(a.get("ic_mean")), _num(a.get("ic_ir")),
              int(a.get("ic_days") or 0), _num(a.get("coverage")) or 0,
              str(a.get("error") or "")[:500]) for a in alphas])
        cur.executemany(
            """INSERT INTO sa_factor_code
                 (run_id, code, composite, confidence, n_used, n_alphas,
                  themes, top, missing)
               VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
               ON CONFLICT (run_id, code) DO NOTHING""",
            [(run_id, _code(code), _num(v.get("composite")),
              str(v.get("confidence") or "none")[:12],
              int(v.get("n_used") or 0), int(v.get("n_alphas") or 0),
              json.dumps(v.get("theme_scores") or {}, ensure_ascii=False),
              json.dumps(v.get("top") or [], ensure_ascii=False),
              str(v.get("missing") or "")[:200]) for code, v in codes.items()])
        # 只保留最近 keep_days 天的快照；策略/报告每天都在跑，不能无限长
        cur.execute("DELETE FROM sa_factor_run WHERE created_at < now() - (%s || ' days')::interval",
                    (int(conf.get("keep_days") or 90),))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:                          # noqa: BLE001
            pass
    return run_id


def _snapshot_market(deps, universe: str, focus_codes=None, force: bool = False,
                     conf: dict | None = None) -> dict:
    """单市场快照。失败返回 {ok:false,error}，**绝不抛给调用方**。"""
    conf = conf or load_conf()
    hk = universe == "equity_hk"
    market = "hk" if hk else "a"
    if hk:
        if not conf.get("hk_enabled", True):
            return {"ok": False, "disabled": True, "universe": universe,
                    "market": market, "error": "factor.hk_enabled=false"}
    elif not conf.get("enabled", True):
        return {"ok": False, "disabled": True, "universe": universe,
                "market": market, "error": "factor.enabled=false"}

    want = 5 if hk else 6
    focus = sorted({_code(c) for c in (focus_codes or default_focus(deps))
                    if c and len(_code(c)) == want})
    key = (universe,
           int(conf.get("hk_pool_size" if hk else "pool_size") or 0),
           int(conf.get("hk_lookback_days" if hk else "lookback_days") or 0),
           int(conf["ic_min_days"]), float(conf["ic_min_abs"]), tuple(focus))
    cache = _CACHES[universe]
    with _CACHE_LOCK:
        cached = cache["data"]
        if (not force and cached and cached.get("ok") and cache["key"] == key
                and time.time() - cache["ts"] < int(conf["cache_minutes"]) * 60):
            return cached
    if not WORKER.exists():
        return {"ok": False, "universe": universe, "market": market,
                "error": "缺少 tools/factor_worker.py"}

    sync = None
    if hk:
        try:
            sync = _sync_hk(deps, conf, force=force, extra_codes=focus)
        except Exception as exc:                   # noqa: BLE001
            sync = {"ok": False, "error": "%s: %s" % (type(exc).__name__,
                                                      str(exc)[:300])}
            print("[factor] 港股同步异常（继续用库内旧数据）：%s"
                  % str(exc)[:300], flush=True)
    try:
        ensure_tables(deps)
        panel = _build_panel(deps, conf, focus, universe=universe)
        try:
            res = _run_worker(conf, panel, focus, universe=universe)
        finally:
            shutil.rmtree(panel["tmpdir"], ignore_errors=True)
        res["universe"] = universe
        res["market"] = market
        res["market_label"] = MARKET_LABEL[market]
        res["n_codes"] = len(res.get("codes") or {})
        res["n_alphas"] = len(res.get("alphas") or [])
        res["n_used"] = sum(1 for a in res.get("alphas") or []
                            if a.get("ic_mean") is not None)
        res["run_id"] = _save(deps, res, focus, conf, universe=universe)
        res["focus"] = focus
        res["ok"] = True
        if sync is not None:
            res["sync"] = sync
        with _CACHE_LOCK:
            cache.update({"key": key, "ts": time.time(), "data": res})
        print("[factor] %s %s as_of=%s codes=%s alphas=%s used=%s %.1fs"
              % (res["run_id"], MARKET_LABEL[market], res.get("as_of"),
                 len(res.get("codes") or {}), len(res.get("alphas") or []),
                 sum(1 for a in res.get("alphas") or [] if a.get("ic_mean") is not None),
                 res.get("_wall_s") or 0), flush=True)
        return res
    except Exception as exc:                       # noqa: BLE001
        print("[factor] %s 计算失败：%s" % (universe, str(exc)[:600]), flush=True)
        return {"ok": False, "universe": universe, "market": market,
                "market_label": MARKET_LABEL[market],
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:500])}


def snapshot(deps, focus_codes=None, force: bool = False, conf: dict | None = None) -> dict:
    """A 股单市场快照（向后兼容；新调用方请用 ``snapshot_all``）。"""
    return _snapshot_market(deps, "equity_cn", focus_codes=focus_codes,
                            force=force, conf=conf)


def snapshot_hk(deps, focus_codes=None, force: bool = False,
                conf: dict | None = None) -> dict:
    """港股单市场快照（只覆盖 integrations/hk_universe.json 的样本 + 关注票）。"""
    return _snapshot_market(deps, "equity_hk", focus_codes=focus_codes,
                            force=force, conf=conf)


def snapshot_all(deps, focus_codes=None, force: bool = False,
                 conf: dict | None = None) -> dict:
    """A 股 + 港股各算一轮，合成一个给策略/日报/模拟盘用的对象。

    ``markets`` 保留各自的面板/算子口径（不能把 A 股与港股的 z 混在一起），
    ``codes`` 是合并后的单票索引，每项带 ``market`` / ``as_of``。
    任一市场失败都不影响另一个；两个都失败才返回 ``ok:false``。
    """
    conf = conf or load_conf()
    focus = sorted({_code(c) for c in (focus_codes or default_focus(deps)) if c})
    a_focus = [c for c in focus if len(c) == 6]
    hk_focus = [c for c in focus if len(c) == 5]
    jobs = {"a": ("equity_cn", a_focus), "hk": ("equity_hk", hk_focus)}
    got: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {m: ex.submit(_snapshot_market, deps, u, f, force, conf)
                for m, (u, f) in jobs.items()}
        for m, fu in futs.items():
            try:
                got[m] = fu.result()
            except Exception as exc:               # noqa: BLE001
                got[m] = {"ok": False, "market": m,
                          "market_label": MARKET_LABEL[m],
                          "error": "%s: %s" % (type(exc).__name__, str(exc)[:400])}

    markets = {}
    errors = []
    codes = {}
    runs = []
    for m, snap in got.items():
        if snap.get("ok"):
            markets[m] = snap
            runs.append({k: snap.get(k) for k in (
                "market", "market_label", "run_id", "as_of", "universe",
                "panel", "n_alphas", "n_used", "elapsed_s", "sync")})
            for code, v in (snap.get("codes") or {}).items():
                codes[code] = {**v, "market": m,
                               "market_label": MARKET_LABEL[m],
                               "universe": snap.get("universe"),
                               "as_of": snap.get("as_of")}
        elif not snap.get("disabled"):
            errors.append({"market": m, "error": str(snap.get("error") or "")[:500]})
    ok = bool(markets)
    first = markets.get("a") or markets.get("hk") or {}
    as_ofs = [s.get("as_of") for s in markets.values() if s.get("as_of")]
    out = {
        "ok": ok,
        "as_of": max(as_ofs) if as_ofs else None,
        "markets": markets,
        "codes": codes,
        "runs": runs,
        "errors": errors,
        "focus": focus,
        "universe": "cn_hk" if len(markets) == 2 else first.get("universe"),
        "panel": first.get("panel") or {},
        "alphas": first.get("alphas") or [],
        "params": first.get("params") or {},
        "run_id": first.get("run_id"),
        "n_codes": sum(len((s.get("codes") or {})) for s in markets.values()),
        "n_alphas": sum(int(s.get("n_alphas") or 0) for s in markets.values()),
        "n_used": sum(int(s.get("n_used") or 0) for s in markets.values()),
    }
    if not ok:
        out["error"] = "；".join("%s: %s" % (MARKET_LABEL.get(e["market"], e["market"]),
                                            e["error"]) for e in errors)[:800]
    return out


# ---------------------------------------------------------------- 决策接入

def _theme_items(theme_scores: dict, limit: int = 4) -> list[dict]:
    items = sorted((theme_scores or {}).items(),
                   key=lambda kv: -abs(_num((kv[1] or {}).get("z")) or 0))
    out = []
    for name, v in items[:limit]:
        z = _num((v or {}).get("z"))
        if z is None or abs(z) < 0.3:
            continue
        out.append({"theme": name, "label": THEME_LABEL.get(name, name),
                    "z": round(z, 2), "n": int((v or {}).get("n") or 0)})
    return out


def attach_to_suggestions(snap: dict, suggestions: list, positions: list | None = None,
                          threshold: float = 0.25) -> dict:
    """把因子同向度写进每条建议（原地），并返回本轮汇总。

    「同向」= 因子合成方向与建议方向一致；方向来自**样本内 rank IC**，
    不是预测。缺失/样本不足时明确写 neutral/missing，不硬凑结论。
    支持 ``snapshot_all`` 的合并结构（A 股/港股各自面板，不混算 z）。
    """
    if not snap or not snap.get("ok"):
        return {"status": "unavailable", "note": str((snap or {}).get("error") or "")[:200]}
    codes = snap.get("codes") or {}
    stat = {"status": "ok", "as_of": snap.get("as_of"),
            "run_id": snap.get("run_id"), "aligned": 0, "against": 0,
            "neutral": 0, "missing": 0, "total": 0, "by_market": {},
            "note": "方向来自各市场面板的样本内 rank IC（不是预测）"}
    for s in suggestions or []:
        code = _code(s.get("code"))
        stat["total"] += 1
        c = codes.get(code)
        mk = (c or {}).get("market") or ("hk" if len(code) == 5 else "a")
        bm = stat["by_market"].setdefault(mk, {
            "aligned": 0, "against": 0, "neutral": 0, "missing": 0, "total": 0})
        bm["total"] += 1
        if not c or c.get("missing") or c.get("composite") is None:
            s["factor"] = {"status": "missing", "market": mk,
                           "market_label": MARKET_LABEL.get(mk, mk),
                           "note": (c or {}).get("missing") or "该票不在因子面板"}
            stat["missing"] += 1
            bm["missing"] += 1
            continue
        comp = _num(c.get("composite"))
        conf = str(c.get("confidence") or "none")
        side = str(s.get("side") or "")
        verdict = "neutral"
        if comp is not None and conf in ("medium", "strong"):
            if side == "buy" and comp >= threshold:
                verdict = "aligned"
            elif side == "sell" and comp <= -threshold:
                verdict = "aligned"
            elif (side == "buy" and comp <= -threshold) or (side == "sell" and comp >= threshold):
                verdict = "against"
        s["factor"] = {
            "status": verdict, "composite": comp, "confidence": conf,
            "n_used": int(c.get("n_used") or 0),
            "market": mk, "market_label": MARKET_LABEL.get(mk, mk),
            "as_of": c.get("as_of") or snap.get("as_of"),
            "themes": _theme_items(c.get("theme_scores") or {}),
            "note": "样本内 IC 定向，不是预测",
        }
        stat[verdict] += 1
        bm[verdict] += 1
    # 组合层面的暴露：没有调仓时也能看「当前持仓更偏哪些风格」
    vals, n_pos = [], 0
    for p in (positions or []):
        c = codes.get(_code(p.get("code")))
        if c and not c.get("missing") and c.get("composite") is not None:
            vals.append(float(c["composite"]))
            n_pos += 1
    if n_pos:
        stat["portfolio"] = {"n": n_pos,
                             "mean_composite": round(sum(vals) / n_pos, 3)}
    return stat


def brief_lines(snap: dict, codes: list[str], limit: int = 12) -> list[str]:
    """日报上下文里的因子段；没有快照就返回 []（注入失败不能拖垮报告）。"""
    if not snap or not snap.get("ok"):
        return []

    markets = snap.get("markets")
    if markets:
        active = [m for m in ("a", "hk") if (markets.get(m) or {}).get("ok")]
        if not active:
            return []
        per = max(1, int(limit) // len(active))
        lines = ["【因子暴露（验证算子；A 股/港股各自面板的样本内 rank IC，不是预测）】"]
        shown = 0
        for m in active:
            s = markets[m]
            panel = s.get("panel") or {}
            alphas = s.get("alphas") or []
            n_alphas = len(alphas) or int(s.get("n_alphas") or 0)
            used = (sum(1 for a in alphas if a.get("ic_mean") is not None)
                    if alphas else int(s.get("n_used") or 0))
            lines.append(
                "- %s：数据截至 %s；面板 %s 只 × %s 天；%s 个算子（%s 有 IC），"
                "方向门槛 |IC|≥%s、|IR|≥%s、≥%s 天。"
                % (MARKET_LABEL.get(m, m), s.get("as_of"), panel.get("codes"),
                   panel.get("days"), n_alphas, used,
                   (s.get("params") or {}).get("ic_min_abs"),
                   (s.get("params") or {}).get("ic_min_ir"),
                   (s.get("params") or {}).get("ic_min_days")))
            got = 0
            for code in codes:
                if got >= per:
                    break
                c = (s.get("codes") or {}).get(_code(code))
                if not c or c.get("missing") or c.get("composite") is None:
                    continue
                themes = _theme_items(c.get("theme_scores") or {}, limit=3)
                t_txt = ("、".join("%s %+.2f" % (t["label"], t["z"])
                                  for t in themes) or "各主题接近中性")
                lines.append("- %s（%s）：综合 %+.2f（%s，%d 个算子）｜%s"
                             % (code, MARKET_LABEL.get(m, m), float(c["composite"]),
                                CONF_LABEL.get(c.get("confidence"),
                                               c.get("confidence") or "—"),
                                int(c.get("n_used") or 0), t_txt))
                got += 1
                shown += 1
        return lines if shown else []

    # 单市场（兼容旧快照）
    panel = snap.get("panel") or {}
    alphas = snap.get("alphas") or []
    n_alphas = len(alphas) or int(snap.get("n_alphas") or 0)
    used = (sum(1 for a in alphas if a.get("ic_mean") is not None)
            if alphas else int(snap.get("n_used") or 0))
    rows = snap.get("codes") or {}
    lines = ["【因子暴露（验证过的算子；方向为面板样本内 rank IC，不是预测）】",
             "- 数据截至 %s；样本 %s 只 × %s 个算子（%s 个有 IC），"
             "方向门槛 |IC|≥%s、|IR|≥%s、≥%s 天。"
             % (snap.get("as_of"), panel.get("codes"), n_alphas, used,
                (snap.get("params") or {}).get("ic_min_abs"),
                (snap.get("params") or {}).get("ic_min_ir"),
                (snap.get("params") or {}).get("ic_min_days"))]
    shown = 0
    for code in codes:
        if shown >= limit:
            break
        c = rows.get(_code(code))
        if not c:
            continue
        if c.get("missing") or c.get("composite") is None:
            continue
        themes = _theme_items(c.get("theme_scores") or {}, limit=3)
        t_txt = "、".join("%s %+.2f" % (t["label"], t["z"]) for t in themes) or "各主题接近中性"
        lines.append("- %s：综合 %+.2f（%s，%d 个算子）｜%s"
                     % (code, float(c["composite"]),
                        CONF_LABEL.get(c.get("confidence"), c.get("confidence") or "—"),
                        int(c.get("n_used") or 0), t_txt))
        shown += 1
    if shown == 0:
        return []
    return lines


def summary_line(stat: dict) -> str:
    if not stat or stat.get("status") != "ok":
        return ""
    out = ("因子同向 %d / 相悖 %d / 中性 %d / 缺数据 %d（%s，样本内 IC）"
           % (stat.get("aligned", 0), stat.get("against", 0),
              stat.get("neutral", 0), stat.get("missing", 0),
              stat.get("as_of") or "—"))
    bm = stat.get("by_market") or {}
    if len(bm) > 1:
        parts = []
        for m in ("a", "hk"):
            v = bm.get(m)
            if v:
                parts.append("%s 同向%d/相悖%d" % (MARKET_LABEL.get(m, m),
                                                  v.get("aligned", 0),
                                                  v.get("against", 0)))
        if parts:
            out += "；" + "，".join(parts)
    return out


# ---------------------------------------------------------------- 查询

def load_latest(cur, code: str | None = None, universe: str | None = None) -> dict:
    sql = """SELECT run_id, as_of, universe, n_codes, n_days, n_alphas, n_used,
                    n_errors, elapsed_s, upstream_version, panel, params,
                    status, error, created_at
               FROM sa_factor_run WHERE status = 'ok'"""
    args = []
    if universe:
        sql += " AND universe = %s"
        args.append(universe)
    sql += " ORDER BY created_at DESC LIMIT 1"
    cur.execute(sql, args)
    row = cur.fetchone()
    if row is None:
        return {"ok": True, "data": None}
    cols = [c[0] for c in (cur.description or [])]
    d = dict(row) if isinstance(row, dict) else dict(zip(cols, row))
    d["market"] = UNIVERSE_MARKET.get(str(d.get("universe") or ""), "a")
    d["market_label"] = MARKET_LABEL.get(d["market"], d["market"])
    run_id = d.get("run_id")
    cur.execute(
        """SELECT code, composite, confidence, n_used, n_alphas, themes, top, missing
             FROM sa_factor_code WHERE run_id = %s
            ORDER BY composite DESC NULLS LAST""", (run_id,))
    cols = [c[0] for c in (cur.description or [])]
    codes = {}
    for r in cur.fetchall():
        x = dict(r) if isinstance(r, dict) else dict(zip(cols, r))
        codes[_code(x.get("code"))] = {
            "composite": _num(x.get("composite")),
            "confidence": x.get("confidence"),
            "n_used": x.get("n_used"), "n_alphas": x.get("n_alphas"),
            "theme_scores": x.get("themes") or {},
            "top": x.get("top") or [],
            "missing": x.get("missing") or "",
            "market": d["market"], "market_label": d["market_label"],
            "universe": d.get("universe"), "as_of": d.get("as_of"),
        }
    d["codes"] = codes if code is None else (
        {_code(code): codes[_code(code)]} if _code(code) in codes else {})
    return {"ok": True, "data": d}


def load_latest_all(cur) -> dict:
    """读 A 股/港股各自的最新快照，合并成页面与单票查询用的结构。"""
    markets = {}
    for m, u in (("a", "equity_cn"), ("hk", "equity_hk")):
        d = load_latest(cur, universe=u).get("data")
        if d:
            markets[m] = d
    if not markets:
        return {"ok": True, "data": None}
    codes = {}
    runs = []
    as_ofs = []
    for m, d in markets.items():
        runs.append({k: d.get(k) for k in (
            "market", "market_label", "run_id", "as_of", "universe",
            "n_codes", "n_days", "n_alphas", "n_used", "elapsed_s",
            "upstream_version", "created_at")})
        if d.get("as_of"):
            as_ofs.append(d["as_of"])
        for code, v in (d.get("codes") or {}).items():
            codes[code] = v
    first = markets.get("a") or markets.get("hk") or {}
    missing = [MARKET_LABEL[m] for m in ("a", "hk") if m not in markets]
    data = {
        "as_of": max(as_ofs) if as_ofs else None,
        "markets": markets,
        "missing_markets": missing,
        "codes": codes,
        "runs": runs,
        "universe": "cn_hk" if len(markets) == 2 else first.get("universe"),
        "n_codes": sum(int(x.get("n_codes") or 0) for x in markets.values()),
        "n_alphas": sum(int(x.get("n_alphas") or 0) for x in markets.values()),
        "n_used": sum(int(x.get("n_used") or 0) for x in markets.values()),
        "panel": first.get("panel") or {},
        "params": first.get("params") or {},
        "run_id": first.get("run_id"),
        "created_at": max((x.get("created_at") for x in markets.values()
                           if x.get("created_at")), default=None),
        "market_labels": MARKET_LABEL,
    }
    return {"ok": True, "data": data}


def status(deps, conf: dict | None = None) -> dict:
    conf = conf or load_conf()
    out = {"enabled": bool(conf.get("enabled", True)),
           "worker_python": worker_python(conf),
           "worker_exists": WORKER.exists(),
           "catalog": {"file": CATALOG.name, "exists": CATALOG.exists()},
           "hk_universe": {"file": HK_UNIVERSE.name,
                           "exists": HK_UNIVERSE.exists(),
                           "count": len(load_hk_universe())},
           "config": {k: conf.get(k) for k in (
               "universe", "pool_size", "lookback_days", "min_bars",
               "ic_min_days", "ic_min_abs", "ic_min_ir", "align_threshold",
               "hk_enabled", "hk_pool_size", "hk_lookback_days", "hk_min_bars",
               "hk_strong_ratio", "hk_medium_ratio")}}
    try:
        out["latest_by_market"] = {
            m: _fetch(deps, """SELECT run_id, as_of, n_codes, n_days, n_alphas,
                                      n_used, elapsed_s, upstream_version, created_at
                                 FROM sa_factor_run WHERE status='ok' AND universe=%s
                                ORDER BY created_at DESC LIMIT 1""", (u,), one=True)
            for m, u in (("a", "equity_cn"), ("hk", "equity_hk"))}
        out["latest"] = out["latest_by_market"].get("a")
        out["hk_data"] = _fetch(
            deps,
            """SELECT count(DISTINCT code) AS codes, count(*) AS rows,
                      max(trade_date) AS last_bar, max(fetched_at) AS last_sync
                 FROM sa_hk_kline""", one=True)
    except Exception as exc:                       # noqa: BLE001
        out["latest"] = None
        out["latest_by_market"] = {}
        out["latest_error"] = str(exc)[:200]
    return out
