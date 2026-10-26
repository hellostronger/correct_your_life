"""外部项目接入层：契约驱动，不 import 上游代码。

设计要点
--------
1. **只认能力名**：业务代码调 ``vibe.call("alpha.list", zoo="crypto")``，
   不出现上游 URL / 字段名。上游改路径时只改 ``contracts/<name>.json``。
2. **契约即测试**：每个能力声明 ``required_fields``，``probe()`` 逐个真调一次
   校验字段/类型，任一失败就判定该上游不可用（升级前先跑它）。
3. **失败不抛到业务**：``call`` 内部异常统一包成 ``IntegrationError``，
   API 层再转 502/504，网页显示降级提示而不是 500。
4. **默认只连本机**：sidecar 只监听 loopback；非 loopback 必须显式
   ``allow_remote=True``，避免把内网服务变成任意 URL 代理。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
MAX_BODY_BYTES = 4 * 1024 * 1024

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


class IntegrationError(Exception):
    """接入层统一异常。``code`` 供 API/UI 判断，不把上游堆栈抛出去。"""

    status = 502

    def __init__(self, code: str, message: str, *, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class IntegrationUnavailable(IntegrationError):
    """连不上 / 未启动 / 认证失败 → 业务侧应降级，不算 bug。"""

    status = 503

    def __init__(self, message: str, *, detail: Any = None):
        super().__init__("unavailable", message, detail=detail)


class IntegrationTimeout(IntegrationError):
    status = 504

    def __init__(self, message: str, *, detail: Any = None):
        super().__init__("timeout", message, detail=detail)


class ContractViolation(IntegrationError):
    """响应缺字段 / 类型不对 → 多半是上游升级改了契约。"""

    status = 502

    def __init__(self, message: str, *, detail: Any = None):
        super().__init__("contract_violation", message, detail=detail)


class IntegrationNotFound(IntegrationError):
    """带合法 id 但上游说没有：alpha 不存在、bench job 过期。不是契约问题。"""

    status = 404

    def __init__(self, message: str, *, detail: Any = None):
        super().__init__("not_found", message, detail=detail)


# ---------------------------------------------------------------- 类型校验

_TYPES: dict[str, tuple] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
    "any": (object,),
}


def _check_type(value: Any, spec: str) -> bool:
    """``spec`` 形如 ``list`` / ``dict`` / ``str?``（``?`` 表示可空）。"""
    nullable = spec.endswith("?")
    if nullable:
        spec = spec[:-1]
        if value is None:
            return True
    if spec == "bool":
        return isinstance(value, bool)
    if spec == "int":
        # bool 是 int 子类，显式排除，避免 status=True 蒙过校验
        return isinstance(value, int) and not isinstance(value, bool)
    if spec == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES.get(spec, (object,)))


def _dig(payload: Any, dotted: str) -> Any:
    """取 ``a.b.0.c`` 这样的路径；缺键返回 ``_MISSING``。"""
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return cur


_MISSING = object()


# ---------------------------------------------------------------- 契约对象

@dataclass
class Capability:
    name: str
    method: str
    path: str
    required: dict[str, str] = field(default_factory=dict)
    query: list[str] = field(default_factory=list)
    timeout: float | None = None
    description: str = ""
    # 顶层类型：list / dict；None 表示不限制
    type: str | None = None
    # 允许的非 2xx 状态码（如 /ready 未配 LLM 时返 503，这本身是有效诊断）
    allow_status: tuple[int, ...] = ()
    # 契约测试用：False = 跳过（有副作用），dict = 带样本参数真调
    probe: dict | bool | None = None


@dataclass
class Contract:
    integration: str
    upstream: str
    tested_version: str
    capabilities: dict[str, Capability]
    health: Capability | None = None
    notes: str = ""

    @classmethod
    def load(cls, name: str, path: Path | None = None) -> "Contract":
        p = path or (CONTRACTS_DIR / f"{name}.json")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise IntegrationError("no_contract", f"缺少契约文件：{p.name}")
        except json.JSONDecodeError as e:
            raise IntegrationError("bad_contract", f"契约文件不是合法 JSON：{e}")

        caps: dict[str, Capability] = {}
        for cap_name, spec in (raw.get("capabilities") or {}).items():
            caps[cap_name] = Capability(
                name=cap_name,
                method=(spec.get("method") or "GET").upper(),
                path=spec["path"],
                required=spec.get("required") or {},
                query=spec.get("query") or [],
                timeout=spec.get("timeout"),
                description=spec.get("description") or "",
                type=spec.get("type"),
                allow_status=tuple(spec.get("allow_status") or ()),
                probe=spec.get("probe"),
            )
        health_spec = raw.get("health")
        health = None
        if health_spec:
            health = Capability(
                name="health",
                method=(health_spec.get("method") or "GET").upper(),
                path=health_spec["path"],
                required=health_spec.get("required") or {},
                query=health_spec.get("query") or [],
                timeout=health_spec.get("timeout"),
                type=health_spec.get("type"),
                allow_status=tuple(health_spec.get("allow_status") or ()),
            )
        return cls(
            integration=raw.get("integration") or name,
            upstream=raw.get("upstream") or "",
            tested_version=raw.get("tested_version") or "",
            capabilities=caps,
            health=health,
            notes=raw.get("notes") or "",
        )


# ---------------------------------------------------------------- 适配器

class Integration:
    """单个外部项目的 REST 适配器基类。

    子类只提供 ``name`` / ``env_prefix`` / 默认端口，其余全部走契约。
    """

    name: str = ""
    env_prefix: str = ""
    default_base_url: str = ""
    default_timeout: float = DEFAULT_TIMEOUT

    def __init__(
        self,
        base_url: str | None = None,
        *,
        contract: Contract | None = None,
        token: str | None = None,
        timeout: float | None = None,
        allow_remote: bool = False,
        client: httpx.Client | None = None,
    ):
        self.base_url = (
            base_url
            or os.environ.get(f"{self.env_prefix}_BASE_URL")
            or self.default_base_url
        ).rstrip("/")
        self.token = token if token is not None else self._env_token()
        self.timeout = min(float(timeout or self.default_timeout), MAX_TIMEOUT)
        self.allow_remote = allow_remote or os.environ.get(
            f"{self.env_prefix}_ALLOW_REMOTE"
        ) in ("1", "true", "yes")
        self.contract = contract or Contract.load(self.name)
        self._client = client
        self._owns_client = client is None
        self._validate_base_url()

    # -- 环境 --------------------------------------------------------

    def _env_token(self) -> str | None:
        # 只认项目专用变量。故意不回落全局 API_AUTH_KEY——那是本站自己的
        # 鉴权变量，混用会把本站密钥发给别的服务。
        val = os.environ.get(f"{self.env_prefix}_API_KEY")
        return val or None

    def _validate_base_url(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise IntegrationError(
                "bad_base_url", f"{self.name} BASE_URL 必须是 http(s)：{self.base_url}"
            )
        if not self.allow_remote and parsed.hostname not in _LOOPBACK_HOSTS:
            raise IntegrationError(
                "remote_not_allowed",
                f"{self.name} 默认只允许本机 sidecar；确需远端请设 "
                f"{self.env_prefix}_ALLOW_REMOTE=1",
            )

    # -- HTTP --------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._headers(),
                follow_redirects=False,
                trust_env=False,  # 不吃系统代理，sidecar 是本机回环
            )
        return self._client

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
        self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- 调用 --------------------------------------------------------

    def capabilities(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "method": c.method,
                "description": c.description,
                "required": c.required,
            }
            for c in self.contract.capabilities.values()
        ]

    def _request(
        self, cap: Capability, params: dict, *, strict: bool = True
    ) -> dict:
        payload = {k: v for k, v in params.items() if v is not None}
        path = self._fill_path(cap.path, payload)
        query = {k: payload.pop(k) for k in cap.query if k in payload}
        body = payload if cap.method in ("POST", "PUT", "PATCH") else None

        started = time.monotonic()
        try:
            resp = self.client.request(
                cap.method,
                path,
                params=query or None,
                json=body,
                timeout=min(cap.timeout or self.timeout, MAX_TIMEOUT),
                headers=self._headers(),
            )
        except httpx.TimeoutException as e:
            raise IntegrationTimeout(
                f"{self.name} {cap.name} 超时（{cap.timeout or self.timeout}s）",
                detail=str(e),
            )
        except httpx.HTTPError as e:
            raise IntegrationUnavailable(
                f"{self.name} 连不上（{self.base_url}）：{type(e).__name__}",
                detail=str(e),
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code in (401, 403):
            raise IntegrationUnavailable(
                f"{self.name} 拒绝访问（HTTP {resp.status_code}）：检查 API key / 是否只允许本机",
                detail=_snip(resp),
            )
        allowed = resp.status_code in cap.allow_status
        if resp.status_code == 404:
            raise IntegrationNotFound(
                f"{self.name} 找不到资源：{cap.method} {path}",
                detail=_snip(resp),
            )
        if resp.status_code >= 400 and not allowed:
            raise IntegrationError(
                "upstream_error",
                f"{self.name} 返回 HTTP {resp.status_code}",
                detail=_snip(resp),
            )

        if len(resp.content) > MAX_BODY_BYTES:
            raise ContractViolation(
                f"{self.name} 响应过大（{len(resp.content)} bytes），已拒绝解析"
            )
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype:
            raise ContractViolation(
                f"{self.name} 返回的不是 JSON（{ctype or '无 content-type'}）",
                detail=_snip(resp),
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise ContractViolation(f"{self.name} JSON 解析失败：{e}")

        result = {
            "capability": cap.name,
            "data": data,
            "elapsed_ms": elapsed_ms,
            "upstream_version": self.contract.tested_version,
            "http_status": resp.status_code,
        }
        ok_status = 200 <= resp.status_code < 300
        if strict and ok_status:
            if cap.type and not _check_type(data, cap.type):
                raise ContractViolation(
                    f"{self.name} {cap.name} 顶层类型应为 {cap.type}，"
                    f"实际 {type(data).__name__}"
                )
            missing = _missing_fields(data, cap.required)
            if missing:
                raise ContractViolation(
                    f"{self.name} {cap.name} 缺字段：{', '.join(missing)}",
                    detail={"required": cap.required, "got_keys": _keys(data)},
                )
        return result

    def _fill_path(self, template: str, payload: dict) -> str:
        if "{" not in template:
            return template
        out = template
        for key in list(payload):
            token = "{" + key + "}"
            if token in out:
                val = str(payload.pop(key))
                if "/" in val or "?" in val:
                    raise IntegrationError("bad_param", f"路径参数 {key} 含非法字符")
                out = out.replace(token, val)
        if "{" in out:
            raise IntegrationError(
                "missing_param", f"缺少路径参数：{template}"
            )
        return out

    def call(self, capability: str, **params) -> dict:
        cap = self.contract.capabilities.get(capability)
        if cap is None:
            raise IntegrationError(
                "unknown_capability",
                f"{self.name} 未声明能力 {capability}",
                detail={"known": sorted(self.contract.capabilities)},
            )
        return self._request(cap, params)

    # -- 健康 / 契约测试 ---------------------------------------------

    def health(self) -> dict:
        """返回 ``{ok, ...}``，永不抛异常——供 /api 面板轮询。"""
        out = {
            "ok": False,
            "integration": self.name,
            "base_url": self.base_url,
            "tested_version": self.contract.tested_version,
            "auth": bool(self.token),
        }
        if self.contract.health is None:
            out.update(ok=None, note="契约未声明 health 能力")
            return out
        try:
            res = self._request(self.contract.health, {})
            out.update(ok=True, elapsed_ms=res["elapsed_ms"], payload=res["data"])
        except IntegrationError as e:
            out.update(error=e.as_dict())
        return out

    def probe(self, only: list[str] | None = None) -> dict:
        """逐个能力真调一遍，校验必填字段。

        路径参数没给的跳过（记 ``skipped``），不算失败。
        """
        results = []
        ok_all = True
        for name, cap in self.contract.capabilities.items():
            if only and name not in only:
                continue
            if cap.probe is False:
                results.append(
                    {"capability": name, "ok": None, "skipped": "契约标记为有副作用"}
                )
                continue
            sample = cap.probe if isinstance(cap.probe, dict) else {}
            need = _path_params(cap.path)
            absent = [p for p in need if p not in sample]
            if absent:
                results.append(
                    {"capability": name, "ok": None, "skipped": f"需要参数 {absent}"}
                )
                continue
            try:
                res = self._request(cap, dict(sample), strict=True)
                results.append(
                    {
                        "capability": name,
                        "ok": True,
                        "elapsed_ms": res["elapsed_ms"],
                        "http_status": res["http_status"],
                        "required": cap.required,
                    }
                )
            except IntegrationError as e:
                ok_all = False
                results.append({"capability": name, "ok": False, "error": e.as_dict()})
        return {
            "integration": self.name,
            "base_url": self.base_url,
            "tested_version": self.contract.tested_version,
            "ok": ok_all,
            "checks": results,
        }


def _path_params(template: str) -> list[str]:
    out, buf = [], ""
    inside = False
    for ch in template:
        if ch == "{":
            inside, buf = True, ""
        elif ch == "}" and inside:
            inside = False
            out.append(buf)
        elif inside:
            buf += ch
    return out


def _missing_fields(payload: Any, required: dict[str, str]) -> list[str]:
    missing = []
    for dotted, spec in required.items():
        val = _dig(payload, dotted)
        if val is _MISSING:
            missing.append(f"{dotted}（缺）")
        elif not _check_type(val, spec):
            missing.append(f"{dotted}（期望 {spec}，实际 {type(val).__name__}）")
    return missing


def _keys(payload: Any) -> list[str]:
    return sorted(payload) if isinstance(payload, dict) else []


def _snip(resp: httpx.Response, limit: int = 400) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001 — 极端编码问题不应掩盖真正的错误
        return "<unreadable body>"
    text = text.replace("\n", " ").strip()
    return text[:limit]
