"""十个开源交易项目的统一接入矩阵。

这里**不 import 上游业务代码**，只做三件事：
1. 记录每个项目的来源、许可、接入面（REST / Skill / Notebook / 已移植 / 阻止项）；
2. 用最短路径探针确认真实状态（HTTP 健康、CLI 预检、Python import）；
3. 给网页一个稳定的 JSON：``live`` / ``ready`` / ``offline`` / ``blocked`` / ``missing``。

探针失败永远返回状态而不是抛异常；上游挂了只让矩阵变灰，不影响行情/持仓/通知。
所有子进程只拿到最小环境变量，不传 DB_* / API key。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

BASE = Path(os.environ.get("SA_PROJECTS_DIR") or r"D:\cyz-integrations")
CACHE_SECONDS = 30.0

# id / 名称 / 仓库 / 许可 / 接入面 / 运行方式 / 端口或入口
PROJECTS: list[dict] = [
    {
        "id": "vibe", "name": "Vibe-Trading", "repo": "HKUDS/Vibe-Trading",
        "license": "MIT", "mode": "REST sidecar", "runtime": "101 systemd + nginx",
        "surface": ["REST", "因子桥"], "market": "A股 / 港股",
        "url": "http://101.43.25.101/vibe/health", "dir": None,
    },
    {
        "id": "tsp", "name": "tick-stock-panel", "repo": "shy3130/tick-stock-panel",
        "license": "MIT", "mode": "Docker sidecar", "runtime": "docker :3018",
        "surface": ["REST", "OpenAPI"], "market": "A股",
        "url": "http://127.0.0.1:3018", "dir": BASE / "tick-stock-panel",
    },
    {
        "id": "daily", "name": "daily_stock_analysis", "repo": "ZhuLinsen/daily_stock_analysis",
        "license": "MIT", "mode": "本地 Web/API", "runtime": "venv :8000",
        "surface": ["Web", "FastAPI"], "market": "A股 / 港股 / 美股",
        "url": "http://127.0.0.1:8000", "dir": BASE / "daily_stock_analysis",
    },
    {
        "id": "last30days", "name": "last30days-skill", "repo": "mvanhorn/last30days-skill",
        "license": "MIT", "mode": "Skill / CLI", "runtime": "python CLI",
        "surface": ["Skill", "CLI"], "market": "全球主题 / 美股",
        "url": None, "dir": BASE / "last30days-skill",
    },
    {
        "id": "daytrader", "name": "day-trader-skill", "repo": "Jakeschincariol/day-trader-skill",
        "license": "MIT", "mode": "Skill / CLI", "runtime": "python CLI",
        "surface": ["Skill", "CLI"], "market": "美股日内",
        "url": None, "dir": BASE / "day-trader-skill",
    },
    {
        "id": "kronos", "name": "Kronos", "repo": "shiyu-coder/Kronos",
        "license": "MIT", "mode": "Notebook / 离线模型", "runtime": "CPU torch venv",
        "surface": ["Notebook", "Skill"], "market": "K线预测实验",
        "url": None, "dir": BASE / "Kronos",
    },
    {
        "id": "openbb", "name": "OpenBB", "repo": "OpenBB-finance/OpenBB",
        "license": "Apache-2.0", "mode": "REST / Python", "runtime": "venv",
        "surface": ["REST", "Python"], "market": "全球数据",
        "url": "http://127.0.0.1:6900", "dir": BASE / "OpenBB",
    },
    {
        "id": "freqtrade", "name": "freqtrade", "repo": "freqtrade/freqtrade",
        "license": "GPL-3.0", "mode": "独立进程（不 import）", "runtime": "venv",
        "surface": ["REST", "CLI"], "market": "加密",
        "url": "http://127.0.0.1:8081", "dir": BASE / "freqtrade",
    },
    {
        "id": "fincept", "name": "FinceptTerminal", "repo": "Fincept-Corporation/FinceptTerminal",
        "license": "AGPL-3.0 + 商业许可", "mode": "桌面应用", "runtime": "C++/Qt",
        "surface": ["设计参考"], "market": "全球",
        "url": None, "dir": BASE / "FinceptTerminal",
    },
    {
        "id": "tradingagents", "name": "TradingAgents", "repo": "TauricResearch/TradingAgents",
        "license": "Apache-2.0", "mode": "代码移植（冻结）", "runtime": "已进 paper_trading/debate",
        "surface": ["已移植"], "market": "A股 / 美股",
        "url": None, "dir": BASE / "TradingAgents",
    },
]

_BY_ID = {p["id"]: p for p in PROJECTS}
_LOCK = threading.Lock()
_CACHE: dict = {"ts": 0.0, "data": None}


# ---------------------------------------------------------------- 工具

def _env() -> dict:
    keys = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "NO_PROXY", "no_proxy")
    out = {k: os.environ[k] for k in keys if k in os.environ}
    out["PYTHONIOENCODING"] = "utf-8"
    return out


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 25.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, env=_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, shell=False,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[:1200]
    except FileNotFoundError:
        return 127, "找不到入口文件/解释器"
    except subprocess.TimeoutExpired:
        return 124, f"探针超时（{timeout:.0f}s）"
    except Exception as exc:                       # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def _git_commit(path: Path | None) -> str:
    if not path or not (path / ".git").exists():
        return ""
    rc, out = _run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], timeout=3)
    return out.strip() if rc == 0 else ""


def _http(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as c:
            r = c.get(url)
        if r.status_code < 500:
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:                       # noqa: BLE001
        return False, type(exc).__name__


def _venv(name: str) -> Path:
    return BASE / "venvs" / name / "Scripts" / "python.exe"



# ---------------------------------------------------------------- 逐项目探针
# 每个探针返回 {state, detail, extra?}；state 只允许 live/ready/offline/blocked/missing。

def _probe_vibe(p: dict) -> dict:
    try:
        from integrations.vibe import get_vibe, vibe_enabled
        if not vibe_enabled():
            return {"state": "blocked", "detail": "SA_VIBE_DISABLED 已关闭"}
        h = get_vibe().health()
        if h.get("ok"):
            payload = h.get("payload") or {}
            return {"state": "live",
                    "detail": f"{payload.get('service', 'Vibe')} · {payload.get('status', 'healthy')}",
                    "extra": {"elapsed_ms": h.get("elapsed_ms")}}
        err = (h.get("error") or {}).get("message") or "sidecar 未连接"
        return {"state": "offline", "detail": err}
    except Exception as exc:                       # noqa: BLE001
        return {"state": "offline", "detail": f"{type(exc).__name__}: {exc}"}


def _probe_tsp(p: dict) -> dict:
    try:
        from integrations.tsp import get_tsp, tsp_enabled
        if not tsp_enabled():
            return {"state": "blocked", "detail": "SA_TSP_DISABLED 已关闭"}
        h = get_tsp().health()
        if h.get("ok"):
            info = (h.get("payload") or {}).get("info") or {}
            return {"state": "live",
                    "detail": f"{info.get('title', 'Tick Stock Panel')} {info.get('version', '')}".strip(),
                    "extra": {"elapsed_ms": h.get("elapsed_ms")}}
        err = (h.get("error") or {}).get("message") or "容器未启动"
        return {"state": "offline", "detail": err}
    except Exception as exc:                       # noqa: BLE001
        return {"state": "offline", "detail": f"{type(exc).__name__}: {exc}"}


def _probe_daily(p: dict) -> dict:
    ok, detail = _http("http://127.0.0.1:8000/")
    if ok:
        return {"state": "live", "detail": f"WebUI {detail}"}
    py = _venv("daily")
    if py.exists():
        return {"state": "ready", "detail": f"依赖已装，服务未启动（{detail}）"}
    return {"state": "missing", "detail": "未完成安装"}


def _probe_last30days(p: dict) -> dict:
    script = p["dir"] / "skills" / "last30days" / "scripts" / "last30days.py"
    if not script.exists():
        return {"state": "missing", "detail": "找不到 last30days.py"}
    rc, out = _run([sys.executable, str(script), "--preflight"], cwd=p["dir"], timeout=25)
    if rc != 0:
        return {"state": "offline", "detail": out.strip().splitlines()[-1][:200] if out.strip() else f"exit {rc}"}
    ready = "Status: Ready" in out
    return {"state": "ready" if ready else "offline",
            "detail": "preflight 通过：Reddit/HN/Polymarket/GitHub 可用" if ready else "preflight 未就绪"}


def _probe_daytrader(p: dict) -> dict:
    script = p["dir"] / "trader-scanner" / "scan.py"
    if not script.exists():
        return {"state": "missing", "detail": "找不到 trader-scanner/scan.py"}
    rc, out = _run([sys.executable, str(script), "--help"], cwd=p["dir"], timeout=15)
    if rc != 0:
        return {"state": "offline", "detail": out.strip().splitlines()[-1][:200] if out.strip() else f"exit {rc}"}
    return {"state": "ready", "detail": "11 个 skill 的 CLI 可用（美股日内，仅研究）"}


def _probe_kronos(p: dict) -> dict:
    py = _venv("kronos")
    if not py.exists():
        return {"state": "missing", "detail": "CPU venv 未建好"}
    rc, out = _run([str(py), "-c", "import torch, pandas; print(torch.__version__)"],
                   cwd=p["dir"], timeout=30)
    if rc != 0:
        return {"state": "offline", "detail": (out.strip().splitlines() or ["import 失败"])[-1][:200]}
    ver = out.strip().splitlines()[-1]
    weights = [x for x in (p["dir"] / "model").glob("*.pth")] if (p["dir"] / "model").exists() else []
    if weights:
        return {"state": "ready", "detail": f"torch {ver}，权重 {len(weights)} 个；离线预测实验"}
    return {"state": "ready", "detail": f"torch {ver}（CPU）；权重未下载，未跑推理"}


def _probe_openbb(p: dict) -> dict:
    ok, detail = _http("http://127.0.0.1:6900/")
    if ok:
        return {"state": "live", "detail": f"API {detail}"}
    py = _venv("openbb")
    if py.exists():
        rc, out = _run([str(py), "-c", "import openbb; print(getattr(openbb, '__version__', 'ok'))"],
                       cwd=p["dir"], timeout=30)
        if rc == 0:
            return {"state": "ready", "detail": f"Python {out.strip().splitlines()[-1]}；API 未启动"}
        return {"state": "offline", "detail": (out.strip().splitlines() or ["import 失败"])[-1][:200]}
    return {"state": "missing", "detail": "venv 未建好"}


def _probe_freqtrade(p: dict) -> dict:
    ok, detail = _http("http://127.0.0.1:8081/api/v1/ping")
    if ok:
        return {"state": "live", "detail": f"webserver {detail}（加密，不参与 A 股决策）"}
    py = _venv("freqtrade")
    if py.exists():
        rc, out = _run([str(py), "-m", "freqtrade", "--version"], cwd=p["dir"], timeout=20)
        if rc == 0:
            lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
            ver = next((ln.split(":")[-1].strip() for ln in lines if "freqtrade version" in ln.lower()), "") or (lines[-1] if lines else "已安装")
            return {"state": "ready", "detail": f"{ver}（加密市场，不参与 A 股/港股决策）"}
        return {"state": "offline", "detail": (out.strip().splitlines() or ["CLI 失败"])[-1][:200]}
    return {"state": "missing", "detail": "venv 未建好"}


def _probe_fincept(p: dict) -> dict:
    if not p["dir"].exists():
        return {"state": "missing", "detail": "仓库未克隆"}
    return {"state": "blocked",
            "detail": "C++/Qt 桌面应用，无服务 API；AGPL 商业/公司内部使用需商业许可"}


def _probe_tradingagents(p: dict) -> dict:
    if not p["dir"].exists():
        return {"state": "missing", "detail": "仓库未克隆"}
    py = _venv("tradingagents")
    if py.exists():
        rc, out = _run([str(py), "-c", "import tradingagents; print('ok')"],
                       cwd=p["dir"], timeout=30)
        if rc == 0:
            return {"state": "ready", "detail": "上游可 import；核心三件已移植进 paper_trading/debate"}
        return {"state": "ready", "detail": "已克隆；依赖未装完，核心逻辑已移植"}
    return {"state": "ready", "detail": "已克隆；不在主环境重复装 LangGraph，核心逻辑已移植"}


_PROBES = {
    "vibe": _probe_vibe, "tsp": _probe_tsp, "daily": _probe_daily,
    "last30days": _probe_last30days, "daytrader": _probe_daytrader,
    "kronos": _probe_kronos, "openbb": _probe_openbb, "freqtrade": _probe_freqtrade,
    "fincept": _probe_fincept, "tradingagents": _probe_tradingagents,
}


def _row(p: dict) -> dict:
    fn = _PROBES[p["id"]]
    try:
        res = fn(p) or {}
    except Exception as exc:                       # noqa: BLE001
        res = {"state": "offline", "detail": f"{type(exc).__name__}: {exc}"}
    out = {k: p[k] for k in ("id", "name", "repo", "license", "mode",
                             "runtime", "surface", "market", "url")}
    out["dir"] = str(p["dir"]) if p.get("dir") else ""
    out["commit"] = _git_commit(p.get("dir"))
    out["state"] = res.get("state", "offline")
    out["detail"] = res.get("detail", "")
    if res.get("extra"):
        out["extra"] = res["extra"]
    return out


def check_all(force: bool = False) -> dict:
    now = time.time()
    with _LOCK:
        if not force and _CACHE["data"] and now - _CACHE["ts"] < CACHE_SECONDS:
            return _CACHE["data"]
    with ThreadPoolExecutor(max_workers=len(PROJECTS)) as ex:
        rows = list(ex.map(_row, PROJECTS))
    summary = {k: 0 for k in ("live", "ready", "offline", "blocked", "missing")}
    for r in rows:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    summary["connected"] = summary["live"] + summary["ready"]
    summary["total"] = len(rows)
    data = {
        "projects": rows,
        "summary": summary,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "base": str(BASE),
        "cache_seconds": CACHE_SECONDS,
    }
    with _LOCK:
        _CACHE.update(ts=now, data=data)
    return data


def check_one(project_id: str) -> dict:
    p = _BY_ID.get(project_id)
    if not p:
        return {"ok": False, "error": f"未知项目：{project_id}"}
    return {"ok": True, "data": _row(p)}
