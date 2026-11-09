"""认证业务：注册、登录、刷新轮换、账号资料与密码。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
    ValidationFailedError,
)
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models.user import RefreshToken, User
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UpdateMeRequest,
    UserOut,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _token_response(user: User, access_token: str, refresh_token: str) -> TokenResponse:
    settings = get_settings()
    return TokenResponse(
        user=UserOut.model_validate(user),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


async def _find_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalar_one_or_none()


async def _new_token_pair(
    db: AsyncSession, user: User, *, user_agent: str, ip: str
) -> tuple[str, str]:
    access = create_access_token(user.id)
    refresh = create_refresh_token(user.id)
    db.add(
        RefreshToken(
            user_id=user.id,
            jti=refresh.jti,
            expires_at=refresh.expires_at,
            user_agent=user_agent,
            ip=ip,
        )
    )
    return access.token, refresh.token


async def register(
    db: AsyncSession,
    payload: RegisterRequest,
    *,
    ip: str,
    user_agent: str,
) -> TokenResponse:
    settings = get_settings()
    limiter.check(
        f"register:{ip}",
        limit=settings.rate_limit_register_max,
        window_seconds=settings.rate_limit_register_window_seconds,
    )
    if await _find_user_by_email(db, payload.email):
        raise ConflictError("该邮箱已注册")
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)
    await db.flush()
    access, refresh = await _new_token_pair(db, user, user_agent=user_agent, ip=ip)
    await db.commit()
    await db.refresh(user)
    return _token_response(user, access, refresh)


async def login(
    db: AsyncSession,
    payload: LoginRequest,
    *,
    ip: str,
    user_agent: str,
) -> TokenResponse:
    settings = get_settings()
    limiter.check(
        f"login:{ip}",
        limit=settings.rate_limit_login_max,
        window_seconds=settings.rate_limit_login_window_seconds,
    )
    user = await _find_user_by_email(db, payload.email)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise UnauthorizedError("邮箱或密码错误")
    if user.status != "active":
        raise ForbiddenError("账号已被停用")
    user.last_login_at = _now()
    access, refresh = await _new_token_pair(db, user, user_agent=user_agent, ip=ip)
    await db.commit()
    await db.refresh(user)
    return _token_response(user, access, refresh)


async def refresh(
    db: AsyncSession,
    refresh_token: str,
    *,
    ip: str,
    user_agent: str,
) -> TokenResponse:
    payload = decode_token(refresh_token, "refresh")
    row = await db.scalar(
        select(RefreshToken).where(RefreshToken.jti == payload.jti).with_for_update()
    )
    now = _now()
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= now
        or row.user_id != payload.user_id
    ):
        raise UnauthorizedError("刷新令牌已失效，请重新登录")
    user = await db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise UnauthorizedError("账号不存在或已停用")
    access = create_access_token(user.id)
    new_refresh = create_refresh_token(user.id)
    row.revoked_at = now
    row.replaced_by_jti = new_refresh.jti
    db.add(
        RefreshToken(
            user_id=user.id,
            jti=new_refresh.jti,
            expires_at=new_refresh.expires_at,
            user_agent=user_agent,
            ip=ip,
        )
    )
    await db.commit()
    return _token_response(user, access.token, new_refresh.token)


async def logout(db: AsyncSession, refresh_token: str) -> None:
    try:
        payload = decode_token(refresh_token, "refresh")
    except UnauthorizedError:
        return
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == payload.jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await db.commit()


async def update_me(db: AsyncSession, user: User, payload: UpdateMeRequest) -> User:
    user.display_name = payload.display_name
    await db.commit()
    await db.refresh(user)
    return user


async def change_password(db: AsyncSession, user: User, payload: ChangePasswordRequest) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise BadRequestError("当前密码不正确", code="invalid_password")
    if payload.new_password == payload.current_password:
        raise ValidationFailedError("新密码不能与当前密码相同")
    user.password_hash = hash_password(payload.new_password)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await db.commit()


__all__ = [
    "change_password",
    "login",
    "logout",
    "refresh",
    "register",
    "update_me",
]
