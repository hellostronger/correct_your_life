"""因子桥 HTTP 门面（/api/factors/*）。

只做三件事：触发一轮计算、读最新快照、给因子目录。计算在
``tools/factor_worker.py``（Vibe venv）里，主应用不 import 上游。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

import factor_zoo

router = APIRouter(prefix="/api/factors", tags=["factors"])

_get_conn = None
_RUN = {"running": False, "started_at": None, "finished_at": None,
        "result": None, "error": ""}
_LOCK = threading.Lock()


def init(get_conn) -> None:
    global _get_conn
    _get_conn = get_conn


def _deps() -> dict:
    if _get_conn is None:
        raise HTTPException(503, "因子服务未初始化（app 未调用 factor_api.init）")
    return {"get_conn": _get_conn}


@router.get("/status")
def factors_status() -> dict:
    return {"ok": True, "data": factor_zoo.status(_deps())}


@router.get("/catalog")
def factors_catalog(
    theme: str | None = Query(None, max_length=24),
    zoo: str | None = Query(None, max_length=24),
    computable: bool | None = Query(None),
    limit: int = Query(0, ge=0, le=2000),
) -> dict:
    doc = factor_zoo.catalog()
    rows = doc.get("alphas") or []
    if theme:
        rows = [a for a in rows if theme in (a.get("themes") or [])]
    if zoo:
        rows = [a for a in rows if a.get("zoo") == zoo]
    if computable is not None:
        rows = [a for a in rows if bool(a.get("computable")) == computable]
    total = len(rows)
    if limit:
        rows = rows[:limit]
    return {"ok": True, "data": {
        "upstream": doc.get("upstream"), "upstream_version": doc.get("upstream_version"),
        "generated_at": doc.get("generated_at"), "total": total,
        "zoo": zoo, "theme": theme, "computable": computable,
        "alphas": rows,
        "error": doc.get("error") or "",
    }}


@router.get("/latest")
def factors_latest(code: str | None = Query(None, max_length=16)) -> dict:
    conn_fn = _deps()["get_conn"]
    code = factor_zoo.normalize_code(code) if code else None
    with conn_fn() as conn, conn.cursor() as cur:
        try:
            # 指定 code 时只读对应市场，别把另一市场的最新快照也算进来
            # （5 位 = 港股，6 位 = A 股）；不带 code 时合并两个市场。
            if code:
                universe = factor_zoo.universe_of(code)
                return factor_zoo.load_latest(cur, code=code, universe=universe)
            return factor_zoo.load_latest_all(cur)
        except Exception as exc:                  # noqa: BLE001
            raise HTTPException(500, "读因子快照失败：%s" % str(exc)[:300])


@router.get("/code/{code}")
def factors_code(code: str) -> dict:
    code = factor_zoo.normalize_code(code)
    if not code:
        raise HTTPException(400, "代码格式不对：A 股 6 位、港股 5 位数字")
    res = factors_latest(code=code)
    data = res.get("data") or {}
    codes = data.get("codes") or {}
    if not codes:
        mk = factor_zoo.MARKET_LABEL.get(
            factor_zoo.UNIVERSE_MARKET[factor_zoo.universe_of(code)], "")
        raise HTTPException(
            404, "最新 %s 快照里没有这只票（不在面板/日线不足/上次重算失败）" % mk)
    detail = next(iter(codes.values()))
    return {"ok": True, "data": {
        "run": {k: (data.get(k) if k != "market" else detail.get("market"))
                for k in ("run_id", "as_of", "universe", "n_codes", "n_days",
                          "n_alphas", "n_used", "elapsed_s",
                          "upstream_version", "params", "created_at", "market")},
        "code": next(iter(codes)),
        "detail": detail,
    }}


def _run_thread(force: bool) -> None:
    try:
        snap = factor_zoo.snapshot_all(_deps(), force=force)
        # 只回传每个市场的摘要，别把 alphas/codes 整包塞进 /run/status
        mk = {m: {k: s.get(k) for k in (
            "market", "market_label", "as_of", "universe", "panel",
            "n_codes", "n_alphas", "n_used", "elapsed_s")}
            for m, s in (snap.get("markets") or {}).items()}
        with _LOCK:
            _RUN.update({"result": {k: snap.get(k) for k in (
                "ok", "run_id", "as_of", "universe", "elapsed_s", "_wall_s",
                "errors", "error")},
                "error": "" if snap.get("ok") else str(snap.get("error"))[:500]})
            _RUN["result"]["markets"] = mk
    except Exception as exc:                      # noqa: BLE001
        traceback.print_exc()
        with _LOCK:
            _RUN["result"] = {"ok": False, "error": str(exc)[:500]}
            _RUN["error"] = str(exc)[:500]
    finally:
        with _LOCK:
            _RUN["running"] = False
            _RUN["finished_at"] = datetime.now().isoformat(timespec="seconds")


@router.post("/run")
def factors_run(force: bool = Query(True)) -> dict:
    with _LOCK:
        if _RUN["running"]:
            return {"ok": True, "data": {"status": "running", "state": dict(_RUN)}}
        _RUN.update({"running": True, "result": None, "error": "",
                     "started_at": datetime.now().isoformat(timespec="seconds"),
                     "finished_at": None})
    threading.Thread(target=_run_thread, args=(force,), daemon=True).start()
    return {"ok": True, "data": {"status": "started", "state": dict(_RUN)}}


@router.get("/run/status")
def factors_run_status() -> dict:
    with _LOCK:
        return {"ok": True, "data": dict(_RUN)}
