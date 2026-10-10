"""海外/港股市场观察：美日韩 + 港股 + A股的相对强弱与「资金抽离」提示。

背景（用户判断）：港股可以直接买美股、日经等标的，所以**当这些市场对港股/A股
形成吸引力时，资金会从港股抽离**。本模块把这个判断做成可复算的数字，而不是
留在脑子里。

## 数据源（全部 2026-10-06 实测，**别凭印象改**）

| 市场 | 源 | 符号 | 实测 |
|---|---|---|---|
| 纳斯达克/标普500/道琼斯 | 腾讯 `qt.gtimg.cn` | `usIXIC,usINX,usDJI` | ✅ 1 次请求取全 |
| 恒生/恒生科技/国企 | 腾讯 | `s_hkHSI,s_hkHSTECH,s_hkHSCEI` | ✅ |
| 上证/沪深300/中证500 | 腾讯 | `s_sh000001,s_sh000300,s_sh000905` | ✅ |
| 日经225 | 新浪 `hq.sinajs.cn` | `int_nikkei` | ✅ **仅现货**，无历史 |
| 道琼/纳指/标普/恒生 | 新浪 | `int_dji,int_nasdaq,int_sp500,int_hangseng` | ✅ 交叉校验用 |
| **KOSPI（韩国）** | — | — | ❌ **全部源都拿不到** |

### 踩过的坑（都是实测，不是推测）

1. **腾讯没有日韩符号**：`s_jpNI225` / `s_krKOSPI` / `jpNI225` / `krKOSPI` / `s_jpTOPIX`
   全部返回 `v_pv_none_match="1";`。日经只能走新浪 `int_nikkei`。
2. **KOSPI 真的取不到**（试过 4 条路径）：
   - `akshare.index_global_spot_em()`（push2 系，AGENTS.md 记着全封；
     **2026-10-05 偶然成功返回 56 行，2026-10-06 就 RemoteDisconnected** ——
     再次印证「一次成功不代表源可用」）
   - `hq.sinajs.cn/list=int_kospi` 等 10 个符号穷举 → 全空
   - `akshare.index_global_name_table()` 里有「首尔综合指数 / KOSPI」，但
     `index_global_hist_sina(symbol="KOSPI")` 抛 `KeyError: 'KOSPI'` ——
     名字表有代码 ≠ 历史接口支持
   - `stock.finance.sina.com.cn` K 线接口 → `Service not valid`
   → 本模块**明确把 KOSPI 标为不可用**，不用任何代理指标冒充。
3. **腾讯字段布局按市场前缀不同**（最容易写错的地方）：
   - `us*`：`[3]`=现价 `[4]`=昨收 `[5]`=今开 **`[32]`=涨跌幅**
   - `s_*`（港/A）：`[3]`=现价 `[4]`=涨跌额 **`[5]`=涨跌幅**
   写错就会把「涨跌额」当成「涨跌幅」——不会报错，只会静默给出错的数。
4. **日K 要去掉 `s_` 前缀**：`s_hkHSI` 返回 0 根，`hkHSI` 返回 30 根。
5. **美股日K 拿不到**：`usIXIC` 只返回 1 根 → 美股/日经/韩国只能算**当日**涨跌，
   算不了 5 日/20 日。模块里如实区分，不假装有。

请求预算：现货 **2 次**（腾讯批量 + 新浪批量）；历史最多 6 次（仅 A/港）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import requests

TENCENT_URL = "https://qt.gtimg.cn/q={}"
SINA_URL = "https://hq.sinajs.cn/list={}"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---- 符号表（源可用性已实测，别加没验过的符号）----
TENCENT_SPOT = {
    "usIXIC": ("纳斯达克", "US"), "usINX": ("标普500", "US"), "usDJI": ("道琼斯", "US"),
    "s_hkHSI": ("恒生指数", "HK"), "s_hkHSTECH": ("恒生科技", "HK"),
    "s_hkHSCEI": ("国企指数", "HK"),
    "s_sh000001": ("上证指数", "CN"), "s_sh000300": ("沪深300", "CN"),
    "s_sh000905": ("中证500", "CN"),
}
SINA_SPOT = {
    "int_nikkei": ("日经225", "JP"), "int_dji": ("道琼斯", "US"),
    "int_nasdaq": ("纳斯达克", "US"), "int_sp500": ("标普500", "US"),
    "int_hangseng": ("恒生指数", "HK"),
}
# 只能算当日涨跌（无历史源）的市场
SPOT_ONLY = {"US", "JP", "KR"}
# 有日K 的（腾讯，去 s_ 前缀）
KLINE_SYMBOLS = {
    "s_sh000001": "sh000001", "s_sh000300": "sh000300", "s_sh000905": "sh000905",
    "s_hkHSI": "hkHSI", "s_hkHSTECH": "hkHSTECH", "s_hkHSCEI": "hkHSCEI",
}
# 明确取不到的（要在报告里如实说明，不能用代理指标冒充）
UNAVAILABLE = {
    "KOSPI(韩国)": "push2 被封 / 新浪 int_* 全空 / akshare 历史 KeyError（2026-10-06 实测三种路径）",
}

# 资金抽离判定阈值（相对强弱，百分点）
DRAIN_HK_VS_US = 1.0     # 港股跑输美股多少个百分点算「抽离」
DRAIN_HK_VS_JP = 0.8
DRAIN_CN_VS_HK = 0.8


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_tencent_line(line: str) -> dict | None:
    """解析腾讯一行。**涨跌幅的位置按前缀不同**，见模块 docstring 坑 3。"""
    m = re.match(r'v_([^=]+)="(.*)"', line.strip().rstrip(";"))
    if not m:
        return None
    sym, body = m.group(1), m.group(2)
    f = body.split("~")
    if len(f) < 6:
        return None
    price = _f(f[3])
    if price is None:
        return None
    if sym.startswith("us"):
        prev = _f(f[4])          # 昨收
        pct = _f(f[32]) if len(f) > 32 else None
    else:
        pct = _f(f[5])           # 港/A：第 6 段就是涨跌幅
        prev = None
    if pct is None and prev:
        pct = round((price - prev) / prev * 100, 2)
    meta = TENCENT_SPOT.get(sym, (sym, "?"))
    return {"symbol": sym, "name": meta[0], "market": meta[1],
            "price": price, "pct": pct, "source": "tencent"}


def _parse_sina_line(line: str) -> dict | None:
    """解析新浪 `var hq_str_int_x="名称,价,涨跌额,涨跌幅%";` 一行。"""
    m = re.match(r'var hq_str_([^=]+)="(.*)";', line.strip())
    if not m:
        return None
    sym, body = m.group(1), m.group(2)
    if not body:
        return None
    parts = body.split(",")
    if len(parts) < 4:
        return None
    name = parts[0]
    price = _f(parts[1])
    pct = _f(parts[3])
    if price is None:
        return None
    meta = SINA_SPOT.get(sym, (name, "?"))
    return {"symbol": sym, "name": meta[0] or name, "market": meta[1],
            "price": price, "pct": pct, "source": "sina"}


def fetch_spot(timeout: int = 15) -> dict:
    """取全部现货。**2 次请求**（腾讯批量 + 新浪批量）。失败的市场如实标 unavailable。"""
    out = {"ts": datetime.now().isoformat(timespec="seconds"),
           "indices": {}, "errors": []}

    syms = ",".join(TENCENT_SPOT)
    try:
        r = requests.get(TENCENT_URL.format(syms), headers={"User-Agent": UA}, timeout=timeout)
        r.encoding = "gbk"
        got = 0
        for line in r.text.split(";"):
            if not line.strip():
                continue
            row = _parse_tencent_line(line)
            if row:
                out["indices"][row["symbol"]] = row
                got += 1
        if got < len(TENCENT_SPOT):
            out["errors"].append(f"tencent 只解析到 {got}/{len(TENCENT_SPOT)} 个指数")
    except Exception as exc:
        out["errors"].append(f"tencent 现货失败: {exc}")

    syms = ",".join(SINA_SPOT)
    try:
        r = requests.get(SINA_URL.format(syms),
                         headers={"Referer": "https://finance.sina.com.cn",
                                  "User-Agent": UA}, timeout=timeout)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if not line.strip():
                continue
            row = _parse_sina_line(line)
            if row and row["market"] == "JP":     # 美股/港股已有腾讯源，新浪只补日经
                out["indices"][row["symbol"]] = row
    except Exception as exc:
        out["errors"].append(f"sina 现货失败: {exc}")

    out["unavailable"] = UNAVAILABLE
    return out


def fetch_kline_history(days: int = 30, timeout: int = 15) -> dict:
    """取 A股/港股日K（最多 6 次请求），用于 5 日/20 日涨跌。

    美股/日经/韩国**没有可用历史源**，不在这里出现 —— 由调用方按
    `SPOT_ONLY` 区分「只有当日」和「有区间涨跌」。
    """
    out = {}
    for sym, ksym in KLINE_SYMBOLS.items():
        try:
            j = requests.get(TENCENT_KLINE,
                             params={"param": f"{ksym},day,,,{days},qfq"},
                             headers={"User-Agent": UA}, timeout=timeout).json()
            node = (j.get("data") or {}).get(ksym, {})
            bars = node.get("qfqday") or node.get("day") or []
            closes = [_f(b[2]) for b in bars if len(b) > 2]
            closes = [c for c in closes if c is not None]
            if len(closes) < 6:
                out[sym] = {"ok": False, "bars": len(closes)}
                continue
            last = closes[-1]
            out[sym] = {
                "ok": True, "bars": len(closes), "last": last,
                "chg5": round((last / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None,
                "chg20": round((last / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
            }
        except Exception as exc:
            out[sym] = {"ok": False, "error": str(exc)[:80]}
    return out


def find_drain_signals(spot: dict, hist: dict | None = None) -> list:
    """判定资金抽离信号。**只给数字对照，不下预测结论。**

    信号定义（阈值见 DRAIN_* 常量）：
    - 港股当日跑输美股 >= 1.0pp
    - 港股当日跑输日经 >= 0.8pp
    - A股（上证）当日跑输港股 >= 0.8pp
    """
    idx = spot.get("indices") or {}

    def pct(sym):
        return (idx.get(sym) or {}).get("pct")

    hk, us, jp, cn = pct("s_hkHSI"), pct("usIXIC"), pct("int_nikkei"), pct("s_sh000001")
    sig = []
    if None not in (hk, us) and hk - us <= -DRAIN_HK_VS_US:
        sig.append({"type": "hk_vs_us", "text":
                    f"恒生 {hk:+.2f}% 跑输纳斯达克 {us:+.2f}%（{us-hk:.2f}pp），"
                    f"港股相对吸引力下降", "gap_pp": round(us - hk, 2)})
    if None not in (hk, jp) and hk - jp <= -DRAIN_HK_VS_JP:
        sig.append({"type": "hk_vs_jp", "text":
                    f"恒生 {hk:+.2f}% 跑输日经225 {jp:+.2f}%（{jp-hk:.2f}pp）",
                    "gap_pp": round(jp - hk, 2)})
    if None not in (cn, hk) and cn - hk <= -DRAIN_CN_VS_HK:
        sig.append({"type": "cn_vs_hk", "text":
                    f"上证 {cn:+.2f}% 跑输恒生 {hk:+.2f}%（{hk-cn:.2f}pp）",
                    "gap_pp": round(hk - cn, 2)})
    return sig


def render(spot: dict, hist: dict | None = None) -> str:
    L = ["【海外市场观察 · 资金抽离视角】"]
    L.append(f"（{spot.get('ts','')[:16]}，现货 2 次请求；括号内为当日涨跌）")

    order = ["usIXIC", "usINX", "usDJI", "int_nikkei", "s_hkHSI", "s_hkHSTECH",
             "s_sh000001", "s_sh000300", "s_sh000905"]
    idx = spot.get("indices") or {}
    rows = []
    for sym in order:
        row = idx.get(sym)
        if not row:
            continue
        p = row.get("pct")
        tag = "（仅当日）" if row.get("market") in SPOT_ONLY else ""
        h = (hist or {}).get(sym) or {}
        extra = ""
        if h.get("ok"):
            bits = []
            if h.get("chg5") is not None:
                bits.append(f"5日{h['chg5']:+.1f}%")
            if h.get("chg20") is not None:
                bits.append(f"20日{h['chg20']:+.1f}%")
            extra = "  " + " ".join(bits) if bits else ""
        pct_txt = f"{p:+.2f}%" if p is not None else "涨跌缺失"
        rows.append(f"  {row['name']:<10} {row['price']:>12,.2f}   {pct_txt}{extra} {tag}")
    L.extend(rows)

    un = spot.get("unavailable") or {}
    if un:
        L.append("")
        for k, why in un.items():
            L.append(f"  ⚠ {k}：**取不到**（{why}）—— 不用其它指标冒充")

    sig = find_drain_signals(spot, hist)
    L.append("")
    if sig:
        L.append("  资金抽离信号（当日，仅数字对照）：")
        for s in sig:
            L.append(f"    · {s['text']}")
    else:
        L.append("  资金抽离信号：当日无触发（港股未显著跑输美股/日经，A 股未跑输港股）")

    if spot.get("errors"):
        L.append("")
        for e in spot["errors"]:
            L.append(f"  ⚠ {e}")
    return "\n".join(L)


def append_snapshot(snapshot: dict, data_dir: str = "") -> str:
    try:
        base = data_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"global_markets_{datetime.now():%Y%m%d}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception as exc:
        print(f"[global_markets] 快照写入失败: {exc}", flush=True)
        return ""


def check_once(days: int = 30) -> dict:
    """一次完整采集：现货 + 历史 + 信号。"""
    spot = fetch_spot()
    hist = fetch_kline_history(days=days)
    sig = find_drain_signals(spot, hist)
    return {"ts": spot.get("ts"), "spot": spot, "hist": hist, "signals": sig}


if __name__ == "__main__":
    import time
    t0 = time.time()
    r = check_once()
    print(render(r["spot"], r["hist"]))
    print(f"\n耗时 {time.time()-t0:.1f}s")
    print(f"快照: {append_snapshot(r)}")
