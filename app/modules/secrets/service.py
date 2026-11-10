"""平台密钥服务：加密存储、管理与引用解析。

写接口要求 ``CAPABILITY_SECRET_KEY``；解析失败（密钥缺失/密文损坏/已禁用）返回
``None``，由调用方跳过该凭据头，不向终端用户抛错。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.errors import ConflictError, NotFoundError, ServiceUnavailableError
from app.core.ssrf import decrypt_secret, encrypt_secret
from app.db.models.secret import PlatformSecret
from app.db.models.user import User
from app.modules.secrets.schemas import (
    SecretCreateRequest,
    SecretOut,
    SecretUpdateRequest,
)

logger = logging.getLogger(__name__)


def _to_out(row: PlatformSecret) -> SecretOut:
    return SecretOut(
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _encrypt(value: str) -> str:
    ciphertext = encrypt_secret(value)
    if ciphertext is None:
        raise ServiceUnavailableError(
            "保存平台密钥需要配置 CAPABILITY_SECRET_KEY",
            code="secret_store_unavailable",
        )
    return ciphertext


async def _get_by_name(db: AsyncSession, name: str) -> PlatformSecret:
    row = await db.scalar(select(PlatformSecret).where(PlatformSecret.name == name))
    if row is None:
        raise NotFoundError("密钥不存在", code="secret_not_found")
    return row


async def list_secrets(db: AsyncSession, *, enabled: bool | None = None) -> list[SecretOut]:
    stmt = select(PlatformSecret).order_by(PlatformSecret.name.asc())
    if enabled is not None:
        stmt = stmt.where(PlatformSecret.enabled.is_(enabled))
    rows = list((await db.execute(stmt)).scalars())
    return [_to_out(row) for row in rows]


async def create_secret(
    db: AsyncSession, admin: User, payload: SecretCreateRequest
) -> SecretOut:
    exists = await db.scalar(select(PlatformSecret.id).where(PlatformSecret.name == payload.name))
    if exists is not None:
        raise ConflictError("同名平台密钥已存在", code="secret_exists")
    row = PlatformSecret(
        id=uuid.uuid4(),
        name=payload.name,
        description=payload.description,
        ciphertext=_encrypt(payload.value),
        enabled=True,
        created_by=admin.id,
    )
    db.add(row)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(admin.id),
        action="secret.create",
        resource_type="platform_secret",
        resource_id=payload.name,
        detail={"name": payload.name},
    )
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


async def update_secret_by_name(
    db: AsyncSession, admin: User, name: str, payload: SecretUpdateRequest
) -> SecretOut:
    row = await _get_by_name(db, name)
    changed: list[str] = []
    if payload.value is not None:
        row.ciphertext = _encrypt(payload.value)
        changed.append("value")
    if payload.description is not None:
        row.description = payload.description
        changed.append("description")
    if payload.enabled is not None:
        row.enabled = payload.enabled
        changed.append("enabled")
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(admin.id),
        action="secret.update",
        resource_type="platform_secret",
        resource_id=name,
        detail={"name": name, "changed_fields": changed},
    )
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


async def delete_secret_by_name(db: AsyncSession, admin: User, name: str) -> None:
    row = await _get_by_name(db, name)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(admin.id),
        action="secret.delete",
        resource_type="platform_secret",
        resource_id=name,
        detail={"name": name},
    )
    await db.delete(row)
    await db.commit()


async def resolve_platform_secret(db: AsyncSession, name: str) -> str | None:
    """解析 ``secret:NAME`` 引用；不存在/禁用/无密钥/解密失败返回 None。"""
    row = await db.scalar(select(PlatformSecret).where(PlatformSecret.name == name))
    if row is None or not row.enabled:
        return None
    plaintext = decrypt_secret(row.ciphertext)
    if plaintext is None:
        logger.warning("平台密钥不可解析（密钥缺失或已轮换）name=%s", name)
    return plaintext
