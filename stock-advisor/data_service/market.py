# -*- coding: utf-8 -*-
"""行情代理：日 K / 实时行情 / 分时。

为什么集中在这里（2026-10-22）
---------------------------
本机（住宅网络）对 `push2his.eastmoney.com` 的 HTTPS 被重置，腾讯旧
`fqkline` 全 501 → 日 K 全空 → 趋势判断只能落到 `flat`、做 T 全部 `range`。
101 实测直连东财 `http=200 / 0.067s`，所以把行情抓取集中到服务端，
本机只做 HTTP 客户端 —— **本机不再直连任何行情源**。

对外统一成东财 CSV 口径（`f51..f61`），调用方按 `fields` 的**位置**解析即可，
不需要知道背后是东财还是腾讯。

单位口径（实测，别改）
---------------------
- 腾讯日 K `volume`：科创板 688/689 是**股**，其余 A 股是**手**，港股是**股**。
  对外 f56 统一成东财口径：A 股 = 手（科创板 /100），港股 = 股。
- 腾讯日 K 第 8 字段是**成交额（万元）**，×1e4 得元；缺失时按 量×价 估算
  （估算值也必须是数字：`paper_trading.fetch_kline_ohlcv` 对 `"-"` 会
  `float()` 抛错并把整行丢掉）。
- 分时 `volume`：A 股手、港股股 —— VWAP 的 ×100 换算只在本模块做一次。
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TX_KLINE_URL = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/"
                "newfqkline/get")
TX_QUOTE_URL = "https://qt.gtimg.cn/q="
SINA_HK_URL = "https://hq.sinajs.cn/list="
TX_TREND_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query",
    "https://ifzq.gtimg.cn/appstock/app/minute/query",
)
TX_MKLINE_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
    "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
)

# f51 日期, f52 开, f53 收, f54 高, f55 低, f56 成交量(手/股), f57 成交额(元),
# f58 振幅, f59 涨跌幅, f60 涨跌额, f61 换手率
EM_FIELDS = ("f51", "f52", "f53", "f54", "f55", "f56", "f57", "f58",
             "f59", "f60", "f61")
DEFAULT_FIELDS = "f51,f52,f53,f54,f55,f56,f57"

# 市场前缀是 1~3 位：东财 A 股是 0/1，港股 116/100，指数 90/128…
SECID_RE = re.compile(r"^(\d{1,3})\.([0-9A-Za-z.]+)$")


class UpstreamError(RuntimeError):
    """上游取不到数据（不是调用方参数错）。"""


# ---------------- HTTP ----------------

def _get(url: str, *, params=None, timeout: int = 15, referer: str | None = None):
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def _num(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "null", "None", "nan"):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if n == n and abs(n) != float("inf") else None


def _fmt(v) -> str:
    return "-" if v is None else "%.2f" % v


def _norm_date(v: str) -> str:
    s = str(v or "").strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return str(v or "").strip()[:10]


# ---------------- secid <-> 腾讯符号 ----------------

def parse_secid(secid: str) -> tuple[str, str]:
    m = SECID_RE.match(str(secid or "").strip())
    if not m:
        raise ValueError("secid 格式应为 <市场>.<代码>，例如 1.600519 / 116.02513")
    return m.group(1), m.group(2)


def secid_to_tx(secid: str) -> str:
    market, code = parse_secid(secid)
    if market == "1":
        return "sh" + code
    if market == "0":
        return "sz" + code
    if market == "116":
        return "hk" + code.zfill(5)
    if market == "100":
        return "hk" + code
    raise ValueError("不支持的 secid 市场前缀 %s" % market)


def _is_hk(secid: str) -> bool:
    return parse_secid(secid)[0] in ("116", "100")


def _is_star(secid: str) -> bool:
    market, code = parse_secid(secid)
    return market in ("0", "1") and code.startswith(("688", "689"))


def tx_to_secid(symbol: str) -> str:
    # 只对前缀小写化：`hkHSI` 的指数代码必须原样保留大小写（东财是 100.HSI）
    raw = str(symbol or "").strip()
    s = raw.lower()
    if s.startswith("sh"):
        return "1." + raw[2:]
    if s.startswith("sz"):
        return "0." + raw[2:]
    if s.startswith("bj"):
        return "0." + raw[2:]
    if s.startswith("hk"):
        rest = raw[2:]
        return ("116." + rest.zfill(5)) if rest.isdigit() else ("100." + rest)
    raise ValueError("无法识别的腾讯符号 %r" % symbol)


# ---------------- 日 K ----------------

def _em_kline(secid: str, days: int, fields: str,
              beg: str, end: str) -> list[str]:
    params = {"secid": secid, "klt": 101, "fqt": 1,
              "lmt": max(int(days or 1), 1),
              "fields1": "f1,f2,f3", "fields2": fields,
              "end": (end or "20500101").replace("-", "")}
    if beg:
        params["beg"] = beg.replace("-", "")
    resp = _get(EM_URL, params=params, referer="https://quote.eastmoney.com/")
    try:
        data = resp.json().get("data") or {}
    except ValueError as exc:
        raise UpstreamError("东财返回非 JSON: %s" % exc) from exc
    rows = data.get("klines") or []
    if not rows:
        raise UpstreamError("东财返回 0 根（secid=%s）" % secid)
    return rows


def _tx_need(days: int, beg: str) -> int:
    n = max(int(days or 0), 0)
    b = _norm_date(beg)
    if b:
        try:
            d0 = date.fromisoformat(b)
            n = max(n, int((date.today() - d0).days * 0.72) + 20)
        except ValueError:
            pass
    return min(max(n, 60), 2000)


def _tx_kline(secid: str, days: int, fields: str,
              beg: str, end: str) -> list[str]:
    """腾讯日 K → 东财 CSV。字段按 `fields` 的顺序输出。"""
    sym = secid_to_tx(secid)
    need = _tx_need(days, beg)
    resp = _get(TX_KLINE_URL,
                params={"param": "%s,day,,,%d,qfq" % (sym, need)},
                referer="https://gu.qq.com/")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise UpstreamError("腾讯返回非 JSON: %s" % exc) from exc
    node = (payload.get("data") or {}).get(sym) or {}
    bars = node.get("qfqday") or node.get("day") or []
    if not bars:
        raise UpstreamError("腾讯返回 0 根（%s, code=%s）"
                            % (sym, payload.get("code")))

    want = [f for f in str(fields).split(",") if f in EM_FIELDS]
    if not want:
        raise ValueError("fields 里没有可用字段")
    hk, star = _is_hk(secid), _is_star(secid)
    b0, e0 = _norm_date(beg), _norm_date(end)
    out: list[str] = []
    prev_close = None
    for p in bars:
        if not isinstance(p, list) or len(p) < 6:
            continue
        d = str(p[0])[:10]
        if b0 and d < b0:
            prev_close = _num(p[2])
            continue
        if e0 and d > e0:
            break
        try:
            o, c, h, lo, v = (float(p[1]), float(p[2]), float(p[3]),
                              float(p[4]), float(p[5]))
        except (TypeError, ValueError):
            continue
        if v < 0:
            continue
        # 东财 f56 口径：A 股手（科创板 /100），港股股
        vol = v if (hk or star) else v
        if star:
            vol = v / 100.0
        shares = v if (hk or star) else v * 100.0
        amt = _num(p[8]) if len(p) > 8 else None
        if amt is not None:
            amt *= 1e4                      # 万元 → 元
        if amt is None or amt <= 0:
            amt = shares * c if c > 0 else None
        tr = _num(p[7]) if len(p) > 7 else None
        if prev_close:
            pct = (c - prev_close) / prev_close * 100.0
            chg = c - prev_close
            amp = (h - lo) / prev_close * 100.0
        else:
            pct = chg = amp = None
        prev_close = c
        vals = {"f51": d, "f52": o, "f53": c, "f54": h, "f55": lo,
                "f56": vol, "f57": amt, "f58": amp, "f59": pct,
                "f60": chg, "f61": tr}
        out.append(",".join(_fmt(vals[f]) if f != "f51" else str(vals[f])
                            for f in want))
    if not out:
        raise UpstreamError("腾讯无可用 bar（%s）" % sym)
    return out


def kline(secid: str, days: int = 60, fields: str = DEFAULT_FIELDS,
          source: str = "auto", beg: str = "", end: str = "") -> dict:
    """日 K。`source=em|tx|auto`；auto 先东财、失败退腾讯。

    返回 {"secid","source","count","klines":["YYYY-MM-DD,开,收,..."],"notes"}。
    """
    parse_secid(secid)
    source = (source or "auto").strip().lower()
    if source not in ("auto", "em", "tx"):
        raise ValueError("source 只能是 auto / em / tx")
    if not fields or any(f not in EM_FIELDS for f in fields.split(",")):
        raise ValueError("fields 只能取 %s" % ",".join(EM_FIELDS))
    order = ("em", "tx") if source == "auto" else (source,)
    errs: list[str] = []
    for src in order:
        try:
            rows = (_em_kline(secid, days, fields, beg, end) if src == "em"
                    else _tx_kline(secid, days, fields, beg, end))
            return {"secid": secid, "source": src, "count": len(rows),
                    "klines": rows, "notes": errs}
        except Exception as exc:            # noqa: BLE001
            errs.append("%s: %s" % (src, str(exc)[:160]))
    raise UpstreamError("K 线全部失败 %s -> %s" % (secid, " | ".join(errs)))


# ---------------- 实时行情 ----------------

def _code_of_symbol(sym: str) -> str:
    s = str(sym).strip()
    if s.startswith("hk"):
        return s[2:]
    if s.startswith(("sh", "sz", "bj")):
        return s[2:]
    return s


def _fmt_quote_time(raw: str):
    if not raw:
        return None
    if re.fullmatch(r"\d{14}", raw):
        return ("%s-%s-%s %s:%s:%s" % (raw[:4], raw[4:6], raw[6:8],
                                       raw[8:10], raw[10:12], raw[12:14]))
    if "/" in raw and ":" in raw:          # 2026/09/04 16:08:05
        d, _, t = raw.partition(" ")
        try:
            y, m, dd = d.split("/")
            return "%s-%s-%s %s" % (y, m, dd, t)
        except ValueError:
            return None
    return None


def _tencent_quotes(symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    resp = _get(TX_QUOTE_URL + ",".join(symbols), timeout=20)
    text = resp.content.decode("gbk", "ignore")
    for sym in symbols:
        m = re.search(rf'{re.escape(sym)}="([^"]*)"', text)
        if not m or not m.group(1):
            out[sym] = {"code": _code_of_symbol(sym), "symbol": sym,
                        "error": "行情未返回"}
            continue
        f = m.group(1).split("~")
        # ⚠️ 不能一刀切 `len(f) < 50`：汇率 `whHKDCNY` 与美国 `usXXX` 的字段
        # 远少于 50，那样会被判成「行情数据异常」。改为**逐字段按需取值**，
        # 只要求价格存在（缺字段留 None）。
        if len(f) < 4 or not f[3]:
            out[sym] = {"code": _code_of_symbol(sym), "symbol": sym,
                        "error": "行情数据异常"}
            continue

        def at(i):
            return f[i] if len(f) > i else ""

        amount = None
        if "/" in at(35):
            parts = at(35).split("/")
            if len(parts) >= 3:
                amount = _num(parts[2])        # 已是绝对元
        if amount is None and at(37):
            a = _num(at(37))
            amount = a * 1e4 if a is not None else None
        hk = sym.startswith("hk")          # 含 hkHSI，与旧行为一致
        us = sym.lower().startswith("us")
        fx = sym.lower().startswith("wh")
        out[sym] = {
            "code": _code_of_symbol(sym),
            "symbol": sym,
            "name": at(1) or sym,
            "market": "hk" if hk else ("us" if us else ("fx" if fx else "a")),
            "currency": "HKD" if hk else ("USD" if us else "CNY"),
            "price": _num(at(3)),
            "prev_close": _num(at(4)),
            "open": _num(at(5)),
            "volume": _num(at(6)),              # A 股手 / 港股股
            "change": _num(at(31)),
            "change_pct": _num(at(32)),
            "high": _num(at(33)),
            "low": _num(at(34)),
            "amount": amount,                  # 绝对元 / 港元
            "market_cap": _num(at(45)),
            "ratio": _num(at(49)),             # 量比
            "updated_at": _fmt_quote_time(at(30)),
        }
    return out


def _sina_hk_quotes(symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    url = SINA_HK_URL + ",".join("rt_hk" + sym[2:] for sym in symbols)
    resp = _get(url, timeout=20, referer="https://finance.sina.com.cn")
    text = resp.content.decode("gbk", "ignore")
    for sym in symbols:
        code = sym[2:]
        m = re.search(rf'rt_hk{re.escape(code)}="([^"]*)"', text)
        if not m or not m.group(1):
            out[sym] = {"code": code, "symbol": sym, "error": "行情未返回"}
            continue
        f = m.group(1).split(",")
        if len(f) < 19 or not f[6]:
            out[sym] = {"code": code, "symbol": sym, "error": "行情数据异常"}
            continue
        out[sym] = {
            "code": code,
            "symbol": sym,
            "name": f[1],
            "market": "hk",
            "currency": "HKD",
            "price": _num(f[6]),
            "prev_close": _num(f[3]),
            "open": _num(f[2]),
            "volume": _num(f[12]),             # 股
            "change": _num(f[7]),
            "change_pct": _num(f[8]),
            "high": _num(f[4]),
            "low": _num(f[5]),
            "amount": _num(f[11]),             # 绝对港元
            "market_cap": None,
            "ratio": None,                     # 新浪 rt_hk 无量比字段
            "updated_at": _fmt_quote_time("%s %s" % (f[17], f[18])),
        }
    return out


_HK_CAP_TTL = 3600
_hk_cap_cache: dict[str, tuple] = {}
_hk_cap_lock = threading.Lock()


def _fill_hk_market_cap(result: dict[str, dict]) -> None:
    """港股行情（新浪无市值）用腾讯 `hk{code}` 的 f45 补，按代码缓存 1 小时。"""
    hk = [s for s, q in result.items()
          if s.startswith("hk") and s[2:].isdigit() and q.get("name")
          and q.get("market_cap") is None]
    if not hk:
        return
    now = time.time()
    with _hk_cap_lock:
        stale = [s for s in hk
                 if s not in _hk_cap_cache
                 or now - _hk_cap_cache[s][1] >= _HK_CAP_TTL]
        for s in hk:
            if s in _hk_cap_cache:
                result[s]["market_cap"] = _hk_cap_cache[s][0]
    if not stale:
        return
    caps: dict[str, float | None] = {}
    try:
        resp = _get(TX_QUOTE_URL + ",".join(stale), timeout=10)
        text = resp.content.decode("gbk", "ignore")
        for s in stale:
            m = re.search(rf'{re.escape(s)}="([^"]*)"', text)
            f = m.group(1).split("~") if m else []
            caps[s] = _num(f[45]) if len(f) > 45 else None
    except Exception:                       # noqa: BLE001
        return
    with _hk_cap_lock:
        for s, v in caps.items():
            _hk_cap_cache[s] = (v, now)
            result[s]["market_cap"] = v


def quotes(symbols: list[str]) -> dict:
    """批量实时行情。symbols 用腾讯符号（sh600519 / hk00100 / hkHSI）。"""
    seen: list[str] = []
    for s in symbols:
        s = str(s or "").strip()
        if s and s not in seen:
            seen.append(s)
    if len(seen) > 300:
        raise ValueError("一次最多 300 个符号")
    out: dict[str, dict] = {}
    errs: list[str] = []
    a_syms = [s for s in seen if not (s.startswith("hk") and s[2:].isdigit())]
    hk_syms = [s for s in seen if s.startswith("hk") and s[2:].isdigit()]
    if a_syms:
        try:
            out.update(_tencent_quotes(a_syms))
        except Exception as exc:            # noqa: BLE001
            errs.append("腾讯: %s" % str(exc)[:140])
    if hk_syms:
        try:
            out.update(_sina_hk_quotes(hk_syms))
        except Exception as exc:            # noqa: BLE001
            errs.append("新浪港股: %s" % str(exc)[:140])
        _fill_hk_market_cap(out)
    for s in seen:
        out.setdefault(s, {"code": _code_of_symbol(s), "symbol": s,
                           "error": "行情未返回"})
    return {"quotes": out, "notes": errs}


# ---------------- 汇率 / 国际期货 ----------------
#
# 为什么自己解析而不是用 akshare（2026-10-22 实测后决定）
# ------------------------------------------------------
# `ak.futures_foreign_commodity_realtime` 内部 requests **没有 timeout**，在
# 服务端会挂住整个 uvicorn worker；且一次调用要打 4 个请求（行情 + hf.html +
# USDCNY + 代码映射），批量时反而更慢。新浪 `hf_` 一次请求就能拿全部标的，
# 这里按**实测的字段位置**解析（与 akshare 同源同口径），并显式给超时。

SINA_FX_URL = "https://hq.sinajs.cn/list="
SINA_HF_URL = "https://hq.sinajs.cn/list="

# 新浪外汇 18 字段（2026-10-22 实测）：0 时间 1 买价 2 卖价 3 昨收
# 8 最新 10 涨跌幅(%) 11 涨跌额 17 日期
_FX_TIME, _FX_BID, _FX_ASK, _FX_PREV, _FX_LAST, _FX_PCT, _FX_CHG, _FX_DATE = (
    0, 1, 2, 3, 8, 10, 11, 17)
_FX_LINE_RE = re.compile(r'hq_str_([A-Za-z0-9_]+)="([^"]*)"')
# 新浪外盘期货 hf_*（2026-10-22 实测）：0 最新 2 买价 3 卖价 4 高 5 低
# 6 时间 7 昨结 8 开 9 持仓 12 日期 13 名称 14 人民币报价。
# ⚠️ 现货金/银（XAU/XAG）只有 14 个字段、没有第 14 列，所以门槛是 len>=14。
_HF_LAST, _HF_BID, _HF_ASK, _HF_HIGH, _HF_LOW = 0, 2, 3, 4, 5
_HF_TIME, _HF_PREV, _HF_OPEN, _HF_HOLD = 6, 7, 8, 9
_HF_DATE, _HF_NAME = 12, 13
_HF_LINE_RE = re.compile(r'hq_str_(hf_[A-Za-z0-9]+)="([^"]*)"')


def _cn_iso(date_s, time_s):
    """新浪给的是北京时间，拼成带 +08:00 的 ISO —— 调用方不需要自己猜时区。"""
    d = str(date_s or "").strip()
    t = str(time_s or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return None
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", t):
        return None
    return "%sT%s+08:00" % (d, t)


def fx_quotes(symbols: list[str]) -> dict:
    """批量汇率（新浪 fx_* 符号）。返回 {quotes: {symbol: {...}}, notes: []}。"""
    seen: list[str] = []
    for s in symbols:
        s = str(s or "").strip()
        if s and s not in seen:
            seen.append(s)
    if len(seen) > 50:
        raise ValueError("一次最多 50 个汇率符号")

    resp = _get(SINA_FX_URL + ",".join(seen), timeout=20,
                referer="https://finance.sina.com.cn")
    text = resp.content.decode("gbk", "ignore")
    raw = {m.group(1): m.group(2) for m in _FX_LINE_RE.finditer(text)}

    out: dict[str, dict] = {}
    errs: list[str] = []
    for sym in seen:
        body = raw.get(sym)
        if body is None:
            errs.append("%s: 未返回" % sym)
            continue
        f = body.split(",")
        if len(f) < 18:
            errs.append("%s: 字段不足（%d）" % (sym, len(f)))
            continue
        last = _num(f[_FX_LAST])
        if last is None or last <= 0:
            errs.append("%s: 最新价无效" % sym)
            continue
        out[sym] = {
            "symbol": sym,
            "last": last,
            "bid": _num(f[_FX_BID]),
            "ask": _num(f[_FX_ASK]),
            "prev_close": _num(f[_FX_PREV]),
            "change_pct": _num(f[_FX_PCT]),
            "change": _num(f[_FX_CHG]),
            "ts": _cn_iso(f[_FX_DATE], f[_FX_TIME]),
        }
    if not out:
        raise UpstreamError("汇率全部失败: %s"
                            % ("; ".join(errs)[:200] or "上游返回为空"))
    return {"quotes": out, "notes": errs}


def futures_quotes(symbols: list[str]) -> dict:
    """批量国际期货（新浪 hf_ 符号，如 XAU / OIL / CL）。

    涨跌幅按 `(最新 - 昨结) / 昨结`，与 akshare `futures_foreign_commodity_realtime`
    同口径（不同标的一次性拿回，调用方不必逐个请求）。
    """
    seen: list[str] = []
    for s in symbols:
        s = str(s or "").strip().upper()
        if s and s not in seen:
            seen.append(s)
    if len(seen) > 20:
        raise ValueError("一次最多 20 个期货符号")

    resp = _get(SINA_HF_URL + ",".join("hf_" + s for s in seen), timeout=20,
                referer="https://finance.sina.com.cn")
    text = resp.content.decode("gbk", "ignore")
    raw = {m.group(1): m.group(2) for m in _HF_LINE_RE.finditer(text)}

    out: dict[str, dict] = {}
    errs: list[str] = []
    for sym in seen:
        body = raw.get("hf_" + sym)
        if body is None:
            errs.append("%s: 未返回" % sym)
            continue
        f = body.split(",")
        if len(f) < 14:
            errs.append("%s: 字段不足（%d）" % (sym, len(f)))
            continue
        last = _num(f[_HF_LAST])
        if last is None:
            errs.append("%s: 最新价无效" % sym)
            continue
        prev = _num(f[_HF_PREV])
        out[sym] = {
            "symbol": sym,
            "name": str(f[_HF_NAME]).strip(),
            "last": last,
            "change_pct": ((last - prev) / prev * 100.0) if prev else None,
            "high": _num(f[_HF_HIGH]),
            "low": _num(f[_HF_LOW]),
            "open": _num(f[_HF_OPEN]),
            "prev_settle": prev,
            "bid": _num(f[_HF_BID]),
            "ask": _num(f[_HF_ASK]),
            "vol": _num(f[_HF_HOLD]),
            "ts": _cn_iso(f[_HF_DATE], f[_HF_TIME]),
        }
    if not out:
        raise UpstreamError("期货全部失败: %s"
                            % ("; ".join(errs)[:200] or "上游返回为空"))
    return {"quotes": out, "notes": errs}


# ---------------- 分时 / 分钟 K ----------------

def _tx_symbol_of_code(code: str) -> str:
    c = str(code or "").strip().lower()
    if c.startswith(("sh", "sz", "bj", "hk")):
        return c
    if re.fullmatch(r"\d{5}", c):
        return "hk" + c
    if c.startswith(("6", "9", "5")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c


def _is_hk_code(code: str) -> bool:
    return _tx_symbol_of_code(code).startswith("hk")


def _is_star_code(code: str) -> bool:
    c = str(code or "").strip().lower()
    if c.startswith(("sh", "sz")):
        c = c[2:]
    return c.startswith(("688", "689"))


def _tx_trend(symbol: str) -> dict:
    last = None
    for host in TX_TREND_URLS:
        try:
            resp = _get(host, params={"code": symbol}, timeout=15)
            j = resp.json()
            node = (j.get("data") or {}).get(symbol)
            if not isinstance(node, dict):
                last = "data 里没有 %s（%s）" % (symbol, str(j.get("msg"))[:40])
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
                pts.append({"t": p[0][:4], "price": _num(p[1]),
                            "volume": _num(p[2]) if len(p) > 2 else None,
                            "amount": _num(p[3]) if len(p) > 3 else None})
            if not pts:
                last = "解析后为空"
                continue
            return {"date": day, "points": pts}
        except Exception as exc:            # noqa: BLE001
            last = exc
    raise UpstreamError("分时失败 %s: %s" % (symbol, str(last)[:120]))


def _tx_mkline(symbol: str, period: str, count: int) -> list[dict]:
    last = None
    for host in TX_MKLINE_URLS:
        try:
            resp = _get(host, params={"param": "%s,%s,,,%d"
                                      % (symbol, period, int(count))},
                        timeout=15)
            j = resp.json()
            node = (j.get("data") or {}).get(symbol)
            rows = node.get(period) if isinstance(node, dict) else None
            if not isinstance(rows, list) or not rows:
                last = "%s 为空（%s）" % (period, str(j.get("msg"))[:40])
                continue
            out = []
            for r in rows:
                if not isinstance(r, list) or len(r) < 6:
                    continue
                out.append({"datetime": str(r[0]), "open": _num(r[1]),
                            "close": _num(r[2]), "high": _num(r[3]),
                            "low": _num(r[4]), "volume": _num(r[5])})
            if out:
                return out
        except Exception as exc:            # noqa: BLE001
            last = exc
    raise UpstreamError("分钟K失败 %s %s: %s"
                        % (symbol, period, str(last)[:120]))


def _vwap(points: list[dict], hk: bool, star: bool) -> float | None:
    amt = sum(p["amount"] for p in points
              if p.get("amount") and p.get("volume") is not None)
    if amt <= 0:
        closes = [p.get("price") if p.get("price") is not None
                  else p.get("close") for p in points]
        closes = [c for c in closes if c]
        return sum(closes) / len(closes) if closes else None
    if hk or star:
        vol = sum(p["volume"] for p in points if p.get("volume"))
    else:
        vol = sum(p["volume"] for p in points if p.get("volume")) * 100.0
    if vol <= 0:
        return None
    return amt / vol


def intraday(code: str, period: str = "m5", count: int = 320) -> dict:
    """日内数据：A 股 = 分钟 K（OHLC）+ 分时（真 VWAP）；港股只有分时。

    返回结构与本机旧的 `market_data.fetch_intraday` 逐字段一致。
    """
    code = str(code or "").strip()
    if not code:
        raise ValueError("code 不能为空")
    if period not in ("m1", "m5", "m15", "m30", "m60"):
        raise ValueError("period 只能是 m1/m5/m15/m30/m60")
    count = max(1, min(int(count or 320), 1000))
    symbol = _tx_symbol_of_code(code)
    hk, star = symbol.startswith("hk"), _is_star_code(code)

    bars = None
    src = None
    if not hk:
        try:
            bars = _tx_mkline(symbol, period, count)
            src = "tx_mkline_" + period
        except Exception:                   # noqa: BLE001
            bars = None
    points = None
    day = None
    tv = None
    try:
        tr = _tx_trend(symbol)
        points, day = tr["points"], tr["date"]
        tv = _vwap(points, hk, star)
    except Exception:                       # noqa: BLE001
        points = None
    if bars is None and points is None:
        raise UpstreamError("日内数据全部失败 %s（分钟K与分时都取不到）" % code)
    if bars is None:
        # 分时没有分钟内 OHLC，用成交价同时充当 O/H/L/C（会低估分钟振幅）
        bars = [{"datetime": "%s%s" % (day, p["t"]), "open": p["price"],
                 "close": p["price"], "high": p["price"], "low": p["price"],
                 "volume": p["volume"], "amount": p["amount"]}
                for p in points if p.get("price")]
        src = "tx_trend"
    vw = tv if tv is not None else _vwap(
        [{"close": b.get("close"), "volume": b.get("volume")} for b in bars],
        hk, star)
    return {"code": code, "date": day or (bars[-1]["datetime"][:8] if bars else None),
            "is_hk": hk, "bars": bars, "points": points,
            "vwap": vw,
            "vwap_is_true": tv is not None,
            "source": src, "has_ohlc": bool(src and src.startswith("tx_mkline"))}
