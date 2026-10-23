# -*- coding: utf-8 -*-
"""本地行情数据层：**全部经 101 data_service**，本机不直连任何行情源。

为什么改成「只经 101」（2026-10-22 用户要求）
--------------------------------------------
2026-10-22 实测：本机对东财 `push2his.eastmoney.com` 的 HTTPS 被重置，
腾讯旧 `fqkline` 全 501 → 日 K 全空 → 趋势判断只能落到 `flat`、做 T 全部
`range`（落库 31 行全是 bias=range，不是市场都震荡，是趋势判断没数据可用）。
同一时刻 101 直连东财 `http=200 / 0.067s`。用户裁决：
**本机不要直接调用任何数据源，都要经过 101。**

所以本模块只发两个 HTTP 请求：`{base}/kline` 与 `{base}/intraday`。
东财 → 腾讯 `newfqkline` 的降级在 101 侧（`data_service/market.py`）完成，
本机**不保留任何直连兜底** —— 有兜底就会在 101 故障时悄悄走另一条路，
两条路的字段口径不同，反而更难发现问题。

服务地址：`SA_DATA_SERVICE_URL` 环境变量优先，其次 config.yaml 的
`sector.data_service_url`；都为空直接报错，不猜默认值。

落库（不变）
------------
  1. 查 sa_market_kline 里该 code 的 max(trade_date)
  2. 只请求 [last_date+1, 今天]（增量）
  3. ON CONFLICT (code, trade_date) DO UPDATE —— 幂等
  4. 同一进程内再挡一层：_inflight 集合

字段口径：101 统一输出东财 CSV（f51..f61），**按位置解析**。缺字段填 NULL
而不是 0 —— 填 0 会让「成交额为 0」和「没取到」混为一谈。
"""
import os
import time
from datetime import date, datetime, timedelta

import requests

# 东财 K 线字段码
# f51 日期, f52 开, f53 收, f54 高, f55 低, f56 成交量(手/股), f57 成交额(元),
# f58 振幅, f59 涨跌幅, f60 涨跌额, f61 换手率
EM_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 只对 101 发请求：本机系统代理只服务 GitHub（127.0.0.1:6478），走代理反而
# 会因调用方的 NO_PROXY 设置差异出现随机 ProxyError。
_session = requests.Session()
_session.trust_env = False

_inflight: set = set()


def _base_url() -> str:
    """101 data_service 地址：环境变量优先于 config.yaml，留空直接报错。"""
    url = os.environ.get("SA_DATA_SERVICE_URL", "").strip()
    if not url:
        try:
            import yaml
            with open(os.path.join(_BASE_DIR, "config.yaml"), encoding="utf-8") as f:
                conf = (yaml.safe_load(f) or {}).get("sector") or {}
            url = str(conf.get("data_service_url") or "").strip()
        except FileNotFoundError:
            url = ""
        except Exception as exc:      # noqa: BLE001
            raise RuntimeError(
                "读取 config.yaml 的 sector.data_service_url 失败: %s" % exc)
    if not url:
        raise RuntimeError(
            "未配置行情服务地址：请设置 SA_DATA_SERVICE_URL 或 config.yaml 的 "
            "sector.data_service_url（本机不直连任何行情源）")
    return url.rstrip("/")


def _get_json(path: str, params: dict, timeout: int = 60) -> dict:
    url = _base_url() + path
    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError("行情服务不可达 %s: %s" % (url, str(exc)[:140]))
    if resp.status_code >= 400:
        raise RuntimeError("行情服务 %s 返回 %s: %s"
                           % (path, resp.status_code, resp.text[:200]))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError("行情服务 %s 返回非 JSON: %s" % (path, exc))
    if not isinstance(payload, dict):
        raise RuntimeError("行情服务 %s 返回结构异常（%s）"
                           % (path, type(payload).__name__))
    return payload


def service_get(path: str, params: dict, timeout: int = 60) -> dict:
    """对 101 data_service 发 GET。

    app.py 的行情 / 日K / 分时 / 汇率 / 美股都从这里走，保证**出口只有一处**：
    地址解析（env → config.yaml）与 `trust_env=False` 都在 `_get_json` 里，
    调用方不许再自己拼 URL 或另建 session。
    """
    return _get_json(path, params, timeout)


def _need_days(start: str, end: str, cap: int = 2000) -> int:
    """按 [start,end] 估算要请求多少根日 K（周末/节假日留 28% 余量）。"""
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = date.fromisoformat(str(end)[:10])
    except (ValueError, TypeError):
        return 800
    span = max((d1 - d0).days, 0)
    return min(max(int(span * 0.72) + 20, 60), cap)


def tx_symbol(code: str) -> str:
    """代码 -> 腾讯符号。

    港股必须走 `hk` + 5 位数字，不能落到 sz 分支（2026-10-04 实测）：
    港股 00136 被拼成 `sz00136`（一个不存在的深市代码），腾讯返回
    `code=0` 但 `data` 里没有该 key → `bars` 为空 → 抛
    「响应里没有 qfqday/day 数组」，看起来像源挂了，实际是符号拼错。
    同样的 `hk00136` + `,day,,,120,qfq` 实测能拿满 120 根，最后一根
    2026-10-02。所以港股不需要特殊 param，只是**符号前缀**要对。

    识别口径与 paper_trading.market_of / benchmark_symbol 一致：
    5 位且首位为 0（00136/09696/01024），或已带 hk 前缀。
    """
    code = str(code).strip().lower()
    if code.startswith("hk"):
        return "hk" + code[2:].zfill(5)
    if len(code) == 5 and code.isdigit():
        return "hk" + code.zfill(5)
    if code.startswith(("sh", "sz", "bj")):
        bare = code[2:]
        if bare[:1] == "0" and len(bare) == 5:
            return "hk" + bare
        code = bare
    if code.startswith(("60", "68", "51", "58", "11", "50", "56")):
        return "sh" + code
    return "sz" + code


def secid_of(code: str) -> str:
    """代码 -> 东财 secid：港股 116.，港股指数 100.，沪 1.，深/北 0.。

    不复用旧的 em_symbol：它对港股会拼成 `0.02513`（东财不认），
    对已带 `sh`/`sz` 前缀的输入也不剥前缀。
    """
    raw = str(code or "").strip()
    low = raw.lower()
    if low.startswith("hk") and not raw[2:].isdigit():
        return "100." + raw[2:].upper()
    sym = tx_symbol(raw)
    if sym.startswith("hk"):
        return "116." + sym[2:]
    if sym.startswith("sh"):
        return "1." + sym[2:]
    return "0." + sym[2:]


def _f(v):
    """东财用 '-' 表示空值。统一转 None，不要转 0。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _rows_from_csv(code: str, lines: list, source: str,
                   start: str = "", end: str = "") -> list[dict]:
    """东财 CSV（按 EM_FIELDS 位置）→ 本模块的行字典。"""
    rows = []
    for line in lines or []:
        p = str(line).split(",")
        if len(p) < 7:
            continue
        d = p[0][:10]
        if start and d < start:
            continue
        if end and d > end:
            continue
        rows.append({
            "code": code, "trade_date": d,
            "open": _f(p[1]), "close": _f(p[2]),
            "high": _f(p[3]), "low": _f(p[4]),
            "volume": _f(p[5]), "amount": _f(p[6]),
            "pct": _f(p[8]) if len(p) > 8 else None,
            "turnover_rate": _f(p[10]) if len(p) > 10 else None,
            "source": source,
        })
    return rows


def fetch_em_kline(code: str, start: str, end: str, timeout: int = 60) -> list[dict]:
    """东财日 K（经 101）。字段比腾讯全（有换手率/振幅）。"""
    payload = _get_json("/kline", {
        "secid": secid_of(code), "source": "em", "fields": EM_FIELDS,
        "days": _need_days(start, end), "beg": start, "end": end,
    }, timeout=timeout)
    rows = _rows_from_csv(code, payload.get("klines"), "em", start, end)
    if not rows:
        raise RuntimeError("东财 K 线为空 %s（服务端 notes=%s）"
                           % (code, payload.get("notes")))
    return rows


def fetch_tx_kline(code: str, start: str, end: str, timeout: int = 60) -> list[dict]:
    """腾讯日 K（经 101，服务端走 `newfqkline`）。

    旧 `fqkline` 在 2026-10-22 实测全主机 501，服务端已换新接口。
    成交额/换手率由服务端按腾讯第 8/7 字段换算（万元 → 元），这里直接收。
    """
    payload = _get_json("/kline", {
        "secid": secid_of(code), "source": "tx", "fields": EM_FIELDS,
        "days": _need_days(start, end), "beg": start, "end": end,
    }, timeout=timeout)
    rows = _rows_from_csv(code, payload.get("klines"), "tx", start, end)
    if not rows:
        raise RuntimeError("腾讯 K 线为空 %s（服务端 notes=%s）"
                           % (code, payload.get("notes")))
    return rows


# ---------------- 分时 / 分钟 K（做 T 的前提，2026-10-11 实测）----------------
#
# 实测结论（别重测）：
#   A 股 分时 minute/query  ✅ 267 点，末条 1530
#   港股 分时 minute/query  ✅ 332 点，末条 1608（港股收市到 16:00）
#   A 股 分钟K mkline m5/m30/m60 ✅ 320 根，末根 202610091500
#   港股 分钟K mkline        ❌ code=-1 "param error" + data:[]  ← 典型「200+空数据」
#
# 所以港股只能走分时。这是可接受的：分时是 **1 分钟粒度**，不比 m5 差，
# 且它同时给出成交额，能算 VWAP —— 做 T 反而只需要价格路径 + 成交额。
# 以上抓取逻辑现已全部在 101（`data_service/market.py`），本机只调 `/intraday`。


def is_hk(code: str) -> bool:
    """港股判定。与 tx_symbol 前缀一致，避免各处重复造轮子。"""
    return tx_symbol(str(code)).startswith("hk")


def _is_star(code: str) -> bool:
    """科创板 688/689：成交量单位是**股**，不能按 A 股「手」×100。"""
    c = str(code or "").strip().lower()
    if c.startswith(("sh", "sz")):
        c = c[2:]
    return c.startswith(("688", "689"))


def fetch_tx_trends(code: str, timeout: int = 60) -> dict:
    """分时（1 分钟，经 101）。A 股与港股都可用。

    返回 {"code","date","points":[{"t","price","volume","amount"}], "source"}。
    `volume` 原样返回、由 vwap() 按市场换算，**不要在调用方猜单位**。
    """
    payload = _get_json("/intraday", {
        "code": code, "period": "m5", "count": 320,
    }, timeout=timeout)
    points = payload.get("points") or []
    if not points:
        raise RuntimeError("分时为空 %s（source=%s）"
                           % (code, payload.get("source")))
    return {"code": code, "date": payload.get("date"),
            "points": points, "source": "tx_trend"}


def vwap(code: str, points: list[dict]) -> float | None:
    """成交量加权均价。**按市场处理成交量单位**，别在调用方算。

    取价兼容两种键名：分时点用 ``price``，分钟 K bar 用 ``close``。
    （只认其中一种会让另一条路径**静默返回 None** —— 2026-10-11 实测踩到。）

    成交量单位（用 `amount/volume≈当分钟价` 双向反算验证过）：
      沪深主板/ETF/北交所 → 手（×100 股）
      **科创板 688/689** → **股**（不再 ×100；2026-10-22 实测
      sh688795 amt/vol=354.97 而价格 350.27 → 已是股，原逻辑算出的
      VWAP 只有 3.57，偏低 100 倍）
      港股 → 股

    没有成交额时退化为「收盘价简单平均」。**注意退化结果不是真 VWAP**，
    判断依据是 `points` 里是否有 ``amount``，见 `has_amount()`。
    """
    def px(p):
        return p.get("price") if p.get("price") is not None else p.get("close")

    amt = sum(p["amount"] for p in points
              if p.get("amount") and p.get("volume") is not None)
    if amt <= 0:
        closes = [px(p) for p in points if px(p)]
        return sum(closes) / len(closes) if closes else None
    if is_hk(code) or _is_star(code):
        vol = sum(p["volume"] for p in points if p.get("volume"))
    else:
        vol = sum(p["volume"] for p in points if p.get("volume")) * 100.0
    if vol <= 0:
        return None
    return amt / vol


def has_amount(points: list[dict]) -> bool:
    """这些 bar 是否带**已验证单位**的成交额。真 VWAP 的前提。

    腾讯 mkline 第 8 字段（实测 694 手那根给 0.55）单位**无法用价格反算验证**，
    按「不猜单位」的原则不采用 —— 所以 A 股的 VWAP 一律走分时。
    """
    return bool(points) and any(p.get("amount") for p in points)


def fetch_tx_min_kline(code: str, period: str = "m5", count: int = 320,
                       timeout: int = 60) -> list[dict]:
    """分钟 K（经 101）。**只有 A 股可用**（港股 mkline 返回 code=-1 param error）。

    period: m1/m5/m15/m30/m60。返回 [{datetime, open, close, high, low, volume}]。
    """
    if is_hk(code):
        raise RuntimeError("港股分钟K不可用（mkline param error），请用 fetch_tx_trends")
    payload = _get_json("/intraday", {
        "code": code, "period": period, "count": int(count),
    }, timeout=timeout)
    bars = payload.get("bars") or []
    if not bars:
        raise RuntimeError("分钟K为空 %s %s（source=%s）"
                           % (code, period, payload.get("source")))
    return [{"datetime": b.get("datetime"), "open": b.get("open"),
             "close": b.get("close"), "high": b.get("high"),
             "low": b.get("low"), "volume": b.get("volume")} for b in bars]


def fetch_intraday(code: str, period: str = "m5", count: int = 320) -> dict:
    """统一的日内数据入口（经 101）：A 股 = 分钟K（带 OHLC）+ 分时（真 VWAP）。

    为什么要两个源：mkline 的 OHLC 更全（真实分钟内高开低收），但它第 8 字段
    的成交额单位**无法用价格反算验证**，按「不猜单位」原则不采用；
    分时的成交额已用 `amount/volume≈当分钟价` 双向验证过。
    所以 **VWAP 只信分时**，OHLC 只信 mkline。港股只有分时，影线失真已标注。

    返回 {"code","date","bars","points","vwap","vwap_is_true","source","has_ohlc"}。
    做 T 的所有判据都应该只用这一个函数的产物，不要在别处直接打接口。
    """
    return _get_json("/intraday", {
        "code": code, "period": period, "count": int(count),
    }, timeout=90)


def fetch_kline(code: str, start: str, end: str) -> list[dict]:
    """取日 K（经 101，服务端自动降级：东财 → 腾讯 `newfqkline`）。

    `source=auto` 让 101 决定用哪条线；本机**不再自己合并两个源** ——
    两条线的字段口径不同，合并逻辑散在两边就会各修各的。
    服务端的 `notes` 记录降级原因，挂到 `_partial` 上便于诊断但不阻断。
    """
    payload = _get_json("/kline", {
        "secid": secid_of(code), "source": "auto", "fields": EM_FIELDS,
        "days": _need_days(start, end), "beg": start, "end": end,
    }, timeout=60)
    source = str(payload.get("source") or "")
    rows = _rows_from_csv(code, payload.get("klines"), source, start, end)
    if not rows:
        raise RuntimeError("K 线为空 %s（服务端 notes=%s）"
                           % (code, payload.get("notes")))
    rows[0]["_partial"] = list(payload.get("notes") or [])
    return rows


# ---------------- 落库与增量 ----------------

def last_date(cur, code: str) -> str | None:
    cur.execute("SELECT max(trade_date) FROM sa_market_kline WHERE code=%s", (code,))
    r = cur.fetchone()
    return str(r[0])[:10] if r and r[0] else None


def store_kline(cur, rows: list[dict]) -> int:
    """写入 K 线。ON CONFLICT 保证幂等 —— 同一天被重复拉到不会产生重复行。"""
    if not rows:
        return 0
    from psycopg2.extras import execute_values
    # execute_values 而非 executemany：这台远程共享云库单语句往返约 65ms，
    # executemany 逐行往返会把 6000 行写成一分钟级（实测 1200 行 81.7 秒）。
    execute_values(
        cur,
        """INSERT INTO sa_market_kline
           (code, trade_date, open, high, low, close, volume, amount,
            pct, turnover_rate, source, fetched_at)
           VALUES %s
           ON CONFLICT (code, trade_date) DO UPDATE SET
             open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
             close = EXCLUDED.close, volume = EXCLUDED.volume,
             amount = EXCLUDED.amount, pct = EXCLUDED.pct,
             turnover_rate = EXCLUDED.turnover_rate,
             source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at""",
        [(r["code"], r["trade_date"], r["open"], r["high"], r["low"], r["close"],
          r["volume"], r["amount"], r["pct"], r["turnover_rate"],
          r.get("source", ""), datetime.now()) for r in rows],
        page_size=1000)
    return len(rows)


def sync_code(deps: dict, code: str, years: float = 3.0,
              end: str | None = None) -> dict:
    """把一只标的的 K 线同步到最新（增量）。返回统计。"""
    get_conn = deps["get_conn"]
    code = str(code).strip()
    end = end or date.today().isoformat()
    if code in _inflight:
        return {"code": code, "skipped": True, "why": "同进程内正在同步"}
    _inflight.add(code)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            have = last_date(cur, code)
            # 防重复拉取：只请求「比库里最后一天更新的部分」。
            # 若库里有今天以前的数据，起点就是 last_date+1（注意 +1 天）。
            if have:
                try:
                    d0 = date.fromisoformat(have)
                    start = (d0 + timedelta(days=1)).isoformat()
                except ValueError:
                    start = (date.today() - timedelta(days=int(years * 365))).isoformat()
            else:
                start = (date.today() - timedelta(days=int(years * 365))).isoformat()
            if start > end:
                return {"code": code, "fetched": 0, "up_to_date": True,
                        "last_date": have, "why": "已是最新"}
            try:
                rows = fetch_kline(code, start, end)
            except Exception as exc:      # noqa: BLE001
                return {"code": code, "fetched": 0, "error": str(exc)[:180],
                        "last_date": have}
            # _partial 是 fetch_kline 附的诊断信息（哪些源失败了），
            # 不是行情字段，必须剥掉再入库，否则列数对不上。
            partial = rows[0].pop("_partial", None) if rows else None
            srcs = sorted({r.get("source", "") for r in rows if r.get("source")})
            n = store_kline(cur, rows)
            conn.commit()
        warn = ""
        if partial:
            warn = "；部分源失败: %s" % " / ".join(partial)
            print(f"[market_data] {code} 部分源失败{warn}", flush=True)
        return {"code": code, "fetched": n, "sources": srcs,
                "from": start, "to": end, "last_date": have,
                "newest": rows[-1]["trade_date"] if rows else None,
                "oldest": rows[0]["trade_date"] if rows else None,
                "partial": partial}
    finally:
        _inflight.discard(code)


def load_kline(deps: dict, code: str, start: str = "", end: str = "",
               limit: int = 0) -> list[dict]:
    """从库里读 K 线（按日期升序）。回测/验证一律走这里，不联网。"""
    sql = ("SELECT trade_date, open, high, low, close, volume, amount, "
           "pct, turnover_rate FROM sa_market_kline WHERE code=%s")
    args = [code]
    if start:
        sql += " AND trade_date >= %s"
        args.append(start)
    if end:
        sql += " AND trade_date <= %s"
        args.append(end)
    sql += " ORDER BY trade_date"
    if limit:
        sql += " LIMIT %s"
        args.append(int(limit))
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def coverage(deps: dict, codes: list[str]) -> list[dict]:
    """看各标的的数据覆盖情况（回测前先自查：缺数据就别开跑）。"""
    if not codes:
        return []
    with deps["get_conn"]() as conn, conn.cursor() as cur:
        cur.execute("SELECT code, count(*) AS n, min(trade_date) AS d0, "
                    "max(trade_date) AS d1 FROM sa_market_kline "
                    "WHERE code = ANY(%s) GROUP BY code", (codes,))
        got = {r[0]: {"n": r[1], "d0": str(r[2])[:10], "d1": str(r[3])[:10]}
               for r in cur.fetchall()}
    out = []
    for c in codes:
        g = got.get(c)
        out.append({"code": c, "rows": g["n"] if g else 0,
                    "from": g["d0"] if g else None,
                    "to": g["d1"] if g else None,
                    "has_data": bool(g and g["n"] > 60)})
    return out


def sync_many(deps: dict, codes: list[str], years: float = 3.0) -> dict:
    """批量同步（**顺序**拉，别并发打同一家的接口 —— 并发会被风控）。

    实测：并发打东财会被拒（我今天已经把它烧穿过两次）。顺序拉虽然慢，
    但能拿到数据；拿不到数据其它都白搭。
    """
    done, failed, notes = [], [], []
    t0 = time.time()
    for i, c in enumerate(codes, 1):
        r = sync_code(deps, c, years=years)
        if r.get("fetched") or r.get("up_to_date"):
            done.append(r)
        else:
            failed.append(r)
        if r.get("partial"):
            notes.append("%s: %s" % (c, " / ".join(r["partial"])[:80]))
        print("  [%d/%d] %s -> %d 行 (%s)" % (
            i, len(codes), c, r.get("fetched") or 0,
            ("%s~%s" % (r.get("oldest"), r.get("newest")))
            if r.get("newest") else (r.get("error") or r.get("why") or "")[:50]),
            flush=True)
    return {"ok": not failed, "synced": len(done), "failed": len(failed),
            "total_rows": sum(x.get("fetched") or 0 for x in done),
            "elapsed_s": round(time.time() - t0, 1),
            "notes": notes, "results": done + failed}
