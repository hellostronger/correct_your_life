"""Vibe-Trading（HKUDS）REST 适配器。

上游只做研究：因子库、基准测试、多智能体模板、研究运行记录。
本适配器**只读**为主，唯一写操作是启动一个远端 bench job（不碰交易）。

环境变量
--------
- ``VIBE_BASE_URL``     默认 ``http://127.0.0.1:8899``；部署在 101 时写
  ``http://101.43.25.101/vibe``
- ``VIBE_API_KEY``      可选；写成上游 ``API_AUTH_KEY`` 的值即可（不自动读取后者）
- ``VIBE_ALLOW_REMOTE`` 1/true 时才允许连非本机地址

``VIBE_*`` 可以直接写在仓库根 ``.env`` 里（进程环境优先），不必依赖启动脚本
导出；只认这个前缀，不会把 ``DB_*`` 等其它变量带进进程。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .base import Integration, IntegrationError

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _load_vibe_env() -> None:
    """把仓库根 ``.env`` 里的 VIBE_* 补进进程环境（已有值不覆盖）。"""
    if not _ENV_FILE.exists():
        return
    try:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key.startswith("VIBE_"):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_vibe_env()



class VibeIntegration(Integration):
    name = "vibe"
    env_prefix = "VIBE"
    default_base_url = "http://127.0.0.1:8899"
    default_timeout = 30.0

    # -- 领域方法（业务代码只用这些，不拼 URL） ----------------------

    def alphas(
        self,
        *,
        zoo: str | None = None,
        theme: str | None = None,
        universe: str | None = None,
        limit: int | None = None,
    ) -> dict:
        return self.call(
            "alpha.list", zoo=zoo, theme=theme, universe=universe, limit=limit
        )

    def alpha(self, alpha_id: str) -> dict:
        return self.call("alpha.get", alpha_id=alpha_id)

    def readiness(self) -> dict:
        return self.call("alpha.readiness")

    def data_sources(self) -> dict:
        return self.call("data.sources")

    def start_bench(
        self,
        *,
        zoo: str,
        universe: str = "csi300",
        period: str,
        top: int = 20,
        alpha_id: str | None = None,
    ) -> dict:
        """提交因子回测。研究覆盖 A 股/港股，但上游 0.1.18 无港股基准，
        bench 只开放 csi300（调用方已校验）。"""
        return self.call(
            "alpha.bench.start",
            zoo=zoo,
            universe=universe,
            period=period,
            top=top,
            alpha_id=alpha_id,
        )

    def bench(self, job_id: str) -> dict:
        return self.call("alpha.bench.get", job_id=job_id)

    def swarm_presets(self) -> dict:
        return self.call("swarm.presets")

    def swarm_runs(self, limit: int | None = None) -> dict:
        return self.call("swarm.runs", limit=limit)

    def skills(self) -> dict:
        return self.call("skills.list")

    def runs(self, limit: int | None = None) -> dict:
        return self.call("runs.list", limit=limit)

    # -- 聚合视图 ----------------------------------------------------

    def summary(self) -> dict:
        """一次拿齐网页首屏需要的状态；任一子项失败只降级该块。"""
        out: dict[str, Any] = {
            "integration": self.name,
            "upstream": self.contract.upstream,
            "tested_version": self.contract.tested_version,
            "base_url": self.base_url,
            "ok": False,
            "blocks": {},
        }
        health = self.health()
        out["health"] = health
        out["ok"] = bool(health.get("ok"))
        if not out["ok"]:
            # health 不通时其余块必然也失败，不再逐个重试：Windows 上每次
            # 连死端口约 2 秒，5 个块串起来就是 10 秒，页面会一直转圈。
            reason = health.get("error") or {
                "code": "unavailable",
                "message": "sidecar 未连接",
            }
            for key in ("ready", "readiness", "data_sources", "swarm_presets"):
                out["blocks"][key] = {"ok": False, "error": reason, "skipped": "health 未通过"}
            return out

        for key, fn in (
            ("ready", self._safe_ready),
            ("readiness", self._safe_readiness),
            ("data_sources", self._safe_sources),
            ("swarm_presets", self._safe_presets),
        ):
            out["blocks"][key] = fn()
        return out

    def _safe(self, fn) -> dict:
        try:
            res = fn()
            return {"ok": True, "data": res["data"], "elapsed_ms": res["elapsed_ms"]}
        except IntegrationError as e:
            return {"ok": False, "error": e.as_dict()}

    def _safe_ready(self) -> dict:
        # /ready 在未配 LLM 时返 503（allow_status），这是有效信息而非失败
        try:
            res = self.call("ready")
            body = res["data"] if isinstance(res["data"], dict) else {}
            return {
                "ok": res["http_status"] < 400,
                "http_status": res["http_status"],
                "detail": body,
                "elapsed_ms": res["elapsed_ms"],
            }
        except IntegrationError as e:
            return {"ok": False, "error": e.as_dict()}

    def _safe_readiness(self) -> dict:
        return self._safe(self.readiness)

    def _safe_sources(self) -> dict:
        return self._safe(self.data_sources)

    def _safe_presets(self) -> dict:
        return self._safe(self.swarm_presets)


# ---------------------------------------------------------------- 单例

_LOCK = threading.Lock()
_INSTANCE: VibeIntegration | None = None


def get_vibe() -> VibeIntegration:
    """进程内复用同一个 httpx 客户端（keep-alive）。"""
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = VibeIntegration()
        return _INSTANCE


def reset_vibe() -> None:
    """测试或改环境变量后重建。"""
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is not None:
            _INSTANCE.close()
        _INSTANCE = None


def vibe_enabled() -> bool:
    return os.environ.get("SA_VIBE_DISABLED") not in ("1", "true", "yes")
