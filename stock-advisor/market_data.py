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
    code = str(code).strip()
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
