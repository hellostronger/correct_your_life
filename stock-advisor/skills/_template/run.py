#!/usr/bin/env python3
"""Skill 入口模板。

运行时约定：
  - stdin:  {"params": {...}}
  - stdout: 恰好一个 JSON 对象 {"ok": bool, "data": ..., "citations": [...], "warnings": [...]}
  - 日志写 stderr，不要写 stdout（stdout 只留给契约 JSON）
  - 工作目录已切到本 skill 目录
"""

import json
import sys


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(f"invalid stdin json: {exc}", file=sys.stderr)
        return 2

    params = payload.get("params")
    if not isinstance(params, dict):
        print("params must be an object", file=sys.stderr)
        return 2

    # TODO: 在这里调用上游 CLI / HTTP / MCP。
    # 注意：不要 import 上游 Python 包，也不要复制上游源码进来。
    data = {"echo": params}

    json.dump(
        {
            "ok": True,
            "data": data,
            "citations": [],
            "warnings": ["这是模板输出，请替换为真实实现"],
        },
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
