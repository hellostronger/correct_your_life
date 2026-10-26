"""Skill REST 端点。

挂在主应用上（app.include_router(skill_api.router)），前缀 /api/skills：

    GET    /api/skills                 列出所有 skill 及其可用状态
    GET    /api/skills/runs            最近运行记录（审计日志尾部）
    POST   /api/skills/reload          重新扫描 skills/（不重启服务）
    GET    /api/skills/{name}          单个 skill 详情（含 SKILL.md）
    POST   /api/skills/{name}/check    静态自检（manifest / 命令 / 环境变量）
    POST   /api/skills/{name}/run      执行（stdin 传 params，stdout 收 JSON 契约）

分层原则：这里只做 HTTP 映射，所有校验/隔离/日志都在 skill_runtime。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import skill_runtime as rt

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillRunIn(BaseModel):
    params: dict = Field(default_factory=dict)
    timeout_seconds: int | None = None


def _http_error(exc: rt.SkillError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.detail:
        detail.update(exc.detail)
    return HTTPException(status_code=exc.http_status, detail=detail)


@router.get("")
def list_skills() -> dict:
    skills = rt.list_skills()
    return {
        "ok": True,
        "count": len(skills),
        "skills": skills,
        "runtime": rt.runtime_info(),
    }


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    name: str | None = Query(default=None),
) -> dict:
    try:
        runs = rt.recent_runs(limit=limit, name=name)
    except rt.SkillError as exc:
        raise _http_error(exc) from None
    return {"ok": True, "runs": runs, "count": len(runs)}


@router.post("/reload")
def reload_skills() -> dict:
    skills = rt.reload_skills()
    return {
        "ok": True,
        "count": len(skills),
        "skills": skills,
        "runtime": rt.runtime_info(),
    }


@router.get("/{name}")
def get_skill(name: str) -> dict:
    try:
        return rt.get_skill(name)
    except rt.SkillError as exc:
        raise _http_error(exc) from None


@router.post("/{name}/check")
def check_skill(name: str) -> dict:
    try:
        return rt.check_skill(name)
    except rt.SkillError as exc:
        raise _http_error(exc) from None


@router.post("/{name}/run")
def run_skill(name: str, body: SkillRunIn | None = None) -> dict:
    body = body or SkillRunIn()
    try:
        return rt.run_skill(
            name,
            params=body.params,
            timeout_seconds=body.timeout_seconds,
        )
    except rt.SkillError as exc:
        raise _http_error(exc) from None
