"""MCP 业务：服务器 CRUD、健康检查与工具发现、聊天工具装配。"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.errors import (
    CapabilityDisabledError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.ssrf import normalize_headers, resolve_headers, validate_mcp_url
from app.core.timeutil import elapsed_ms
from app.db.models.mcp import McpServer, McpTool, UserCapabilityOverride
from app.db.models.user import User
from app.modules.mcp.client import StdioMcpClient, ToolInfo, build_client
from app.modules.mcp.schemas import (
    McpAvailableListOut,
    McpAvailableServerOut,
    McpCheckResult,
    McpServerCreateRequest,
    McpServerListOut,
    McpServerOut,
    McpServerUpdateRequest,
    McpToolOut,
    McpToolPatchRequest,
)

logger = logging.getLogger(__name__)

_EXPOSED_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")

# 测试注入：按 URL 注册 httpx transport，绕过真实网络。
_test_transports: dict[str, httpx.AsyncBaseTransport] = {}


def register_test_transport(url: str, transport: httpx.AsyncBaseTransport) -> None:
    _test_transports[url.rstrip("/")] = transport


def clear_test_transports() -> None:
    _test_transports.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def _require_enabled() -> None:
    if not get_settings().mcp_enabled:
        raise CapabilityDisabledError("MCP 能力未启用")


async def _effective_enabled(db: AsyncSession, server: McpServer, user: User | None) -> bool:
    if server.force_disabled:
        return False
    if server.scope == "user":
        return server.enabled
    if user is None:
        return server.enabled
    override = await db.scalar(
        select(UserCapabilityOverride.enabled).where(
            UserCapabilityOverride.user_id == user.id,
            UserCapabilityOverride.capability == "mcp_server",
            UserCapabilityOverride.target_id == server.id,
        )
    )
    return server.enabled if override is None else override


def _server_out(
    server: McpServer, *, effective: bool, tool_count: int = 0
) -> McpServerOut:
    return McpServerOut(
        id=server.id,
        scope=server.scope,
        slug=server.slug,
        name=server.name,
        description=server.description,
        transport=server.transport,
        url=server.url,
        enabled=server.enabled,
        auto_inject=server.auto_inject,
        force_disabled=server.force_disabled,
        effective_enabled=effective,
        health_status=server.health_status,  # type: ignore[arg-type]
        last_checked_at=server.last_checked_at,
        last_error=server.last_error,
        latency_ms=server.latency_ms,
        tool_count=tool_count,
        credentials_hint=server.credentials_hint,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


async def _tool_out(db: AsyncSession, tool: McpTool, user: User) -> McpToolOut:
    server = await db.get(McpServer, tool.server_id)
    user_allowed = tool.user_allowed
    if server is not None and server.scope == "system" and tool.admin_only and user.role != "admin":
        user_allowed = False
    return McpToolOut(
        id=tool.id,
        server_id=tool.server_id,
        name=tool.name,
        exposed_name=tool.exposed_name,
        description=tool.description,
        input_schema=tool.input_schema or {},
        risk=tool.risk,  # type: ignore[arg-type]
        requires_approval=tool.requires_approval,
        admin_only=tool.admin_only,
        user_allowed=user_allowed,
        enabled=tool.enabled,
        discovered_at=tool.discovered_at,
        updated_at=tool.updated_at,
    )


async def list_servers(db: AsyncSession, user: User) -> McpServerListOut:
    _require_enabled()
    rows = list(
        (
            await db.execute(
                select(McpServer)
                .where((McpServer.scope == "system") | (McpServer.owner_user_id == user.id))
                .order_by(McpServer.created_at.desc())
            )
        ).scalars()
    )
    counts = dict(
        (
            await db.execute(
                select(McpTool.server_id, func.count(McpTool.id)).group_by(McpTool.server_id)
            )
        ).all()
    )
    stats = {"total": len(rows), "system": 0, "user": 0, "enabled": 0, "healthy": 0}
    items: list[McpServerOut] = []
    for row in rows:
        effective = await _effective_enabled(db, row, user)
        stats[row.scope] += 1
        if effective:
            stats["enabled"] += 1
        if row.health_status == "healthy":
            stats["healthy"] += 1
        items.append(_server_out(row, effective=effective, tool_count=int(counts.get(row.id, 0))))
    return McpServerListOut(items=items, stats=stats)


async def available(db: AsyncSession, user: User) -> McpAvailableListOut:
    """输入框选择器用的精简目录。"""
    _require_enabled()
    rows = list(
        (
            await db.execute(
                select(McpServer)
                .where((McpServer.scope == "system") | (McpServer.owner_user_id == user.id))
                .order_by(McpServer.scope.desc(), McpServer.created_at.desc())
            )
        ).scalars()
    )
    counts = dict(
        (
            await db.execute(
                select(McpTool.server_id, func.count(McpTool.id)).group_by(McpTool.server_id)
            )
        ).all()
    )
    items: list[McpAvailableServerOut] = []
    for row in rows:
        effective = await _effective_enabled(db, row, user)
        items.append(
            McpAvailableServerOut(
                id=row.id,
                scope=row.scope,
                slug=row.slug,
                name=row.name,
                transport=row.transport,
                effective_enabled=effective,
                health_status=row.health_status,  # type: ignore[arg-type]
                tool_count=int(counts.get(row.id, 0)),
            )
        )
    return McpAvailableListOut(items=items)


async def _load_visible(db: AsyncSession, user: User, server_id: uuid.UUID) -> McpServer:
    _require_enabled()
    server = await db.get(McpServer, server_id)
    if server is None or (server.scope == "user" and server.owner_user_id != user.id):
        raise NotFoundError("MCP 服务器不存在")
    return server


async def get_server(db: AsyncSession, user: User, server_id: uuid.UUID) -> McpServerOut:
    server = await _load_visible(db, user, server_id)
    count = await db.scalar(
        select(func.count(McpTool.id)).where(McpTool.server_id == server.id)
    )
    effective = await _effective_enabled(db, server, user)
    return _server_out(server, effective=effective, tool_count=int(count or 0))


def _validate_create(payload: McpServerCreateRequest, user: User) -> None:
    if payload.transport == "stdio":
        if user.role != "admin":
            raise ValidationFailedError(
                "普通用户只能创建远程 URL 的 MCP 服务器（stdio 未开放）",
                code="mcp_transport_not_allowed",
            )
        if not payload.command:
            raise ValidationFailedError("stdio 传输必须提供 command")
        allowlist = get_settings().mcp_stdio_command_allowlist
        if allowlist and payload.command.split("/")[-1] not in allowlist:
            raise ValidationFailedError(
                "stdio 命令不在白名单内", code="mcp_transport_not_allowed"
            )
        return
    if not payload.url:
        raise ValidationFailedError("远程传输必须提供 url")
    validate_mcp_url(payload.url)


async def create_server(
    db: AsyncSession, user: User, payload: McpServerCreateRequest
) -> McpServerOut:
    _require_enabled()
    _validate_create(payload, user)
    settings = get_settings()
    own_count = await db.scalar(
        select(func.count(McpServer.id)).where(
            McpServer.scope == "user", McpServer.owner_user_id == user.id
        )
    )
    if int(own_count or 0) >= settings.mcp_max_servers_per_user:
        raise ValidationFailedError(
            f"每人最多创建 {settings.mcp_max_servers_per_user} 个 MCP 服务器"
        )
    system_slug = await db.scalar(
        select(McpServer.id).where(McpServer.scope == "system", McpServer.slug == payload.slug)
    )
    if system_slug is not None:
        raise ConflictError("slug 与系统服务器冲突")
    stored_headers, hint = normalize_headers(payload.headers)
    server = McpServer(
        scope="user",
        owner_user_id=user.id,
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        transport=payload.transport,
        url=payload.url,
        command=payload.command,
        args=list(payload.args),
        headers=stored_headers,
        credentials_hint=hint,
        enabled=True,
        auto_inject=True,
        force_disabled=False,
        health_status="unknown",
    )
    db.add(server)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="mcp.server.create",
        resource_type="mcp_server",
        resource_id=str(server.id),
        detail={"slug": payload.slug, "transport": payload.transport, "url": payload.url},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("slug 已存在") from exc
    await db.refresh(server)
    # 创建后立即做一次健康检查与工具发现；失败也保留配置（前端可修正）。
    await _check_and_sync(db, server, force=True)
    await db.refresh(server)
    return await get_server(db, user, server.id)


async def update_server(
    db: AsyncSession, user: User, server_id: uuid.UUID, payload: McpServerUpdateRequest
) -> McpServerOut:
    server = await _load_visible(db, user, server_id)
    if server.scope != "user":
        raise ForbiddenError("系统 MCP 服务器不可修改")
    if payload.url is not None:
        if not payload.url:
            raise ValidationFailedError("远程传输必须提供 url")
        validate_mcp_url(payload.url)
        server.url = payload.url
    if payload.name is not None:
        server.name = payload.name.strip()
    if payload.description is not None:
        server.description = payload.description.strip()
    if payload.auto_inject is not None:
        server.auto_inject = payload.auto_inject
    if payload.headers is not None:
        stored_headers, hint = normalize_headers(payload.headers)
        server.headers = stored_headers
        server.credentials_hint = hint
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="mcp.server.update",
        resource_type="mcp_server",
        resource_id=str(server_id),
    )
    await db.commit()
    await db.refresh(server)
    await _check_and_sync(db, server, force=True)
    await db.refresh(server)
    return await get_server(db, user, server.id)


async def delete_server(db: AsyncSession, user: User, server_id: uuid.UUID) -> None:
    server = await _load_visible(db, user, server_id)
    if server.scope != "user":
        raise ForbiddenError("系统 MCP 服务器不可删除")
    await db.delete(server)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="mcp.server.delete",
        resource_type="mcp_server",
        resource_id=str(server_id),
        detail={"slug": server.slug},
    )
    await db.commit()


async def set_server_enabled(
    db: AsyncSession, user: User, server_id: uuid.UUID, enabled: bool
) -> McpServerOut:
    server = await _load_visible(db, user, server_id)
    if server.scope == "user":
        server.enabled = enabled
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="mcp.server.enable",
            resource_type="mcp_server",
            resource_id=str(server_id),
            detail={"enabled": enabled},
        )
    else:
        override = await db.get(UserCapabilityOverride, (user.id, "mcp_server", server.id))
        if override is None:
            override = UserCapabilityOverride(
                user_id=user.id, capability="mcp_server", target_id=server.id, enabled=enabled
            )
            db.add(override)
        else:
            override.enabled = enabled
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="capability.override",
            resource_type="mcp_server",
            resource_id=str(server_id),
            detail={"capability": "mcp_server", "enabled": enabled},
        )
    await db.commit()
    await db.refresh(server)
    effective = await _effective_enabled(db, server, user)
    count = await db.scalar(
        select(func.count(McpTool.id)).where(McpTool.server_id == server.id)
    )
    return _server_out(server, effective=effective, tool_count=int(count or 0))


def _client_for(server: McpServer) -> Any:
    headers = resolve_headers(server.headers or {})
    if server.transport == "stdio":
        if not server.command:
            raise ValidationFailedError("stdio 传输缺少 command")
        allowlist = get_settings().mcp_stdio_command_allowlist
        if allowlist and server.command.split("/")[-1] not in allowlist:
            raise ValidationFailedError("stdio 命令不在白名单内", code="mcp_transport_not_allowed")
        return StdioMcpClient(command=server.command, args=list(server.args or []))
    transport = _test_transports.get((server.url or "").rstrip("/"))
    return build_client(
        transport=server.transport,
        url=server.url,
        command=server.command,
        args=list(server.args or []),
        headers=headers,
        http_transport=transport,
    )


def _risk_of(annotations: dict[str, Any]) -> str:
    if annotations.get("destructiveHint") is True:
        return "destructive"
    if annotations.get("readOnlyHint") is True:
        return "read"
    if annotations.get("idempotentHint") is True:
        return "read"
    return "unknown"


def _exposed_name(slug: str, tool_name: str) -> str:
    base = f"mcp__{slug}__{_EXPOSED_PATTERN.sub('_', tool_name)}"
    if len(base) <= 64:
        return base
    digest = hashlib.sha256(tool_name.encode()).hexdigest()[:6]
    return f"{base[:56]}__{digest}"


async def _sync_tools(db: AsyncSession, server: McpServer, tools: list[ToolInfo]) -> int:
    settings = get_settings()
    existing = {
        item.name: item
        for item in (
            await db.execute(select(McpTool).where(McpTool.server_id == server.id))
        ).scalars()
    }
    now = _now()
    seen: set[str] = set()
    discovered = 0
    for index, info in enumerate(tools):
        if index >= settings.mcp_max_tools_per_server:
            break
        if not info.name or info.name in seen:
            continue
        seen.add(info.name)
        risk = _risk_of(info.annotations)
        is_system_write = server.scope == "system" and risk != "read"
        exposed = _exposed_name(server.slug, info.name)
        # 同服务器内 exposed_name 冲突时追加短哈希。
        if await db.scalar(
            select(McpTool.id).where(
                McpTool.server_id == server.id,
                McpTool.exposed_name == exposed,
                McpTool.name != info.name,
            )
        ):
            exposed = _exposed_name(server.slug, f"{info.name}-{hashlib.sha256(info.name.encode()).hexdigest()[:6]}")
        row = existing.get(info.name)
        if row is None:
            row = McpTool(
                server_id=server.id,
                name=info.name,
                exposed_name=exposed,
                description=info.description,
                input_schema=info.input_schema or {},
                annotations=info.annotations,
                risk=risk,
                requires_approval=risk != "read",
                admin_only=is_system_write,
                user_allowed=not is_system_write,
                enabled=True,
                discovered_at=now,
            )
            db.add(row)
        else:
            row.exposed_name = exposed
            row.description = info.description
            row.input_schema = info.input_schema or {}
            row.annotations = info.annotations
            row.risk = risk
            row.admin_only = is_system_write
            row.user_allowed = not is_system_write
            # requires_approval / enabled 属于用户策略，刷新时保留已有配置。
            row.updated_at = now
        discovered += 1
    gone = [name for name in existing if name not in seen]
    if gone:
        await db.execute(
            delete(McpTool).where(McpTool.server_id == server.id, McpTool.name.in_(gone))
        )
    server.tools_synced_at = now
    return discovered


async def _check_and_sync(
    db: AsyncSession, server: McpServer, *, force: bool = False
) -> McpCheckResult:
    """执行连通性检查并同步工具缓存；不抛连接错误，统一写回健康状态。"""
    settings = get_settings()
    if not force and server.tools_synced_at is not None:
        age = (_now() - server.tools_synced_at).total_seconds()
        if age < settings.mcp_tool_cache_ttl_seconds:
            return McpCheckResult(
                id=server.id,
                health_status=server.health_status,  # type: ignore[arg-type]
                latency_ms=server.latency_ms,
                message="使用缓存结果",
                tools_discovered=await db.scalar(
                    select(func.count(McpTool.id)).where(McpTool.server_id == server.id)
                ) or 0,
                last_checked_at=server.last_checked_at,
            )
    started = datetime.now(UTC)
    try:
        client = _client_for(server)
        status, latency, _ = await client.check()
        tools = await client.list_tools()
        discovered = await _sync_tools(db, server, tools)
        server.health_status = status
        server.latency_ms = latency
        server.last_checked_at = _now()
        server.last_error = None
        await db.commit()
        await write_audit(
            db,
            actor_type="system",
            actor_id="",
            action="mcp.server.check",
            resource_type="mcp_server",
            resource_id=str(server.id),
            detail={"healthy": True, "tools": discovered, "latency_ms": latency},
            commit=True,
        )
        return McpCheckResult(
            id=server.id,
            health_status=status,  # type: ignore[arg-type]
            latency_ms=latency,
            message=f"连接成功，发现 {discovered} 个工具",
            tools_discovered=discovered,
            last_checked_at=server.last_checked_at,
        )
    except Exception as exc:
        elapsed = elapsed_ms(started)
        message = str(exc)[:400]
        server.health_status = "unhealthy"
        server.latency_ms = elapsed
        server.last_checked_at = _now()
        server.last_error = message
        await db.commit()
        await write_audit(
            db,
            actor_type="system",
            actor_id="",
            action="mcp.server.check",
            resource_type="mcp_server",
            resource_id=str(server.id),
            detail={"healthy": False, "error": message[:200]},
            commit=True,
        )
        return McpCheckResult(
            id=server.id,
            health_status="unhealthy",
            latency_ms=elapsed,
            message=message,
            tools_discovered=await db.scalar(
                select(func.count(McpTool.id)).where(McpTool.server_id == server.id)
            ) or 0,
            last_checked_at=server.last_checked_at,
        )


async def check_server(db: AsyncSession, user: User, server_id: uuid.UUID) -> McpCheckResult:
    server = await _load_visible(db, user, server_id)
    return await _check_and_sync(db, server, force=True)


async def refresh_tools(db: AsyncSession, user: User, server_id: uuid.UUID) -> McpCheckResult:
    server = await _load_visible(db, user, server_id)
    server.tools_synced_at = None
    await db.commit()
    return await _check_and_sync(db, server, force=True)


async def list_tools(db: AsyncSession, user: User, server_id: uuid.UUID) -> list[McpToolOut]:
    server = await _load_visible(db, user, server_id)
    rows = list(
        (
            await db.execute(
                select(McpTool)
                .where(McpTool.server_id == server.id)
                .order_by(McpTool.name.asc())
            )
        ).scalars()
    )
    return [await _tool_out(db, row, user) for row in rows]


async def patch_tool(
    db: AsyncSession, user: User, tool_id: uuid.UUID, payload: McpToolPatchRequest
) -> McpToolOut:
    _require_enabled()
    tool = await db.get(McpTool, tool_id)
    if tool is None:
        raise NotFoundError("MCP 工具不存在")
    server = await db.get(McpServer, tool.server_id)
    if server is None:
        raise NotFoundError("MCP 服务器不存在")
    if server.scope == "user" and server.owner_user_id != user.id:
        raise NotFoundError("MCP 工具不存在")
    if server.scope == "system" and user.role != "admin":
        raise ForbiddenError("系统 MCP 工具策略仅管理员可修改")
    if payload.requires_approval is False and tool.risk == "destructive":
        raise ValidationFailedError(
            "破坏性工具必须保留审批", code="approval_required_locked"
        )
    if payload.enabled is not None:
        tool.enabled = payload.enabled
    if payload.requires_approval is not None:
        if payload.requires_approval is False and tool.risk != "read" and tool.admin_only:
            raise ForbiddenError("系统写工具不能取消审批")
        tool.requires_approval = payload.requires_approval
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="mcp.tool.update",
        resource_type="mcp_tool",
        resource_id=str(tool_id),
        detail={"enabled": payload.enabled, "requires_approval": payload.requires_approval},
    )
    await db.commit()
    await db.refresh(tool)
    return await _tool_out(db, tool, user)


# --------------------------------------------------------------------------- 聊天工具装配


@dataclass
class ChatMcpTool:
    tool_id: uuid.UUID
    server_id: uuid.UUID
    slug: str
    exposed_name: str
    description: str
    input_schema: dict[str, Any]
    risk: str
    requires_approval: bool


async def _visible_servers(db: AsyncSession, user: User) -> list[McpServer]:
    return list(
        (
            await db.execute(
                select(McpServer).where(
                    (McpServer.scope == "system") | (McpServer.owner_user_id == user.id)
                )
            )
        ).scalars()
    )


async def collect_chat_tools(
    db: AsyncSession,
    user: User,
    *,
    explicit_servers: list[uuid.UUID],
    explicit_tools: list[uuid.UUID],
) -> tuple[list[ChatMcpTool], list[dict[str, str]]]:
    """按「自动注入 + 显式选择 + 角色过滤」生成本轮 MCP 工具目录。

    返回 (工具列表, 降级 notice 列表)。资源不可见抛 404，已停用抛 422。
    """
    if not get_settings().mcp_enabled:
        return [], []
    servers = await _visible_servers(db, user)
    by_id = {server.id: server for server in servers}
    notices: list[dict[str, str]] = []

    for server_id in explicit_servers:
        server = by_id.get(server_id)
        if server is None:
            raise NotFoundError("所选 MCP 服务器不存在")
        if not await _effective_enabled(db, server, user):
            raise ValidationFailedError(
                f"MCP 服务器「{server.name}」已停用", code="capability_disabled"
            )
        if server.health_status == "unhealthy":
            notices.append(
                {
                    "level": "warning",
                    "code": "mcp_unavailable",
                    "message": f"MCP 服务器「{server.name}」不可用，本轮其工具可能无法调用",
                }
            )
    explicit_tool_ids = [str(item) for item in explicit_tools]
    for tool_id in explicit_tools:
        tool = await db.get(McpTool, tool_id)
        if tool is None or tool.server_id not in by_id:
            raise NotFoundError("所选 MCP 工具不存在")
        if not tool.enabled:
            raise ValidationFailedError("所选 MCP 工具已停用", code="capability_disabled")

    selected_server_ids: set[uuid.UUID] = set(explicit_servers)
    for tool_id in explicit_tools:
        tool = await db.get(McpTool, tool_id)
        if tool is not None:
            selected_server_ids.add(tool.server_id)
    settings = get_settings()
    for server in servers:
        if server.id in selected_server_ids:
            continue
        if not server.auto_inject or not await _effective_enabled(db, server, user):
            continue
        if server.health_status == "unhealthy":
            notices.append(
                {
                    "level": "warning",
                    "code": "mcp_unavailable",
                    "message": f"MCP 服务器「{server.name}」不可用，本轮未注入其工具",
                }
            )
            continue
        selected_server_ids.add(server.id)

    tools: list[ChatMcpTool] = []
    explicit_only_tool_ids: set[uuid.UUID] = set()
    for server_id in selected_server_ids:
        server = by_id.get(server_id)
        if server is None:
            continue
        rows = list(
            (
                await db.execute(
                    select(McpTool)
                    .where(McpTool.server_id == server.id, McpTool.enabled.is_(True))
                    .order_by(McpTool.name.asc())
                )
            ).scalars()
        )
        for row in rows:
            if server.scope == "system" and row.admin_only and user.role != "admin":
                if str(row.id) in explicit_tool_ids:
                    raise ForbiddenError(
                        f"系统 MCP 工具「{row.exposed_name}」仅管理员可用"
                    )
                continue
            if str(row.id) in explicit_tool_ids:
                explicit_only_tool_ids.add(row.id)
            tools.append(
                ChatMcpTool(
                    tool_id=row.id,
                    server_id=server.id,
                    slug=server.slug,
                    exposed_name=row.exposed_name,
                    description=f"[{server.name}] {row.description}".strip()[:4000],
                    input_schema=row.input_schema or {},
                    risk=row.risk,
                    requires_approval=row.requires_approval,
                )
            )
    # 自动注入总量上限：超出时先裁掉未被显式选择的服务器工具。
    if len(tools) > settings.mcp_max_auto_tools:
        keep_explicit = [
            tool for tool in tools if tool.tool_id in explicit_only_tool_ids
        ]
        auto = [tool for tool in tools if tool.tool_id not in explicit_only_tool_ids]
        if len(keep_explicit) > settings.mcp_max_auto_tools:
            raise ValidationFailedError("本轮显式选择的 MCP 工具超过上限")
        tools = keep_explicit + auto[: settings.mcp_max_auto_tools - len(keep_explicit)]
        notices.append(
            {
                "level": "info",
                "code": "mcp_tools_trimmed",
                "message": "MCP 工具过多，部分自动注入工具本轮未加载",
            }
        )
    return tools, notices
