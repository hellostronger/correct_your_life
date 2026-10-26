"""外部项目接入层。

每个上游一个模块（``vibe.py``），共享 ``base.py`` 的契约与错误模型。
契约文件在 ``contracts/<name>.json``，升级前用 ``probe()`` 验字段。
"""

from .base import (  # noqa: F401
    Contract,
    ContractViolation,
    Integration,
    IntegrationError,
    IntegrationNotFound,
    IntegrationTimeout,
    IntegrationUnavailable,
)
from .vibe import VibeIntegration, get_vibe, reset_vibe, vibe_enabled  # noqa: F401

__all__ = [
    "Contract",
    "ContractViolation",
    "Integration",
    "IntegrationError",
    "IntegrationNotFound",
    "IntegrationTimeout",
    "IntegrationUnavailable",
    "VibeIntegration",
    "get_vibe",
    "reset_vibe",
    "vibe_enabled",
]
