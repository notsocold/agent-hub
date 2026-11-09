"""FastAPI 公共依赖：数据库会话、当前用户、管理员门禁。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.db.models.user import User
from app.db.session import get_db

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


async def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise ForbiddenError("需要管理员权限")
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
