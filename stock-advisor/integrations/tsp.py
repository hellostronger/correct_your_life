"""tick-stock-panel（shy3130）REST 适配器。

TSP 是单容器 A 股工作台；本站只开只读行情/指标，不接 write:ext 与 paper:trade。
默认连 ``http://127.0.0.1:3018``，容器没起时 ``health()`` 返回 ok=false，
业务侧降级即可，不影响主站。

TSP 自带密码登录（HttpOnly cookie ``tf_session``）：公网部署时 ``/api/kline/*``
会返回 401。适配器用 ``TSP_PASSWORD`` 惰性登录，会话过期（30 天）后自动重登
一次；密码只从环境读，不落日志、不进契约文件。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .base import Integration, IntegrationUnavailable, _snip

_DEFAULT_BASE = "http://127.0.0.1:3018"
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _load_tsp_env() -> None:
    """把仓库根 ``.env`` 里的 TSP_* 补进进程环境（已有值不覆盖）。"""
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
        if not key.startswith("TSP_"):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_tsp_env()



class TspIntegration(Integration):
    name = "tsp"
    env_prefix = "TSP"
    default_base_url = _DEFAULT_BASE
    default_timeout = 20.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.password = os.environ.get("TSP_PASSWORD") or ""
        self._logged_in = not self.password

    # -- 认证 --------------------------------------------------------

    def _ensure_login(self, *, force: bool = False) -> None:
        if not self.password or (self._logged_in and not force):
            return
        try:
            resp = self.client.post(
                "/api/auth/login",
                json={"password": self.password},
                timeout=min(self.timeout, 30.0),
            )
        except Exception as exc:  # noqa: BLE001 - 认证失败统一走降级
            raise IntegrationUnavailable(
                f"tsp 登录请求失败（{self.base_url}）",
                detail=f"{type(exc).__name__}: {exc}",
            )
        if resp.status_code != 200:
            self._logged_in = False
            raise IntegrationUnavailable(
                f"tsp 登录被拒（HTTP {resp.status_code}）：检查 TSP_PASSWORD",
                detail=_snip(resp),
            )
        self._logged_in = True

    def health(self) -> dict:
        out = super().health()
        out["auth"] = bool(self.token or self.password)
        return out

    # -- 能力 --------------------------------------------------------

    def daily(self, *, symbol: str, limit: int = 60) -> dict:
        self._ensure_login()
        try:
            return self.call("kline.daily", symbol=symbol, limit=limit)
        except IntegrationUnavailable as exc:
            # 会话过期 / sidecar 重启后 cookie 失效：重登一次再试
            if not self.password or "401" not in exc.message:
                raise
            self._ensure_login(force=True)
            return self.call("kline.daily", symbol=symbol, limit=limit)


_LOCK = threading.Lock()
_INSTANCE: TspIntegration | None = None


def get_tsp() -> TspIntegration:
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = TspIntegration()
        return _INSTANCE


def reset_tsp() -> None:
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is not None:
            _INSTANCE.close()
        _INSTANCE = None


def tsp_enabled() -> bool:
    return os.environ.get("SA_TSP_DISABLED") not in ("1", "true", "yes")
