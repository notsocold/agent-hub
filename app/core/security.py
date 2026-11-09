"""密码哈希与 JWT 令牌。

令牌只放身份与类型（access/refresh），权限每次从数据库读取；
refresh token 通过 ``jti`` 落库，支持轮换与吊销。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

_password_hash = PasswordHash.recommended()

TokenType = Literal["access", "refresh"]


@dataclass(frozen=True)
class TokenPayload:
    user_id: uuid.UUID
    jti: str
    token_type: TokenType
    expires_at: datetime


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # 损坏的哈希按验证失败处理，避免 500
        return False


def _encode(user_id: uuid.UUID, token_type: TokenType, ttl: timedelta) -> IssuedToken:
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "jti": jti,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.resolved_jwt_secret, algorithm=settings.jwt_algorithm)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def create_access_token(user_id: uuid.UUID) -> IssuedToken:
    settings = get_settings()
    return _encode(user_id, "access", timedelta(minutes=settings.access_token_ttl_minutes))


def create_refresh_token(user_id: uuid.UUID) -> IssuedToken:
    settings = get_settings()
    return _encode(user_id, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> TokenPayload:
    """解码并校验令牌；任何失败统一转 401，避免泄露具体原因。"""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.resolved_jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "jti", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("登录状态已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("登录状态无效，请重新登录") from exc

    if payload.get("type") != expected_type:
        raise UnauthorizedError("令牌类型不正确")
    try:
        user_id = uuid.UUID(str(payload["sub"]))
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (ValueError, KeyError, TypeError) as exc:
        raise UnauthorizedError("登录状态无效，请重新登录") from exc
    return TokenPayload(
        user_id=user_id,
        jti=str(payload["jti"]),
        token_type=expected_type,
        expires_at=expires_at,
    )
