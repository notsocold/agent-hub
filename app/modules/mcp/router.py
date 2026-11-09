"""MCP 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.core.deps import CurrentUser, DbSession
from app.modules.mcp import service
from app.modules.mcp.schemas import (
    McpAvailableListOut,
    McpCheckResult,
    McpServerCreateRequest,
    McpServerEnabledRequest,
    McpServerListOut,
    McpServerOut,
    McpServerUpdateRequest,
    McpToolOut,
    McpToolPatchRequest,
)

router = APIRouter(prefix="/mcp", tags=["MCP"])


@router.get("/servers", response_model=McpServerListOut)
async def list_servers(user: CurrentUser, db: DbSession) -> McpServerListOut:
    return await service.list_servers(db, user)


@router.get("/available", response_model=McpAvailableListOut)
async def available(user: CurrentUser, db: DbSession) -> McpAvailableListOut:
    return await service.available(db, user)


@router.post("/servers", response_model=McpServerOut, status_code=201)
async def create_server(
    payload: McpServerCreateRequest, user: CurrentUser, db: DbSession
) -> McpServerOut:
    return await service.create_server(db, user, payload)


@router.get("/servers/{server_id}", response_model=McpServerOut)
async def get_server(server_id: uuid.UUID, user: CurrentUser, db: DbSession) -> McpServerOut:
    return await service.get_server(db, user, server_id)


@router.patch("/servers/{server_id}", response_model=McpServerOut)
async def update_server(
    server_id: uuid.UUID,
    payload: McpServerUpdateRequest,
    user: CurrentUser,
    db: DbSession,
) -> McpServerOut:
    return await service.update_server(db, user, server_id, payload)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await service.delete_server(db, user, server_id)


@router.put("/servers/{server_id}/enabled", response_model=McpServerOut)
async def set_server_enabled(
    server_id: uuid.UUID,
    payload: McpServerEnabledRequest,
    user: CurrentUser,
    db: DbSession,
) -> McpServerOut:
    return await service.set_server_enabled(db, user, server_id, payload.enabled)


@router.post("/servers/{server_id}/check", response_model=McpCheckResult)
async def check_server(server_id: uuid.UUID, user: CurrentUser, db: DbSession) -> McpCheckResult:
    return await service.check_server(db, user, server_id)


@router.post("/servers/{server_id}/refresh-tools", response_model=McpCheckResult)
async def refresh_tools(server_id: uuid.UUID, user: CurrentUser, db: DbSession) -> McpCheckResult:
    return await service.refresh_tools(db, user, server_id)


@router.get("/servers/{server_id}/tools", response_model=list[McpToolOut])
async def list_tools(server_id: uuid.UUID, user: CurrentUser, db: DbSession) -> list[McpToolOut]:
    return await service.list_tools(db, user, server_id)


@router.patch("/tools/{tool_id}", response_model=McpToolOut)
async def patch_tool(
    tool_id: uuid.UUID, payload: McpToolPatchRequest, user: CurrentUser, db: DbSession
) -> McpToolOut:
    return await service.patch_tool(db, user, tool_id, payload)
