# -*- coding: utf-8 -*-
"""本地行情数据层：K 线落库 + 增量更新 + 防重复拉取。

为什么必须落库（2026-09-30）
---------------------------
1) **回测要反复取数**。一个 3 年回测、5 只标的、调 20 组参数 = 300 次取数。
   直接打行情接口会被风控 —— 我今天已经被东财 push2 封过一次（6 个镜像主机全挂，
   十几分钟不恢复）。app.py 里 fetch_kline_volumes 已经写了「东财超时回退腾讯」
   的补丁，就是那次踩坑的产物。
2) **增量更新天然防重**。K 线是**只追加**的数据：今天的数据不会再变，历史更不会。
   所以用 `UNIQUE(code, trade_date)` + 只请求「比库里最后一天更新的部分」，
   就同时解决了「防重复拉取」和「省流量」两件事 —— 不需要额外的去重表。

防重复拉取的具体做法
--------------------
  1. 查 sa_market_kline 里该 code 的 max(trade_date)
  2. 只请求 [last_date+1, 今天]
  3. ON CONFLICT (code, trade_date) DO UPDATE —— 幂等，
     即使同一天被两个进程同时拉也不会产生重复行
  4. 同一进程内再挡一层：_inflight 集合，防并发重复请求

数据源与降级
------------
东财 push2his（字段最全，有换手率/成交额）为主；它有 WAF，超时自动回退腾讯
ifzq.gtimg.cn（只有 OHLCV，少换手率）。两条路的字段差在下面统一处理，
缺字段填 NULL 而不是 0 —— 填 0 会让「成交额为 0」和「没取到」混为一谈。
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 东财 K 线字段码
# f51 日期, f52 开, f53 收, f54 高, f55 低, f56 成交量(手), f57 成交额, f58 振幅,
# f59 涨跌幅, f60 涨跌额, f61 换手率
EM_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东财的备用主机（每个是独立限频桶）
EM_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://push2his.eastmoney.com",
    "https://push2his.eastmoney.com",
]
# 顺序有意义：`web.` 前缀那个从 2026-09-16 起频发 501，放最后兜底
# （见 fetch_tx_kline 的注释）。
TX_URLS = [
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
]

_inflight: set = set()


def em_symbol(code: str) -> str:
    """把 6 位代码转成东财 secid：沪 1.xxxxxx / 深 0.xxxxxx / 北交所 0.xxxxxx。"""
    code = str(code).strip()
    if code.startswith(("5", "6", "9", "11", "13", "68")) and not code.startswith("688"):
        pass
    if code.startswith(("60", "68", "51", "58", "11", "50", "56")):
        return "1." + code
    return "0." + code


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


def fetch_em_kline(code: str, start: str, end: str, timeout: int = 20) -> list[dict]:
    """东财 push2his K 线。字段比腾讯全（有换手率/振幅），优先用它。"""
    sym = em_symbol(code)
    params = {
        "secid": sym, "fields": EM_FIELDS, "klt": "101", "fqt": "1",
        "beg": start.replace("-", ""), "end": end.replace("-", ""),
        "lmt": "10000", "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    last = None
    for host in EM_HOSTS:
        url = "%s/api/qt/stock/kline/get?%s" % (
            host, urllib.parse.urlencode(params))
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": "https://quote.eastmoney.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            data = d.get("data") or {}
            rows = []
            for line in (data.get("klines") or []):
                p = line.split(",")
                if len(p) < 7:
                    continue
                rows.append({
                    "code": code,
                    "trade_date": p[0],
                    "open": _f(p[1]), "close": _f(p[2]),
                    "high": _f(p[3]), "low": _f(p[4]),
                    "volume": _f(p[5]), "amount": _f(p[6]),
                    "pct": _f(p[8]) if len(p) > 8 else None,
                    "turnover_rate": _f(p[10]) if len(p) > 10 else None,
                    "source": "em",
                })
            if rows:
                return rows
        except Exception as exc:      # noqa: BLE001
            last = exc
    raise RuntimeError("东财 K 线失败 %s: %s" % (code, str(last)[:100]))


def fetch_tx_kline(code: str, start: str, end: str, timeout: int = 20) -> list[dict]:
    """腾讯 ifzq 备用源。只有 OHLCV，没有成交额/换手率（缺失留 None，不填 0）。

    两个坑（都踩过，注释留着别再踩）：
    1) **域名**：`web.ifzq.gtimg.cn` 从 2026-09-16 起频发 **501 Not Implemented**
       （app.py:fetch_kline_volumes 的注释里记着 2026-09-26 的实测结论：
       去掉 `web.` 前缀就正常）。所以顺序是：ifzq -> proxy.finance -> web.（兜底）。
    2) **响应是标准 JSON**，不是 JS 变量。结构：
           {"code":0,"data":{"sz510300":{"qfqday":[[date,open,close,high,low,volume],...]}}}
       键 `qfqday` 是前复权，缺失时取 `day`。**不是** `v_sz510300="...~[[...]]"`。
    3) param 用**数量**口径 `symbol,day,,,{n},qfq`（日期留空），而不是日期区间 ——
       腾讯对日期区间参数支持不稳，容易返回空。
    """
    sym = tx_symbol(code)
    # 按起始日回推需要多少天：预留 weekends + 节假日余量
    try:
        d0 = date.fromisoformat(str(start)[:10])
        need = max(120, int((date.today() - d0).days * 0.75) + 30)
    except (ValueError, TypeError):
        need = 800
    need = min(need, 1600)
    last = None
    for host in TX_URLS:
        url = "%s?param=%s,day,,,%d,qfq" % (host, sym, need)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            node = ((d.get("data") or {}).get(sym) or {})
            bars = node.get("qfqday") or node.get("day") or []
            rows = []
            for p in bars:
                if len(p) < 6:
                    continue
                rows.append({
                    "code": code, "trade_date": p[0],
                    "open": _f(p[1]), "close": _f(p[2]),
                    "high": _f(p[3]), "low": _f(p[4]),
                    "volume": _f(p[5]),
                    "amount": None,          # 这条线没有成交额
                    "pct": None, "turnover_rate": None,
                    "source": "tx",
                })
            if rows:
                return [r for r in rows if start <= r["trade_date"] <= end]
            last = "响应里没有 qfqday/day 数组（code=%s）" % d.get("code")
        except Exception as exc:      # noqa: BLE001
            last = exc
    raise RuntimeError("腾讯 K 线失败 %s: %s" % (code, str(last)[:120]))


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
TX_TREND_URLS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query",
    "https://ifzq.gtimg.cn/appstock/app/minute/query",
]
TX_MKLINE_URLS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
    "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
]


def is_hk(code: str) -> bool:
    """港股判定。与 tx_symbol 前缀一致，避免各处重复造轮子。"""
    return tx_symbol(str(code)).startswith("hk")


def _get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_tx_trends(code: str, timeout: int = 15) -> dict:
    """分时（1 分钟）。A 股与港股都可用。

    返回 {"code","date","points":[{"t","price","volume","amount"}], "source"}。

    **成交量单位两边不同 —— 这是算 VWAP 时最容易错 100 倍的地方**：
      A 股 手（1 手 = 100 股）：`24480690.00 / (195*100) = 1255.42` = 当分钟价格 ✓
      港股 股：                `179021692.0 / 431380     = 415.00` = 当分钟价格 ✓
    所以 `volume` 原样返回、由 vwap() 按市场换算，**不要在调用方猜单位**。
    """
    sym = tx_symbol(code)
    last = None
    for host in TX_TREND_URLS:
        url = "%s?code=%s" % (host, urllib.parse.quote(sym))
        try:
            j = _get_json(url, timeout)
            data = j.get("data") or {}
            # 结构和 mkline 不同：data.<sym>.data.data 才是点，data.<sym>.data.date 是日期
            node = data.get(sym)
            if not isinstance(node, dict):
                last = "data 里没有 %s（%s）" % (sym, str(j.get("msg"))[:40])
                continue
            inner = node.get("data")
            if not isinstance(inner, dict):
                last = "缺少 data.<sym>.data"
                continue
            raw = inner.get("data")
            day = str(inner.get("date") or "")
            if not isinstance(raw, list) or not raw:
                last = "points 为空（date=%s）" % day
                continue
            pts = []
            for line in raw:
                p = str(line).split()
                if len(p) < 2:
                    continue
                pts.append({
                    "t": p[0][:4],                       # HHMM，如 '0930'
                    "price": _f(p[1]),
                    "volume": _f(p[2]) if len(p) > 2 else None,
                    "amount": _f(p[3]) if len(p) > 3 else None,
                })
            if not pts:
                last = "解析后为空"
                continue
            return {"code": code, "date": day, "points": pts, "source": "tx_trend"}
        except Exception as exc:      # noqa: BLE001
            last = exc
    raise RuntimeError("分时失败 %s: %s" % (code, str(last)[:120]))


def vwap(code: str, points: list[dict]) -> float | None:
    """成交量加权均价。**按市场处理成交量单位**，别在调用方算。

    取价兼容两种键名：分时点用 ``price``，分钟 K bar 用 ``close``。
    （只认其中一种会让另一条路径**静默返回 None** —— 2026-10-11 实测踩到。）

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
    if is_hk(code):
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
                       timeout: int = 15) -> list[dict]:
    """分钟 K。**只有 A 股可用**（港股 mkline 返回 code=-1 param error）。

    period: m1/m5/m15/m30/m60。返回 [{datetime, open, close, high, low, volume}]。
    """
    if is_hk(code):
        raise RuntimeError("港股分钟K不可用（mkline param error），请用 fetch_tx_trends")
    sym = tx_symbol(code)
    last = None
    for host in TX_MKLINE_URLS:
        url = "%s?param=%s,%s,,,%d" % (host, urllib.parse.quote(sym), period, int(count))
        try:
            j = _get_json(url, timeout)
            node = (j.get("data") or {}).get(sym)
            rows = node.get(period) if isinstance(node, dict) else None
            if not isinstance(rows, list) or not rows:
                last = "%s 为空（%s）" % (period, str(j.get("msg"))[:40])
                continue
            out = []
            for r in rows:
                if not isinstance(r, list) or len(r) < 6:
                    continue
                out.append({"datetime": str(r[0]), "open": _f(r[1]),
                            "close": _f(r[2]), "high": _f(r[3]),
                            "low": _f(r[4]), "volume": _f(r[5])})
            if out:
                return out
        except Exception as exc:      # noqa: BLE001
            last = exc
    raise RuntimeError("分钟K失败 %s %s: %s" % (code, period, str(last)[:120]))


def fetch_intraday(code: str, period: str = "m5", count: int = 320) -> dict:
    """统一的日内数据入口。A 股拿分钟K（带 OHLC）+ 分时（**已验证的 VWAP**）。

    为什么要两个源：mkline 的 OHLC 更全（真实分钟内高开低收），但它第 8 字段
    的成交额单位**无法用价格反算验证**，按「不猜单位」原则不采用；
    分时的成交额已用 `amount/volume≈当分钟价` 双向验证过（见 fetch_tx_trends）。
    所以 **VWAP 只信分时**，OHLC 只信 mkline。港股只有分时，影线失真已标注。

    返回 {"code","date","bars","points","vwap","vwap_is_true","source","has_ohlc"}。
    做 T 的所有判据都应该只用这一个函数的产物，不要在别处直接打接口。
    """
    hk = is_hk(code)
    bars = None
    src = None
    if not hk:
        try:
            bars = fetch_tx_min_kline(code, period, count)
            src = "tx_mkline_" + period
        except Exception:                   # noqa: BLE001
            bars = None
    # 分时是 VWAP 的唯一可信来源，两条路径都要试
    points = None
    day = None
    tv = None
    try:
        tr = fetch_tx_trends(code)
        points, day = tr["points"], tr["date"]
        tv = vwap(code, points)
    except Exception:                       # noqa: BLE001
        points = None
    if bars is None and points is None:
        raise RuntimeError("日内数据全部失败 %s（分钟K与分时都取不到）" % code)
    if bars is None:
        # 分时没有每分钟 OHLC，用该分钟的成交价同时充当 O/H/L/C。
        # 这会**低估**分钟内振幅 —— 做 T 的判据必须知道这点：
        # 任何依赖「分钟 K 影线」的逻辑在分时源上是失真的。
        bars = [{"datetime": "%s%s" % (day, p["t"]), "open": p["price"],
                 "close": p["price"], "high": p["price"], "low": p["price"],
                 "volume": p["volume"], "amount": p["amount"]}
                for p in points if p.get("price")]
        src = "tx_trend"
    return {"code": code, "date": day or (bars[-1]["datetime"][:8] if bars else None),
            "is_hk": hk, "bars": bars, "points": points,
            "vwap": tv if tv is not None else vwap(code, bars),
            "vwap_is_true": tv is not None,
            "source": src, "has_ohlc": src.startswith("tx_mkline")}


def fetch_kline(code: str, start: str, end: str) -> list[dict]:
    """取 K 线：**两个源合并**，不是「东财优先、失败就退」。

    为什么改成合并而不是二选一（2026-09-30 实测）
    ---------------------------------------------
    1) 两个源的**历史长度上限不同**。腾讯单次上限约 640 行（实测要 3 年只拿到
       638 行、起点被截到 2024-02），东财能一次给 1000+。只用一个源就必然
       少一段历史。
    2) 东财的 WAF 比我预想的紧。今天我已经把它烧穿两次（push2 全主机被封、
       push2his 也 RemoteDisconnected），而腾讯始终稳定。
    3) 字段互补：东财有成交额/换手率，腾讯没有。合并后能拿到就用。

    所以：各取各的，按 trade_date 去重（**东财优先**保留它的字段，因为它更全）。
    全挂才抛错。
    """
    got: dict[str, dict] = {}
    errs = []
    for label, fn in (("em", fetch_em_kline), ("tx", fetch_tx_kline)):
        try:
            for r in fn(code, start, end):
                # 已有的不覆盖：东财先跑且字段更全，腾讯后跑只补空缺
                prev = got.get(r["trade_date"])
                if prev is None:
                    got[r["trade_date"]] = r
                else:
                    for k, v in r.items():
                        if prev.get(k) is None and v is not None:
                            prev[k] = v
        except Exception as exc:      # noqa: BLE001
            errs.append("%s: %s" % (label, str(exc)[:90]))
    if not got:
        raise RuntimeError("两个源都失败 %s -> %s" % (code, " | ".join(errs)))
    rows = [got[d] for d in sorted(got)]
    rows[0]["_partial"] = errs      # 附上失败的源，便于诊断但不阻断
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
