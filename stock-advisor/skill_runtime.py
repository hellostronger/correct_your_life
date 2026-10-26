"""Skill 运行时 —— 把外部项目/流程封装成受控子进程，主应用只认 JSON 契约。

设计目标（对应 docs/integration-survey-10-trading-repos.md 0.4 节）：

1. **不 vendoring**：上游代码不进本仓库，skill 只是一层很薄的 `SKILL.md +
   skill.json + run.py`。上游升级只改 skill 版本/依赖，主应用无感。
2. **fail closed**：manifest 不合法、缺命令/环境变量、输出不是合法 JSON、
   超时、非零退出，一律报错，绝不把半截/伪造结果当成功。
3. **最小权限**：子进程只拿到白名单环境变量；数据库凭据永远不传；
   不经过 shell，不拼接命令字符串。
4. **可审计**：每次运行写一行 JSONL（不记录参数值和输出内容，避免把
   用户数据/密钥落盘）。

目录约定：

    stock-advisor/skills/<name>/
        skill.json     机器可读清单（唯一事实来源）
        SKILL.md       给人看的说明（可被 GET /api/skills/{name} 返回）
        run.py         入口：stdin 读 {"params": {...}}，stdout 写一个 JSON 对象

stdout 契约（必须恰好是一个 JSON 对象）：

    {"ok": true, "data": {...}, "citations": ["https://..."], "warnings": []}

子进程的工作目录固定为 skill 目录；输出编码固定 UTF-8。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = Path(os.environ.get("SA_SKILLS_DIR") or (BASE_DIR / "skills")).resolve()
RUN_LOG = BASE_DIR / "data" / "skill_runs.jsonl"
RUN_LOG_MAX_BYTES = 2 * 1024 * 1024

MANIFEST_FILE = "skill.json"
README_FILE = "SKILL.md"

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
DEFAULT_MAX_OUTPUT = 64 * 1024
MIN_MAX_OUTPUT = 1024
MAX_MAX_OUTPUT = 1024 * 1024
MAX_PARAMS_BYTES = 64 * 1024
MAX_README_BYTES = 64 * 1024
MAX_CITATIONS = 50
MAX_WARNINGS = 20

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CMD_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")

# 这两个名字会与 /api/skills/runs、/api/skills/reload 两个静态路由冲突
_RESERVED_NAMES = {"runs", "reload"}


# ---------------------------------------------------------------- 异常

class SkillError(Exception):
    """所有 skill 相关错误的基类，API 层据此映射 HTTP 状态码。"""

    code = "skill_error"
    http_status = 400

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class SkillInvalid(SkillError):
    code = "invalid_skill"
    http_status = 400


class SkillNotFound(SkillError):
    code = "skill_not_found"
    http_status = 404


class SkillUnavailable(SkillError):
    code = "skill_unavailable"
    http_status = 409


class SkillBusy(SkillError):
    code = "skill_busy"
    http_status = 409


class SkillTimeout(SkillError):
    code = "skill_timeout"
    http_status = 504


class SkillRunFailed(SkillError):
    code = "skill_run_failed"
    http_status = 502


class SkillBadOutput(SkillError):
    code = "skill_bad_output"
    http_status = 502


# ---------------------------------------------------------------- 运行时开关

def _runtime_disabled() -> bool:
    return str(os.environ.get("SA_SKILLS_DISABLED", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


# ---------------------------------------------------------------- manifest

def _as_str(value: Any, field: str, *, default: str = "", max_len: int = 500) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise SkillInvalid(f"{field} 必须是字符串")
    value = value.strip()
    if len(value) > max_len:
        raise SkillInvalid(f"{field} 超过 {max_len} 字符")
    return value


def _as_int(value: Any, field: str, *, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise SkillInvalid(f"{field} 必须是整数")
    if not (lo <= value <= hi):
        raise SkillInvalid(f"{field} 必须在 {lo}..{hi} 之间")
    return value


def _as_bool(value: Any, field: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SkillInvalid(f"{field} 必须是布尔值")
    return value


def _as_str_list(value: Any, field: str, *, max_items: int, max_len: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillInvalid(f"{field} 必须是字符串数组")
    if len(value) > max_items:
        raise SkillInvalid(f"{field} 最多 {max_items} 项")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SkillInvalid(f"{field} 的每一项都必须是字符串")
        item = item.strip()
        if not item:
            continue
        if len(item) > max_len:
            raise SkillInvalid(f"{field} 的单项超过 {max_len} 字符")
        out.append(item)
    return out


# 无论 manifest 怎么写都不传给子进程的变量。skill 需要别的密钥必须显式写进
# env_allow —— 那是仓库维护者的一次明确授权，而不是默认继承。
_ENV_BLOCKED_EXACT = {
    "DATABASE_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE",
    "PGSSLMODE", "PGSERVICE", "PGCONNECT_TIMEOUT",
}
_ENV_BLOCKED_PREFIX = ("DB_",)

_ENV_BASE = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)


def _env_blocked(key: str) -> bool:
    upper = key.upper()
    return upper in _ENV_BLOCKED_EXACT or upper.startswith(_ENV_BLOCKED_PREFIX)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SkillInvalid(
            "skill 名称只能由小写字母、数字、下划线、连字符组成，且以字母或数字开头（1-64 位）"
        )
    if name in _RESERVED_NAMES:
        raise SkillInvalid(f"{name!r} 是保留名（与 /api/skills 的静态路由冲突）")
    return name


def _load_manifest(skill_dir: Path) -> dict:
    """读取并严格校验 skill.json。任何异常都转成 SkillInvalid。"""
    manifest_path = skill_dir / MANIFEST_FILE
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SkillInvalid(f"缺少 {MANIFEST_FILE}")
    except OSError as exc:
        raise SkillInvalid(f"读取 {MANIFEST_FILE} 失败：{exc}")

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillInvalid(f"{MANIFEST_FILE} 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）")
    if not isinstance(obj, dict):
        raise SkillInvalid(f"{MANIFEST_FILE} 顶层必须是对象")

    name = _as_str(obj.get("name"), "name", max_len=64)
    _validate_name(name)
    if name != skill_dir.name:
        raise SkillInvalid(f"name 必须与目录名一致（目录 {skill_dir.name!r}）")

    version = _as_str(obj.get("version"), "version", default="0.0.0", max_len=40)
    if not _VERSION_RE.match(version):
        raise SkillInvalid("version 必须形如 1.0.0")

    description = _as_str(obj.get("description"), "description", max_len=500)
    if not description:
        raise SkillInvalid("description 不能为空")

    entrypoint = _as_str(obj.get("entrypoint"), "entrypoint", default="run.py", max_len=200)
    if not entrypoint.endswith(".py"):
        raise SkillInvalid("entrypoint 目前只支持 .py 入口")
    skill_root = skill_dir.resolve()
    entry_path = (skill_dir / entrypoint).resolve()
    if not entry_path.is_relative_to(skill_root):
        raise SkillInvalid("entrypoint 不能越出 skill 目录")
    if not entry_path.is_file():
        raise SkillInvalid(f"entrypoint 不存在：{entrypoint}")

    # 只读是硬约束：skill 能读外部服务，但不允许改本仓库/生产库。
    read_only = obj.get("read_only")
    if read_only is not True:
        raise SkillInvalid("read_only 必须显式为 true（skill 不允许有写副作用）")

    capabilities = _as_str_list(
        obj.get("capabilities"), "capabilities", max_items=20, max_len=64)
    env_allow = _as_str_list(
        obj.get("env_allow"), "env_allow", max_items=30, max_len=64)
    for key in env_allow:
        if not _ENV_KEY_RE.match(key):
            raise SkillInvalid(f"env_allow 中的 {key!r} 不是合法环境变量名")
        if _env_blocked(key):
            raise SkillInvalid(f"env_allow 不允许包含数据库凭据：{key}")

    requires = obj.get("requires") or {}
    if not isinstance(requires, dict):
        raise SkillInvalid("requires 必须是对象")
    req_commands = _as_str_list(
        requires.get("commands"), "requires.commands", max_items=10, max_len=64)
    for cmd in req_commands:
        if not _CMD_RE.match(cmd):
            raise SkillInvalid(f"requires.commands 中的 {cmd!r} 不是合法命令名")
    req_env = _as_str_list(
        requires.get("env"), "requires.env", max_items=10, max_len=64)
    for key in req_env:
        if not _ENV_KEY_RE.match(key):
            raise SkillInvalid(f"requires.env 中的 {key!r} 不是合法环境变量名")
        if _env_blocked(key):
            raise SkillInvalid(f"requires.env 不允许依赖数据库凭据：{key}")

    upstream = obj.get("upstream") or {}
    if not isinstance(upstream, dict):
        raise SkillInvalid("upstream 必须是对象")
    upstream_clean = {
        key: _as_str(upstream.get(key), f"upstream.{key}", max_len=300)
        for key in ("name", "version", "url", "commit")
        if upstream.get(key) is not None
    }

    params = obj.get("params") or {}
    if not isinstance(params, dict):
        raise SkillInvalid("params 必须是对象（参数说明/schema）")

    return {
        "name": name,
        "version": version,
        "description": description,
        "entrypoint": entrypoint,
        "entry_path": entry_path,
        "enabled": _as_bool(obj.get("enabled"), "enabled", default=True),
        "hidden": _as_bool(obj.get("hidden"), "hidden", default=False),
        "read_only": True,
        "timeout_seconds": _as_int(
            obj.get("timeout_seconds"), "timeout_seconds",
            default=DEFAULT_TIMEOUT, lo=1, hi=MAX_TIMEOUT),
        "max_output_bytes": _as_int(
            obj.get("max_output_bytes"), "max_output_bytes",
            default=DEFAULT_MAX_OUTPUT, lo=MIN_MAX_OUTPUT, hi=MAX_MAX_OUTPUT),
        "capabilities": capabilities,
        "env_allow": env_allow,
        "requires": {"commands": req_commands, "env": req_env},
        "upstream": upstream_clean,
        "params": params,
        "path": str(skill_dir.resolve()),
    }


_MANIFEST_CACHE: dict[tuple[str, int], dict] = {}
_CACHE_LOCK = threading.Lock()


def _cached_manifest(skill_dir: Path) -> dict:
    manifest_path = skill_dir / MANIFEST_FILE
    try:
        mtime = manifest_path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = (str(skill_dir), mtime)
    with _CACHE_LOCK:
        hit = _MANIFEST_CACHE.get(key)
    if hit is not None:
        return hit
    manifest = _load_manifest(skill_dir)
    with _CACHE_LOCK:
        # 同一个目录的旧 mtime 条目清掉，其他 skill 的缓存保留；
        # 这样一次 list 不会反复清空/重读全部 manifest。
        for old_key in [k for k in _MANIFEST_CACHE if k[0] == str(skill_dir)]:
            _MANIFEST_CACHE.pop(old_key, None)
        _MANIFEST_CACHE[key] = manifest
    return manifest


def _availability(manifest: dict) -> tuple[str, list[str]]:
    """返回 (status, reasons)。status ∈ ready / disabled / unavailable。"""
    reasons: list[str] = []
    if _runtime_disabled():
        reasons.append("运行时已被 SA_SKILLS_DISABLED 关闭")
    if not manifest["enabled"]:
        reasons.append("manifest 中 enabled=false")
    for cmd in manifest["requires"]["commands"]:
        if shutil.which(cmd) is None:
            reasons.append(f"缺少命令：{cmd}")
    for key in manifest["requires"]["env"]:
        if not os.environ.get(key):
            reasons.append(f"缺少环境变量：{key}")
    if reasons:
        # 运行时开关/禁用优先显示 disabled，其余按 unavailable
        if _runtime_disabled() or not manifest["enabled"]:
            return "disabled", reasons
        return "unavailable", reasons
    return "ready", []


def _public_entry(manifest: dict) -> dict:
    status, reasons = _availability(manifest)
    return {
        "name": manifest["name"],
        "version": manifest["version"],
        "description": manifest["description"],
        "entrypoint": manifest["entrypoint"],
        "status": status,
        "reasons": reasons,
        "enabled": manifest["enabled"],
        "hidden": manifest["hidden"],
        "read_only": manifest["read_only"],
        "timeout_seconds": manifest["timeout_seconds"],
        "max_output_bytes": manifest["max_output_bytes"],
        "capabilities": manifest["capabilities"],
        "env_allow": manifest["env_allow"],
        "requires": manifest["requires"],
        "upstream": manifest["upstream"],
        "params": manifest["params"],
        "path": manifest["path"],
        "has_readme": (Path(manifest["path"]) / README_FILE).is_file(),
    }


def _iter_skill_dirs():
    if not SKILLS_DIR.is_dir():
        return
    for child in sorted(SKILLS_DIR.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue  # _template 等不参与扫描
        yield child


def list_skills(*, include_hidden: bool = False) -> list[dict]:
    out: list[dict] = []
    for skill_dir in _iter_skill_dirs():
        try:
            manifest = _cached_manifest(skill_dir)
        except SkillInvalid as exc:
            out.append({
                "name": skill_dir.name,
                "version": "",
                "description": "",
                "status": "invalid",
                "reasons": [exc.message],
                "error": exc.message,
                "path": str(skill_dir.resolve()),
            })
            continue
        entry = _public_entry(manifest)
        if entry["hidden"] and not include_hidden:
            continue
        out.append(entry)
    return out


def _find(name: str) -> dict:
    _validate_name(name)
    for entry in list_skills(include_hidden=True):
        if entry.get("name") == name:
            return entry
    raise SkillNotFound(f"未找到 skill：{name}")


def get_skill(name: str) -> dict:
    entry = _find(name)
    readme_path = Path(entry.get("path") or "") / README_FILE
    readme = ""
    truncated = False
    if readme_path.is_file():
        try:
            with readme_path.open("r", encoding="utf-8", errors="replace") as fh:
                readme = fh.read(MAX_README_BYTES + 1)
        except OSError as exc:
            readme = f"读取 {README_FILE} 失败：{exc}"
        if len(readme) > MAX_README_BYTES:
            readme = readme[:MAX_README_BYTES]
            truncated = True
    entry = dict(entry)
    entry["readme"] = readme
    entry["readme_truncated"] = truncated
    return entry


def check_skill(name: str) -> dict:
    """静态自检：只做 manifest/命令/环境变量检查，不启动子进程。"""
    _validate_name(name)
    skill_dir = SKILLS_DIR / name
    if not skill_dir.is_dir():
        raise SkillNotFound(f"未找到 skill：{name}")

    checks: list[dict] = []
    try:
        manifest = _cached_manifest(skill_dir)
    except SkillInvalid as exc:
        return {
            "ok": False,
            "name": name,
            "status": "invalid",
            "checks": [{"name": "manifest", "ok": False, "detail": exc.message}],
        }

    checks.append({"name": "manifest", "ok": True, "detail": f"v{manifest['version']}"})
    checks.append({
        "name": "entrypoint", "ok": True,
        "detail": manifest["entrypoint"],
    })
    for cmd in manifest["requires"]["commands"]:
        found = shutil.which(cmd)
        checks.append({
            "name": f"command:{cmd}", "ok": bool(found),
            "detail": found or "未安装/不在 PATH",
        })
    for key in manifest["requires"]["env"]:
        present = bool(os.environ.get(key))
        checks.append({
            "name": f"env:{key}", "ok": present,
            "detail": "已配置" if present else "未配置",
        })
    checks.append({
        "name": "enabled", "ok": manifest["enabled"],
        "detail": "manifest enabled=true" if manifest["enabled"] else "manifest enabled=false",
    })
    runtime_ok = not _runtime_disabled()
    checks.append({
        "name": "runtime", "ok": runtime_ok,
        "detail": "关闭（SA_SKILLS_DISABLED）" if not runtime_ok else "可用",
    })
    status, reasons = _availability(manifest)
    return {
        "ok": all(c["ok"] for c in checks),
        "name": name,
        "version": manifest["version"],
        "status": status,
        "reasons": reasons,
        "checks": checks,
    }


# ---------------------------------------------------------------- 执行

_RUN_LOCKS: dict[str, threading.Lock] = {}
_RUN_LOCKS_GUARD = threading.Lock()
_LOG_LOCK = threading.Lock()


def _run_lock(name: str) -> threading.Lock:
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _RUN_LOCKS[name] = lock
        return lock


def _build_env(manifest: dict, run_id: str) -> dict:
    env: dict[str, str] = {}
    for key in _ENV_BASE:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for key in manifest["env_allow"]:
        if _env_blocked(key):
            continue
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": os.environ.get("NO_PROXY", "*"),
        "no_proxy": os.environ.get("no_proxy", "*"),
        "SKILL_NAME": manifest["name"],
        "SKILL_VERSION": manifest["version"],
        "SKILL_DIR": manifest["path"],
        "SKILL_RUN_ID": run_id,
    })
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时/中断时连子进程树一起杀；否则 taskkill 只杀壳，孙进程会泄漏。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_limited(fh, limit: int) -> bytes:
    fh.seek(0)
    return fh.read(limit)


def _decode(raw: bytes, what: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillBadOutput(f"{what} 不是 UTF-8 编码：{exc}") from None


def _validate_envelope(obj: Any) -> tuple[bool, Any, list[str], list[str], dict]:
    if not isinstance(obj, dict):
        raise SkillBadOutput("stdout 顶层必须是 JSON 对象")
    if "ok" not in obj or not isinstance(obj["ok"], bool):
        raise SkillBadOutput('stdout 必须包含布尔字段 "ok"')
    ok = bool(obj["ok"])
    data = obj.get("data")

    citations = obj.get("citations", [])
    if citations is None:
        citations = []
    if not isinstance(citations, list):
        raise SkillBadOutput("citations 必须是数组")
    citations = [str(x)[:2000] for x in citations if isinstance(x, (str, int, float))][:MAX_CITATIONS]

    warnings = obj.get("warnings", [])
    if warnings is None:
        warnings = []
    if not isinstance(warnings, list):
        raise SkillBadOutput("warnings 必须是数组")
    warnings = [str(x)[:500] for x in warnings if isinstance(x, (str, int, float))][:MAX_WARNINGS]

    extra: dict = {}
    upstream_version = obj.get("upstream_version")
    if isinstance(upstream_version, str) and upstream_version.strip():
        extra["upstream_version"] = upstream_version.strip()[:100]
    if ok is False and isinstance(obj.get("error"), str):
        extra["error"] = obj["error"][:500]
    return ok, data, citations, warnings, extra


def _log_run(record: dict) -> None:
    """写审计日志：只记元数据，不记参数值和输出内容。"""
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            try:
                if RUN_LOG.exists() and RUN_LOG.stat().st_size > RUN_LOG_MAX_BYTES:
                    rotated = RUN_LOG.parent / (RUN_LOG.name + ".1")
                    try:
                        if rotated.exists():
                            rotated.unlink()
                    except OSError:
                        pass
                    os.replace(RUN_LOG, rotated)
            except OSError:
                pass
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with RUN_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        # 审计日志写失败绝不能影响 skill 本身的结果
        pass


def run_skill(
    name: str,
    params: dict | None = None,
    timeout_seconds: int | None = None,
    *,
    actor: str = "api",
) -> dict:
    """执行一个 skill，返回合并了运行元数据的 JSON 契约。

    - 进程正常结束且 stdout 合法 ⇒ 返回 {"ok": <skill 的 ok>, ...}（HTTP 200）
    - 进程/契约层失败 ⇒ 抛 SkillError 子类（由 API 映射 4xx/5xx）
    """
    _validate_name(name)
    entry = _find(name)
    if entry.get("status") == "invalid":
        raise SkillInvalid(entry.get("error") or "manifest 不合法", detail={"skill": entry})
    if entry.get("status") == "disabled":
        reasons = entry.get("reasons") or []
        msg = "skill 已禁用" + ("：" + "；".join(reasons) if reasons else "")
        raise SkillUnavailable(msg, detail={"reasons": reasons})
    if entry.get("status") != "ready":
        reasons = entry.get("reasons") or []
        msg = "skill 当前不可用" + ("：" + "；".join(reasons) if reasons else "")
        raise SkillUnavailable(msg, detail={"reasons": reasons})

    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise SkillInvalid("params 必须是 JSON 对象")
    try:
        payload_bytes = json.dumps(
            {"params": params}, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SkillInvalid(f"params 无法序列化为 JSON：{exc}") from None
    if len(payload_bytes) > MAX_PARAMS_BYTES:
        raise SkillInvalid(f"params 超过 {MAX_PARAMS_BYTES // 1024} KB 上限")

    manifest = _cached_manifest(Path(entry["path"]))
    manifest_timeout = int(manifest["timeout_seconds"])
    if timeout_seconds is None:
        timeout = manifest_timeout
    else:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise SkillInvalid("timeout_seconds 必须是整数")
        if not (1 <= timeout_seconds <= MAX_TIMEOUT):
            raise SkillInvalid(f"timeout_seconds 必须在 1..{MAX_TIMEOUT} 之间")
        timeout = min(timeout_seconds, manifest_timeout)
    max_output = int(manifest["max_output_bytes"])

    run_id = uuid.uuid4().hex[:12]
    started_at = time.time()
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started_at))
    lock = _run_lock(name)
    if not lock.acquire(blocking=False):
        raise SkillBusy(f"skill {name} 正在运行，请稍后再试")

    base_record = {
        "run_id": run_id,
        "name": name,
        "version": manifest["version"],
        "started_at": started_iso,
        "actor": actor,
        "param_keys": sorted(str(k)[:64] for k in params.keys()),
    }

    try:
        env = _build_env(manifest, run_id)
        entry_path = manifest["entry_path"]
        skill_dir = Path(manifest["path"])
        kwargs: dict[str, Any] = dict(
            cwd=str(skill_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=None,   # 下面换成临时文件
            stderr=None,
            close_fds=True,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True

        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            kwargs["stdout"] = out_f
            kwargs["stderr"] = err_f
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-u", str(entry_path)],
                    **kwargs,
                )
            except OSError as exc:
                raise SkillRunFailed(f"无法启动子进程：{exc}") from None

            timed_out = False
            try:
                proc.communicate(input=payload_bytes, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                try:
                    proc.communicate(timeout=10)
                except Exception:
                    pass

            duration_ms = int((time.time() - started_at) * 1000)
            if timed_out:
                _log_run({**base_record, "ok": False, "error_code": "timeout",
                          "duration_ms": duration_ms, "timeout_seconds": timeout})
                raise SkillTimeout(
                    f"skill {name} 超过 {timeout} 秒未结束，已终止",
                    detail={"run_id": run_id, "timeout_seconds": timeout},
                )

            exit_code = proc.returncode
            if exit_code != 0:
                err_raw = _read_limited(err_f, 8192)
                err_text = err_raw.decode("utf-8", errors="replace").strip()
                _log_run({**base_record, "ok": False, "error_code": "exit_nonzero",
                          "duration_ms": duration_ms, "exit_code": exit_code})
                raise SkillRunFailed(
                    f"skill {name} 退出码 {exit_code}",
                    detail={
                        "run_id": run_id,
                        "exit_code": exit_code,
                        "stderr": err_text[-4000:],
                    },
                )

            raw = _read_limited(out_f, max_output + 1)
            output_bytes = len(raw)
            if output_bytes > max_output:
                _log_run({**base_record, "ok": False, "error_code": "output_too_large",
                          "duration_ms": duration_ms, "output_bytes": output_bytes})
                raise SkillBadOutput(
                    f"skill {name} 输出超过 {max_output // 1024} KB 上限，已拒绝解析",
                    detail={"run_id": run_id, "output_bytes": output_bytes,
                            "limit": max_output},
                )

            text = _decode(raw, f"skill {name} 的 stdout").lstrip("\ufeff").strip()
            if not text:
                _log_run({**base_record, "ok": False, "error_code": "empty_output",
                          "duration_ms": duration_ms, "output_bytes": 0})
                raise SkillBadOutput(
                    f"skill {name} 没有向 stdout 输出 JSON",
                    detail={"run_id": run_id, "output_bytes": 0},
                )
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                _log_run({**base_record, "ok": False, "error_code": "invalid_json",
                          "duration_ms": duration_ms, "output_bytes": output_bytes})
                raise SkillBadOutput(
                    f"stdout 不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
                    detail={"run_id": run_id, "output_bytes": output_bytes},
                )

            ok, data, citations, warnings, extra = _validate_envelope(obj)
            _log_run({**base_record, "ok": ok, "error_code": "" if ok else "skill_reported_failure",
                      "duration_ms": duration_ms, "exit_code": 0,
                      "output_bytes": output_bytes})

            result = {
                "ok": ok,
                "data": data,
                "citations": citations,
                "warnings": warnings,
                "run": {
                    "run_id": run_id,
                    "name": name,
                    "version": manifest["version"],
                    "duration_ms": duration_ms,
                    "output_bytes": output_bytes,
                    "started_at": started_iso,
                    "actor": actor,
                },
            }
            result.update(extra)
            return result
    finally:
        lock.release()


def reload_skills() -> list[dict]:
    """清掉 manifest 缓存后重新扫描（新增/修改 skill 不需要重启服务）。"""
    with _CACHE_LOCK:
        _MANIFEST_CACHE.clear()
    return list_skills()


def runtime_info() -> dict:
    return {
        "skills_dir": str(SKILLS_DIR),
        "run_log": str(RUN_LOG),
        "disabled": _runtime_disabled(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }


def recent_runs(limit: int = 50, name: str | None = None) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 200):
        raise SkillInvalid("limit 必须在 1..200 之间")
    if name is not None:
        _validate_name(name)
    if not RUN_LOG.is_file():
        return []
    rows: list[dict] = []
    try:
        with RUN_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if name is not None and item.get("name") != name:
                    continue
                rows.append(item)
    except OSError:
        return []
    return rows[-limit:][::-1]
