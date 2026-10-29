"""板块数据服务 HTTP 接口。

stock-advisor 只需把原来的 sector.fetch_all_boards() 换成调这里的
`/boards`，其余评分/存库/前端逻辑不用动。

端点
----
GET  /health                 服务与各数据源健康度、请求量预算
GET  /boards                 全量板块快照（已算好评分、按分降序）
     ?include_concept=false  只取行业（省 9 个请求）
     &include_aux=false      不用辅助源（最省，只剩同花顺）
     &with_momentum=true      附带动量分（会多打很多请求，默认关）
     &limit=N                 只返回前 N（默认全量，不截断）
GET  /boards/{code}/constituents  板块成分股
GET  /zt-pool                涨停池
GET  /history/{name}         板块指数历史日线
POST /collect                强制立刻采集一轮并返回

行情代理（2026-10-22 新增：本机不再直连任何行情源）
GET  /kline                  日 K（东财 CSV 口径，东财失败自动退腾讯）
GET  /quote                  实时行情（A 股腾讯 / 港股新浪）
GET  /intraday               分时 + 分钟 K（做 T 用）
GET  /fx                     汇率（新浪 fx_*，一次批量）
GET  /futures                国际期货（新浪 hf_*，如 XAU / OIL / CL）

估值代理（2026-10-28 新增：市场温度计不再在本机直连 akshare）
GET  /sw-calendar            申万公布过的月末交易日
GET  /sw-month               某月末的申万行业 PE/收盘（一级行业 + 市场表征）
GET  /sw-day                 某个交易日的申万行业 PE/收盘
GET  /index-hist             中证指数日线（收盘 + 滚动市盈率），精简行
"""
from __future__ import annotations

import os
import re
import threading
import time

from fastapi import FastAPI, HTTPException, Query

from . import __version__
from . import aggregate as A

app = FastAPI(title="SA 板块数据服务", version=__version__,
              description="多厂商板块行情服务，替代被 WAF 掐断的东财直连采集器")

#: 本服务只认同花顺体系的板块 code。实测（见 README「code 体系陷阱」）：
#: 行业 881xxx -> adata 直通；概念要 886xxx；akshare 口径的概念 3xxxxx 直喂 0/6 全败。
#: 东财的 BKxxxx 是**完全不同的第三套体系**，adata 拿到它会在解析时抛
#: ``ValueError('substring not found')`` —— 那本来是「调用方传错了」，却以 502
#: 「成分股接口异常」的形式冒出来，看起来像上游挂了，很难排查。
#: 所以在打 adata 之前就按体系拦掉，回可读的 4xx。
THS_BOARD_CODE = re.compile(r"(?:881|886)\d{3}")

# 交易日 / 数据日：collect 时写在这里，供 /health 暴露
# （调用方在休市日需要知道该按哪一天入库）
_market_clock: dict = {"is_trading_day": None, "data_date": None, "at": None}

_lock = threading.Lock()
_cache: dict = {"snap": None, "at": 0.0}
CACHE_TTL = float(os.environ.get("SA_CACHE_TTL", "60"))


def _get_snap(force: bool = False, **kw) -> dict:
    """带 TTL 的快照缓存，避免前端每次刷新都打源。"""
    now = time.time()
    with _lock:
        fresh = (_cache["snap"] is not None
                 and (now - _cache["at"]) < CACHE_TTL)
        if fresh and not force:
            return _cache["snap"]
    snap = A.collect(**kw)
    d = snap.to_dict()
    with _lock:
        _cache["snap"] = d
        _cache["at"] = time.time()
        _market_clock.update({"is_trading_day": d.get("is_trading_day"),
                              "data_date": d.get("data_date"),
                              "at": d.get("collected_at")})
    return d


@app.get("/")
def root() -> dict:
    return {"service": "sa-data-service", "version": __version__,
            "endpoints": ["/health", "/boards",
                          "/boards/{code}/constituents",
                          "/zt-pool", "/history/{name}", "/collect",
                          "/kline", "/quote", "/intraday", "/fx", "/futures",
                          "/sw-calendar", "/sw-month", "/sw-day", "/index-hist",
                          "/financials/report", "/financials/preannounce",
                          "/financials/express", "/financials/consensus",
                          "/financials/abstract", "/financials/bundle"]}


@app.get("/health")
def health() -> dict:
    snap = _cache["snap"]
    return {
        **A.health(),
        "cached": snap is not None,
        "cache_age_sec": round(time.time() - _cache["at"], 1) if snap else None,
        "last_degraded": (snap or {}).get("degraded"),
        "last_notes": (snap or {}).get("notes", []),
        "last_cross_check": (snap or {}).get("cross_check"),
        # 交易日感知：休市日 data_date != trade_date，调用方按 data_date 入库
        "is_trading_day": (snap or {}).get("is_trading_day"),
        "data_date": (snap or {}).get("data_date") or _market_clock["data_date"],
        "collected_at": _market_clock["at"],
    }


@app.get("/boards")
def boards(include_concept: bool = Query(True),
           include_aux: bool = Query(True),
           include_sina: bool = Query(True),
           with_momentum: bool = Query(False),
           momentum_limit: int = Query(30, ge=0, le=200),
           limit: int = Query(0, ge=0),
           refresh: bool = Query(False)) -> dict:
    """全量板块快照 + 轮动评分。默认不截断。"""
    snap = _get_snap(force=refresh, include_concept=include_concept,
                     include_aux=include_aux, include_sina=include_sina)

    momentum: dict[str, float] = {}
    notes = list(snap.get("notes", []))
    if with_momentum and momentum_limit:
        ordered = sorted(snap["boards"], key=lambda b: -(b.get("pct") or -99))
        momentum = A.build_momentum([b["name"] for b in ordered],
                                    limit=momentum_limit)
        notes.append(f"动量分只算了前 {momentum_limit} 个板块"
                     f"（d.10jqka 有日预算）")

    rows = A.score_boards([dict(b) for b in snap["boards"]],
                          zt=snap.get("zt"), momentum=momentum)
    out = {
        "collected_at": snap["collected_at"],
        "trade_date": snap["trade_date"],
        # 休市日 trade_date 是今天，data_date 是真实数据日。
        # 调用方入库请用 data_date，否则历史表会写进错标日期的行。
        "is_trading_day": snap.get("is_trading_day"),
        "data_date": snap.get("data_date") or snap["trade_date"],
        "total_boards": len(rows),
        "boards": rows[:limit] if limit else rows,
        "zt": snap.get("zt", {}),
        "sources_used": snap.get("sources_used", []),
        "sources_failed": snap.get("sources_failed", []),
        "degraded": snap.get("degraded", False),
        "notes": notes,
        "meter": snap.get("meter", {}),
    }
    if snap.get("cross_check"):
        out["cross_check"] = snap["cross_check"]
    return out


@app.get("/zt-pool")
def zt_pool() -> dict:
    from .sources import ZT
    try:
        return ZT.fetch()
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(503, f"涨停池不可用: {exc}") from exc


@app.get("/history/{board_name}")
def history(board_name: str, days: int = Query(30, ge=5, le=250)) -> dict:
    from .sources import THS, SourceError
    try:
        rows = THS.index_history(board_name, days=days)
    except SourceError as exc:
        raise HTTPException(429, str(exc)) from exc
    if not rows:
        raise HTTPException(404, f"板块 {board_name} 无历史数据")
    return {"name": board_name, "days": len(rows), "rows": rows}


@app.get("/boards/{board_code}/constituents")
def constituents(board_code: str, limit: int = Query(50, ge=1, le=500)) -> dict:
    """板块成分股。

    实测的 code 体系陷阱（务必读 README）：
      - 行业 881xxx  -> adata.stock.info.concept_constituent_ths(index_code=)
                        直通，实测 6/6
      - 概念 3xxxxx  -> 直喂 adata **0/6 全败**，它要 886xxx
    所以对 3xxxxx 明确返回可读错误，而不是静默给 0 行。

    东财 BKxxxx 是第三套体系，本服务一律不接（见 THS_BOARD_CODE 的注释）：
    放它进来只会在 adata 内部炸成 `substring not found` -> 假 502。
    """
    code = (board_code or "").strip()
    if not THS_BOARD_CODE.fullmatch(code):
        raise HTTPException(
            404,
            f"板块 code {board_code!r} 不是同花顺体系，本接口只接受 881xxx（行业）"
            f"或 886xxx（概念）。BKxxxx 是东财体系、3xxxxx 是 akshare 概念口径，"
            f"两者都不适用。")
    try:
        import adata
    except ImportError as exc:
        raise HTTPException(503, f"adata 未安装: {exc}") from exc
    try:
        df = adata.stock.info.concept_constituent_ths(index_code=code)
    except Exception as exc:                            # noqa: BLE001
        raise HTTPException(502, f"成分股接口异常: {exc}") from exc
    rows = list(df.itertuples(index=False)) if len(df) else []
    if not rows:
        # 881xxx/886xxx 体系但没数据 —— 是真的没成分股或 code 不存在。
        # （3xxxxx 的那条提示已上移到 THS_BOARD_CODE 守卫里，这里不再重复。）
        raise HTTPException(404, f"板块 {board_code} 无成分股或代码不存在")
    return {"code": board_code, "count": len(rows),
            "items": [{"code": r.stock_code, "name": r.short_name}
                      for r in rows[:limit]]}


@app.post("/collect")
def force_collect(include_concept: bool = Query(True),
                  include_aux: bool = Query(True)) -> dict:
    snap = _get_snap(force=True, include_concept=include_concept,
                     include_aux=include_aux)
    rows = A.score_boards([dict(b) for b in snap["boards"]],
                          zt=snap.get("zt"))
    return {"collected_at": snap["collected_at"],
            "total_boards": len(rows),
            "top10": [{"name": b["name"], "score": b["score"]}
                      for b in rows[:10]],
            "degraded": snap["degraded"],
            "notes": snap["notes"]}


# ---------------- 行情代理（本机只调这里，不直连行情源） ----------------

@app.get("/kline")
def kline(secid: str = Query(..., description="东财 secid，如 1.600519 / 116.02513"),
          days: int = Query(60, ge=1, le=2000),
          fields: str = Query("f51,f52,f53,f54,f55,f56,f57"),
          source: str = Query("auto", pattern="^(auto|em|tx)$"),
          beg: str = Query(""), end: str = Query("")) -> dict:
    """日 K。返回东财 CSV 口径的 klines，调用方按 fields 的位置解析。

    东财（push2his）在 101 上实测可用，失败自动退腾讯 newfqkline。
    """
    from . import market as M
    try:
        return M.kline(secid, days=days, fields=fields,
                       source=source, beg=beg, end=end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except M.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/quote")
def quote(symbols: str = Query(..., description="逗号分隔腾讯符号，如 sh600519,hk00100")) -> dict:
    """批量实时行情。返回 {quotes: {symbol: {...}}, notes: []}。"""
    from . import market as M
    lst = [s.strip() for s in str(symbols or "").split(",") if s.strip()]
    if not lst:
        raise HTTPException(400, "symbols 不能为空")
    try:
        return M.quotes(lst)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/intraday")
def intraday(code: str = Query(..., description="600519 / 00100 / sh600519"),
             period: str = Query("m5"), count: int = Query(320, ge=1, le=1000)) -> dict:
    """分时 + 分钟 K。结构与旧 market_data.fetch_intraday 逐字段一致。"""
    from . import market as M
    try:
        return M.intraday(code, period=period, count=count)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except M.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/fx")
def fx(symbols: str = Query(...,
                            description="逗号分隔新浪外汇符号，如 fx_susdcny,fx_susdcnh")) -> dict:
    """批量汇率。返回 {quotes: {symbol: {...}}, notes: []}。"""
    from . import market as M
    lst = [s.strip() for s in str(symbols or "").split(",") if s.strip()]
    if not lst:
        raise HTTPException(400, "symbols 不能为空")
    try:
        return M.fx_quotes(lst)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except M.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/futures")
def futures(symbols: str = Query(...,
                                 description="逗号分隔新浪外盘代码，如 XAU,OIL,CL,HG")) -> dict:
    """批量国际期货。返回 {quotes: {symbol: {...}}, notes: []}。"""
    from . import market as M
    lst = [s.strip() for s in str(symbols or "").split(",") if s.strip()]
    if not lst:
        raise HTTPException(400, "symbols 不能为空")
    try:
        return M.futures_quotes(lst)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except M.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/sw-calendar")
def sw_calendar() -> dict:
    """申万公布过的月末交易日。返回 {dates: ['2016-12-30', ...]}。"""
    from . import valuation as V
    try:
        return V.sw_calendar()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except V.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/sw-month")
def sw_month(date: str = Query(..., description="月末交易日，YYYY-MM-DD 或 YYYYMMDD")) -> dict:
    """某月末的申万行业 PE/收盘。返回 {date, items: {code: {pe, close, name}}, notes}。

    不做标的过滤（过滤权在调用方的 UNIVERSE，加标的不用重建镜像）。
    """
    from . import valuation as V
    try:
        return V.sw_month(date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except V.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/sw-day")
def sw_day(date: str = Query(..., description="交易日，YYYY-MM-DD 或 YYYYMMDD")) -> dict:
    """某个交易日的申万行业 PE/收盘。结构与 /sw-month 一致。"""
    from . import valuation as V
    try:
        return V.sw_day(date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except V.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/index-hist")
def index_hist(code: str = Query(..., description="6 位中证指数代码，如 000300 / 931573"),
               start: str = Query(..., description="起，YYYY-MM-DD 或 YYYYMMDD"),
               end: str = Query(..., description="止，YYYY-MM-DD 或 YYYYMMDD")) -> dict:
    """中证指数日线（精简）。返回 {code, start, end, rows: [{d, close, pe}]}。"""
    from . import valuation as V
    try:
        return V.index_hist(code, start, end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except V.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


# ---------------- 财务代理（event_risk：披露前冻结预期 / 披露后核对实际） ----------------

@app.get("/financials/report")
def fin_report(period: str = Query(..., description="季末报告期，如 20260930"),
               codes: str = Query("", description="逗号分隔 6 位代码，留空=全表")) -> dict:
    """业绩报表（**已披露**的正式值，归母口径，单位元）。

    ⚠️ 只有已披露的公司才在表里：**查不到 = 还没披露，不等于亏损或没利润**。
    """
    from . import financials as F
    try:
        return F.report(period, codes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except F.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/financials/preannounce")
def fin_preannounce(period: str = Query(..., description="季末报告期，如 20260930"),
                    codes: str = Query("", description="逗号分隔 6 位代码")) -> dict:
    """业绩预告。`profit_mid` 是**预告区间中值**，不是实际值（也未审计）。"""
    from . import financials as F
    try:
        return F.preannounce(period, codes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except F.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/financials/express")
def fin_express(period: str = Query(..., description="季末报告期，如 20260930"),
                codes: str = Query("", description="逗号分隔 6 位代码")) -> dict:
    """业绩快报（比正式报表早，**未经审计**）。"""
    from . import financials as F
    try:
        return F.express(period, codes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except F.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/financials/consensus")
def fin_consensus(codes: str = Query("", description="逗号分隔 6 位代码")) -> dict:
    """机构一致预期 —— **只有年度 EPS，没有季度共识**（响应 notes 里也这么说）。"""
    from . import financials as F
    try:
        return F.consensus(codes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except F.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/financials/abstract")
def fin_abstract(code: str = Query(..., description="6 位代码，如 300394")) -> dict:
    """单只票的历史财务序列（常用指标，单位为原始单位）。"""
    from . import financials as F
    try:
        return F.abstract(code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except F.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/financials/bundle")
def fin_bundle(code: str = Query(..., description="6 位代码，如 300394"),
               period: str = Query(..., description="季末报告期，如 20260930")) -> dict:
    """一次拿齐一只票的 report/preannounce/express/consensus/abstract。

    各子块独立失败：缺的那块是 null，原因在 `errors` 里 —— 一个源挂了不该让
    整只票的预案变成空白。
    """
    from . import financials as F
    try:
        return F.bundle(code, period)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except F.UpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc
