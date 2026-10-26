"""十个开源项目的统一接入矩阵（``/api/projects``）。

只读探针：不写数据库、不触发交易、不改上游配置。前端拿到的是
``{projects, summary, checked_at}``，每个项目固定 ``live/ready/offline/blocked/missing``
之一；任何一个上游挂掉都只让对应行变灰，不影响行情/持仓/通知。

探针细节在 ``project_hub.py``：HTTP 健康、CLI 预检、venv import，子进程只拿最小
环境变量（不传 ``DB_*`` / API key）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import project_hub

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects(refresh: bool = Query(False, description="true 时绕过 30s 缓存")) -> dict:
    return project_hub.check_all(force=refresh)


@router.get("/{project_id}")
def check_project(project_id: str) -> dict:
    res = project_hub.check_one(project_id)
    if not res.get("ok"):
        raise HTTPException(
            404,
            detail={
                "code": "unknown_project",
                "message": res.get("error") or project_id,
                "known": sorted(project_hub._BY_ID),
            },
        )
    return res["data"]
