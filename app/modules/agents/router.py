"""远程 Agent 管理端点（``/api/v1/agents``）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Response, status

from app.api.schemas import ErrorEnvelopeOut
from app.core.deps import AdminUser, CurrentUser, DbSession
from app.modules.agents import service
from app.modules.agents.schemas import (
    AgentCreateRequest,
    AgentDetailOut,
    AgentHealthResult,
    AgentPage,
    AgentPreviewRequest,
    AgentPreviewResult,
    AgentRefreshResult,
    AgentUpdateRequest,
)

router = APIRouter(prefix="/agents", tags=["agents"])

# 写操作的管理员门禁 403：覆盖 include 级通用 403（capability_disabled 口径）。
_ADMIN_WRITE_RESPONSES: dict[int | str, dict] = {
    403: {
        "model": ErrorEnvelopeOut,
        "description": "需要管理员权限（`forbidden`，`details.required_role=\"admin\"`）",
    }
}


@router.post("/preview", response_model=AgentPreviewResult)
async def preview_agent(payload: AgentPreviewRequest, user: CurrentUser) -> AgentPreviewResult:
    """拉取并校验 Agent Card；不落库。"""
    del user
    return await service.preview_agent(payload)


@router.post(
    "",
    response_model=AgentDetailOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ADMIN_WRITE_RESPONSES,
)
async def create_agent(
    payload: AgentCreateRequest, db: DbSession, admin: AdminUser
) -> AgentDetailOut:
    """注册远程 Agent（仅管理员）；同 url+slug 幂等返回已有记录。"""
    return await service.create_agent(db, admin, payload)


@router.get("", response_model=AgentPage)
async def list_agents(
    db: DbSession,
    user: CurrentUser,
    health_status: str | None = Query(default=None, max_length=16),
    enabled: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
) -> AgentPage:
    del user
    return await service.list_agents(
        db, health_status=health_status, enabled=enabled, q=q, limit=limit, cursor=cursor
    )


@router.get("/{agent_id}", response_model=AgentDetailOut)
async def get_agent(agent_id: uuid.UUID, db: DbSession, user: CurrentUser) -> AgentDetailOut:
    del user
    return await service.get_agent(db, agent_id)


@router.patch("/{agent_id}", response_model=AgentDetailOut, responses=_ADMIN_WRITE_RESPONSES)
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdateRequest,
    db: DbSession,
    admin: AdminUser,
) -> AgentDetailOut:
    return await service.update_agent(db, admin, agent_id, payload)


@router.delete(
    "/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ADMIN_WRITE_RESPONSES
)
async def delete_agent(agent_id: uuid.UUID, db: DbSession, admin: AdminUser) -> Response:
    await service.delete_agent(db, admin, agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{agent_id}/refresh", response_model=AgentRefreshResult, responses=_ADMIN_WRITE_RESPONSES
)
async def refresh_agent(
    agent_id: uuid.UUID, db: DbSession, admin: AdminUser
) -> AgentRefreshResult:
    return await service.refresh_agent(db, admin, agent_id)


@router.post(
    "/{agent_id}/health", response_model=AgentHealthResult, responses=_ADMIN_WRITE_RESPONSES
)
async def health_check_agent(
    agent_id: uuid.UUID, db: DbSession, admin: AdminUser
) -> AgentHealthResult:
    return await service.health_check_agent(db, admin, agent_id)
