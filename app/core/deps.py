"""FastAPI 公共依赖：数据库会话、当前用户、管理员门禁。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.db.models.user import User
from app.db.session import get_db, get_session_factory

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Bearer access token")

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: DbSession,
) -> User:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("请先登录")
    payload = decode_token(credentials.credentials, "access")
    user = await db.get(User, payload.user_id)
    if user is None or user.status != "active":
        raise UnauthorizedError("账号不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def _record_rbac_denied(user: User, request: Request) -> None:
    """用独立会话落管理员门禁拒绝审计；审计失败不能吞掉 403 响应。"""
    path = request.url.path
    try:
        async with get_session_factory()() as session:
            await write_audit(
                session,
                actor_type="user",
                actor_id=str(user.id),
                action="security.rbac_denied",
                resource_type="endpoint",
                resource_id=path[:64],
                detail={
                    "method": request.method,
                    "path": path[:500],
                    "required_role": "admin",
                },
                commit=True,
            )
    except Exception:
        # 审计失败只记录日志：权限结果必须稳定返回，不能因写日志故障变成 500。
        logger.warning(
            "RBAC 拒绝审计写入失败 path=%s actor_id=%s",
            path[:500],
            user.id,
            exc_info=True,
        )


async def require_admin(user: CurrentUser, request: Request) -> User:
    if user.role != "admin":
        await _record_rbac_denied(user, request)
        raise ForbiddenError("需要管理员权限", details={"required_role": "admin"})
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def client_ip(request: Request) -> str:
    """取客户端 IP；仅信任反向代理注入的 X-Forwarded-For 第一段。"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def client_user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:255]
