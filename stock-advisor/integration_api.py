"""外部研究引擎（Vibe-Trading）的 REST 门面。

路径统一挂在 ``/api/integrations/vibe/*``，前端只认这一层，
上游改端口/路径时只动 ``contracts/vibe.json`` 和 ``VIBE_BASE_URL``。
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from integrations import base
from integrations.tsp import TspIntegration, get_tsp, tsp_enabled
from integrations.vibe import VibeIntegration, get_vibe, vibe_enabled

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

_ALPHA_ID_RE = re.compile(r"^[a-z][a-z0-9]+_[a-z0-9_]{1,64}$")
_VALID_ZOOS = {"alpha101", "gtja191", "qlib158", "academic", "fundamental"}
# 研究范围只覆盖 A 股与港股。上游 `/alpha/list` 的 universe 用市场名，
# csi300 / hk 作为更直观的别名一并收下；bench 仍只认 csi300 —— 0.1.18 的
# 基准域只有 csi300 / sp500 / btc-usdt，没有港股基准，别在这里假装支持。
_MARKET_SCOPE = "cn_hk"
_BENCH_UNIVERSES = {"csi300"}
_ALPHA_UNIVERSE_ALIASES = {
    "csi300": "equity_cn",
    "equity_cn": "equity_cn",
    "hk": "equity_hk",
    "equity_hk": "equity_hk",
}
# 与 A 股/港股数据面无关的上游模板不往本站透出（加密/商品/信用/宏观汇率/全球混合）。
# 只做展示过滤，不改上游；要放开删掉对应名字即可。
_CN_HK_PRESET_DENYLIST = {
    "crypto_research_lab",
    "crypto_trading_desk",
    "commodity_research_team",
    "credit_research_team",
    "macro_rates_fx_desk",
    "global_equities_desk",
    "global_allocation_committee",
}
_PERIOD_RE = re.compile(r"^(\d{4}|\d{4}-\d{2}-\d{2})[/-](\d{4}|\d{4}-\d{2}-\d{2})$")


# ---------------------------------------------------------------- 工具

def _client():
    if not vibe_enabled():
        raise HTTPException(
            status_code=409,
            detail={"code": "disabled", "message": "Vibe 接入已被 SA_VIBE_DISABLED 关闭"},
        )
    try:
        return get_vibe()
    except base.IntegrationError as e:
        raise HTTPException(status_code=e.status, detail=e.as_dict())


def _fail(e: base.IntegrationError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.as_dict())


def _tsp():
    if not tsp_enabled():
        raise HTTPException(
            status_code=409,
            detail={"code": "disabled", "message": "TSP 接入已被 SA_TSP_DISABLED 关闭"},
        )
    try:
        return get_tsp()
    except base.IntegrationError as e:
        raise _fail(e)


def _call(capability: str, **params) -> dict:
    cli = _client()
    try:
        res = cli.call(capability, **params)
    except base.IntegrationError as e:
        raise _fail(e)
    return {
        "ok": True,
        "data": res["data"],
        "meta": {
            "capability": res["capability"],
            "elapsed_ms": res["elapsed_ms"],
            "upstream_version": res["upstream_version"],
        },
    }


# ---------------------------------------------------------------- 元信息

@router.get("")
def list_integrations() -> dict:
    items = [
        {
            "name": "vibe",
            "title": "Vibe-Trading",
            "upstream": "HKUDS/Vibe-Trading",
            "kind": "rest",
            "enabled": vibe_enabled(),
            "market_scope": _MARKET_SCOPE,
            "alpha_universes": ["equity_cn", "equity_hk"],
            "bench_universe": "csi300",
            "bench_note": "上游 0.1.18 无港股基准，bench 仅 A 股 csi300",
            "base_url": os.environ.get("VIBE_BASE_URL") or VibeIntegration.default_base_url,
        },
        {
            "name": "tsp",
            "title": "tick-stock-panel",
            "upstream": "shy3130/tick-stock-panel",
            "kind": "rest",
            "enabled": tsp_enabled(),
            "market_scope": "cn",
            "capabilities": ["kline.daily"],
            "base_url": os.environ.get("TSP_BASE_URL") or TspIntegration.default_base_url,
            "note": "只读行情/指标；不接 write:ext 与 paper:trade",
        },
    ]
    return {"ok": True, "data": items}


def _filter_presets(rows: list) -> list:
    """滤掉与 A 股/港股数据面无关的上游模板（只做展示过滤，不改上游）。"""
    return [
        p
        for p in rows
        if isinstance(p, dict) and p.get("name") not in _CN_HK_PRESET_DENYLIST
    ]


def _scope() -> dict:
    return {
        "market_scope": _MARKET_SCOPE,
        "alpha_universes": ["equity_cn", "equity_hk"],
        "bench_universes": sorted(_BENCH_UNIVERSES),
        "bench_note": "上游 0.1.18 无港股基准，bench 仅 A 股 csi300",
    }


@router.get("/vibe/summary")
def vibe_summary() -> dict:
    cli = _client()
    try:
        data = cli.summary()
    except base.IntegrationError as e:
        raise _fail(e)
    # summary 里也带一份模板列表；前端优先用它渲染，所以这里同样要滤。
    blk = (data.get("blocks") or {}).get("swarm_presets")
    if isinstance(blk, dict) and isinstance(blk.get("data"), list):
        rows = blk["data"]
        blk["data"] = _filter_presets(rows)
        blk["filtered_out"] = len(rows) - len(blk["data"])
    data["scope"] = _scope()
    return {"ok": True, "data": data}


@router.get("/vibe/health")
def vibe_health() -> dict:
    cli = _client()
    return {"ok": True, "data": cli.health()}


@router.get("/vibe/capabilities")
def vibe_capabilities() -> dict:
    cli = _client()
    return {
        "ok": True,
        "data": {
            "upstream": cli.contract.upstream,
            "tested_version": cli.contract.tested_version,
            "notes": cli.contract.notes,
            "capabilities": cli.capabilities(),
        },
    }


class ProbeIn(BaseModel):
    only: list[str] | None = Field(default=None, max_length=50)


@router.post("/vibe/probe")
def vibe_probe(body: ProbeIn | None = None) -> dict:
    cli = _client()
    try:
        data = cli.probe(only=(body.only if body else None))
    except base.IntegrationError as e:
        raise _fail(e)
    return {"ok": True, "data": data}


# ---------------------------------------------------------------- TSP

@router.get("/tsp/health")
def tsp_health() -> dict:
    return {"ok": True, "data": _tsp().health()}


@router.get("/tsp/capabilities")
def tsp_capabilities() -> dict:
    cli = _tsp()
    return {
        "ok": True,
        "data": {
            "upstream": cli.contract.upstream,
            "tested_version": cli.contract.tested_version,
            "notes": cli.contract.notes,
            "capabilities": cli.capabilities(),
        },
    }


@router.get("/tsp/kline")
def tsp_kline(
    symbol: str = Query(..., max_length=32),
    limit: int = Query(60, ge=1, le=500),
) -> dict:
    if not re.fullmatch(r"[0-9A-Za-z.\-]{3,32}", symbol):
        raise HTTPException(400, detail={"code": "bad_param", "message": "symbol 不合法"})
    cli = _tsp()
    try:
        res = cli.daily(symbol=symbol, limit=limit)
    except base.IntegrationError as e:
        raise _fail(e)
    return {
        "ok": True,
        "data": res["data"],
        "meta": {
            "capability": res["capability"],
            "elapsed_ms": res["elapsed_ms"],
            "upstream_version": res["upstream_version"],
        },
    }


# ---------------------------------------------------------------- 因子

@router.get("/vibe/alphas")
def vibe_alphas(
    zoo: str | None = Query(None, max_length=32),
    theme: str | None = Query(None, max_length=32),
    universe: str | None = Query(None, max_length=32),
    limit: int | None = Query(None, ge=1, le=500),
) -> dict:
    if zoo and zoo not in _VALID_ZOOS:
        raise HTTPException(400, detail={"code": "bad_param", "message": f"未知 zoo：{zoo}"})
    # 不给 universe 时默认按 A 股筛，避免把美股/加密因子混进来；
    # 港股显式传 universe=equity_hk（或别名 hk）。
    u = (universe or "equity_cn").strip().lower()
    if u not in _ALPHA_UNIVERSE_ALIASES:
        raise HTTPException(
            400,
            detail={
                "code": "bad_param",
                "message": f"研究范围仅 A 股与港股，universe 支持 csi300 / equity_cn / equity_hk / hk，收到：{universe}",
            },
        )
    return _call(
        "alpha.list",
        zoo=zoo,
        theme=theme,
        universe=_ALPHA_UNIVERSE_ALIASES[u],
        limit=limit,
    )


@router.get("/vibe/alphas/{alpha_id}")
def vibe_alpha(alpha_id: str) -> dict:
    if not _ALPHA_ID_RE.fullmatch(alpha_id):
        raise HTTPException(400, detail={"code": "bad_param", "message": "alpha_id 格式不合法"})
    return _call("alpha.get", alpha_id=alpha_id)


@router.get("/vibe/readiness")
def vibe_readiness() -> dict:
    return _call("alpha.readiness")


# ---------------------------------------------------------------- 基准测试

class BenchIn(BaseModel):
    zoo: str = Field(..., min_length=1, max_length=32)
    universe: str = Field(..., min_length=1, max_length=32)
    period: str = Field(..., min_length=4, max_length=32)
    top: int = Field(20, ge=1, le=500)
    alpha_id: str | None = Field(None, max_length=80)


@router.post("/vibe/bench")
def vibe_bench(body: BenchIn) -> dict:
    if body.zoo not in _VALID_ZOOS:
        raise HTTPException(400, detail={"code": "bad_param", "message": f"未知 zoo：{body.zoo}"})
    u = body.universe.strip().lower()
    if u not in _BENCH_UNIVERSES:
        raise HTTPException(
            400,
            detail={
                "code": "bad_param",
                "message": "bench 仅开放 A 股 csi300（上游 0.1.18 的基准域只有 csi300/sp500/btc-usdt，暂不支持港股），"
                           f"收到：{body.universe}",
            },
        )
    if not _PERIOD_RE.fullmatch(body.period):
        raise HTTPException(
            400,
            detail={"code": "bad_param", "message": "period 需为 YYYY-YYYY 或 YYYY-MM-DD/YYYY-MM-DD"},
        )
    if body.alpha_id and not _ALPHA_ID_RE.fullmatch(body.alpha_id):
        raise HTTPException(400, detail={"code": "bad_param", "message": "alpha_id 格式不合法"})
    return _call(
        "alpha.bench.start",
        zoo=body.zoo,
        universe="csi300",
        period=body.period,
        top=body.top,
        alpha_id=body.alpha_id,
    )


@router.get("/vibe/bench/{job_id}")
def vibe_bench_status(job_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, detail={"code": "bad_param", "message": "job_id 格式不合法"})
    return _call("alpha.bench.get", job_id=job_id)


# ---------------------------------------------------------------- 模板 / skill / 运行

@router.get("/vibe/swarm-presets")
def vibe_swarm_presets() -> dict:
    res = _call("swarm.presets")
    rows = res.get("data")
    if isinstance(rows, list):
        res["data"] = _filter_presets(rows)
        res["meta"]["filtered_out"] = len(rows) - len(res["data"])
        res["meta"]["market_scope"] = _MARKET_SCOPE
    return res


@router.get("/vibe/skills")
def vibe_skills() -> dict:
    return _call("skills.list")


@router.get("/vibe/runs")
def vibe_runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return _call("runs.list", limit=limit)


@router.get("/vibe/data-sources")
def vibe_data_sources() -> dict:
    return _call("data.sources")
