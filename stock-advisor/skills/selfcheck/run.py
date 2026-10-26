#!/usr/bin/env python3
"""运行时自检 skill：不依赖网络/数据库，只回报自身环境。

故意把「数据库凭据有没有泄漏」写成可见数据：这是运行时最重要的一条边界，
每次接入新 skill 都应该能亲眼确认它没有拿到 DB_* / PG* / DATABASE_URL。
"""

import json
import os
import platform
import sys
import time
from pathlib import Path

MAX_STDIN = 64 * 1024
DB_PREFIXES = ("DB_", "PG")
DB_EXACT = {"DATABASE_URL"}


def _db_leaks() -> list[str]:
    return sorted(
        key for key in os.environ
        if key.upper().startswith(DB_PREFIXES) or key.upper() in DB_EXACT
    )


def main() -> int:
    raw = sys.stdin.read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        print("stdin too large", file=sys.stderr)
        return 2

    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"invalid stdin json: {exc}", file=sys.stderr)
        return 2

    params = payload.get("params")
    if not isinstance(params, dict):
        print("params must be an object", file=sys.stderr)
        return 2

    sleep_seconds = params.get("sleep_seconds", 0)
    try:
        sleep_seconds = float(sleep_seconds or 0)
    except (TypeError, ValueError):
        sleep_seconds = 0
    sleep_seconds = max(0.0, min(sleep_seconds, 3.0))
    if sleep_seconds:
        time.sleep(sleep_seconds)

    leaks = _db_leaks()
    data = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
        "skill": {
            "name": os.environ.get("SKILL_NAME"),
            "version": os.environ.get("SKILL_VERSION"),
            "dir": os.environ.get("SKILL_DIR"),
            "run_id": os.environ.get("SKILL_RUN_ID"),
        },
        "stdin_params": params,
        "slept_seconds": sleep_seconds,
        "db_env_leaked": leaks,
        "utc_now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    warnings = []
    if leaks:
        warnings.append(
            "检测到数据库相关环境变量进入了子进程：" + ", ".join(leaks)
        )

    if params.get("fail") is True:
        result = {
            "ok": False,
            "data": {"reason": "params.fail=true（这是一次预期的业务失败）", "env": data},
            "citations": [],
            "warnings": warnings,
        }
    else:
        result = {
            "ok": True,
            "data": data,
            "citations": [],
            "warnings": warnings,
        }

    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
