"""tick-stock-panel（shy3130）REST 适配器。

TSP 是单容器 A 股工作台；本站只开只读行情/指标，不接 write:ext 与 paper:trade。
默认连 ``http://127.0.0.1:3018``，容器没起时 ``health()`` 返回 ok=false，
业务侧降级即可，不影响主站。
"""

from __future__ import annotations

import os
import threading

from .base import Integration

_DEFAULT_BASE = "http://127.0.0.1:3018"


class TspIntegration(Integration):
    name = "tsp"
    env_prefix = "TSP"
    default_base_url = _DEFAULT_BASE
    default_timeout = 20.0

    def daily(self, *, symbol: str, limit: int = 60) -> dict:
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
