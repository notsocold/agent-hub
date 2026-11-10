"""平台密钥管理路由（仅管理员）。"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.core.deps import AdminUser, DbSession
from app.modules.secrets import service
from app.modules.secrets.schemas import (
    SecretCreateRequest,
    SecretListOut,
    SecretOut,
    SecretUpdateRequest,
)

router = APIRouter(prefix="/admin/secrets", tags=["平台密钥"])


@router.get(
    "",
    response_model=SecretListOut,
    summary="列出平台密钥",
    description="只返回名称、描述、启用状态与时间；永不返回明文或密文。",
)
async def list_secrets(
    admin: AdminUser,
    db: DbSession,
    enabled: bool | None = Query(default=None),
) -> SecretListOut:
    return SecretListOut(items=await service.list_secrets(db, enabled=enabled))


@router.post(
    "",
    response_model=SecretOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建平台密钥",
    description=(
        "写入时用 `CAPABILITY_SECRET_KEY` 做 AES-GCM 加密；未配置该密钥返回 "
        "503 `secret_store_unavailable`。响应不含明文。"
    ),
)
async def create_secret(
    payload: SecretCreateRequest, admin: AdminUser, db: DbSession
) -> SecretOut:
    return await service.create_secret(db, admin, payload)


@router.patch(
    "/{name}",
    response_model=SecretOut,
    summary="更新平台密钥",
    description="`value` 用于轮换密钥；也可修改描述或启停。",
)
async def update_secret(
    name: str, payload: SecretUpdateRequest, admin: AdminUser, db: DbSession
) -> SecretOut:
    return await service.update_secret_by_name(db, admin, name, payload)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除平台密钥",
    description="删除不检查引用；引用方解析失败时跳过该凭据头。",
)
async def delete_secret(name: str, admin: AdminUser, db: DbSession) -> None:
    await service.delete_secret_by_name(db, admin, name)
