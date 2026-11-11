"""指令业务：读取系统默认与用户覆盖，合并出生效指令。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import content_fingerprint, write_audit
from app.core.config import get_settings
from app.core.errors import CapabilityDisabledError, ValidationFailedError
from app.db.models.system import SystemSetting, UserSetting
from app.db.models.user import User
from app.modules.instructions.schemas import (
    DEFAULT_INSTRUCTION,
    SYSTEM_KEY_INSTRUCTION,
    InstructionOut,
    InstructionUpdateRequest,
    InstructionUpdateResult,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _platform_instruction_content(value: Any) -> str | None:
    """从历史两种形态提取正文：``{"content": "..."}`` 或裸字符串。"""
    if isinstance(value, dict):
        content = value.get("content")
        return content if isinstance(content, str) else None
    if isinstance(value, str):
        return value
    return None


async def get_platform_instruction(
    db: AsyncSession,
) -> tuple[str, SystemSetting | None]:
    """读取平台默认指令；无行或形态非法时回退到内置默认。"""
    row = await db.get(SystemSetting, SYSTEM_KEY_INSTRUCTION)
    if row is None:
        return DEFAULT_INSTRUCTION, None
    content = _platform_instruction_content(row.value)
    if content is None:
        return DEFAULT_INSTRUCTION, None
    return content, row


async def _default_instruction(db: AsyncSession) -> tuple[str, datetime | None]:
    """保留既有内部契约：``(文本, 行更新时间)``，供 ``get_effective`` 使用。"""
    content, row = await get_platform_instruction(db)
    return content, row.updated_at if row is not None else None


async def set_platform_instruction(
    db: AsyncSession, admin: User, content: str | None
) -> SystemSetting | None:
    """保存平台默认指令；``content is None`` 时删除覆盖并恢复内置默认。

    管理端配置面不受 ``instructions_enabled`` 总开关影响（R1），因此这里不调用
    ``_require_enabled``/``get_effective``。``SystemSetting`` 没有 ``onupdate``，
    只改 ``updated_by`` 不会自动刷新时间，必须手动写 ``updated_at``。
    """
    settings = get_settings()
    if content is not None and len(content) > settings.instruction_max_chars:
        raise ValidationFailedError(
            f"指令超过 {settings.instruction_max_chars} 字符上限",
            code="instruction_too_long",
        )

    row = await db.get(SystemSetting, SYSTEM_KEY_INSTRUCTION)
    if content is None:
        if row is None:
            # 没有覆盖行时恢复默认是 no-op：不写审计、不改库。
            return None
        await db.delete(row)
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(admin.id),
            action="instruction.update",
            resource_type="system_setting",
            resource_id=SYSTEM_KEY_INSTRUCTION,
            detail={"scope": "platform", "restored_default": True},
        )
        await db.commit()
        return None

    now = datetime.now(UTC)
    if row is None:
        row = SystemSetting(
            key=SYSTEM_KEY_INSTRUCTION,
            value={"content": content},
            updated_by=admin.id,
            updated_at=now,
        )
        db.add(row)
    else:
        row.value = {"content": content}
        row.updated_by = admin.id
        row.updated_at = now
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(admin.id),
        action="instruction.update",
        resource_type="system_setting",
        resource_id=SYSTEM_KEY_INSTRUCTION,
        detail={
            "scope": "platform",
            "length": len(content),
            "fingerprint": content_fingerprint(content),
        },
    )
    await db.commit()
    await db.refresh(row)
    return row


async def get_effective(db: AsyncSession, user: User) -> InstructionOut:
    settings = get_settings()
    if not settings.instructions_enabled:
        raise CapabilityDisabledError("指令能力未启用")
    user_setting = await db.get(UserSetting, user.id)
    user_value = user_setting.instructions if user_setting else None
    default_value, _ = await _default_instruction(db)
    if user_value is not None:
        effective, source = user_value, "user"
        updated_at = user_setting.updated_at if user_setting else _now()
    else:
        effective, source = default_value, "system_default"
        _, updated_at = await _default_instruction(db)
        updated_at = updated_at or _now()
    return InstructionOut(
        effective=effective,
        source=source,
        user_value=user_value,
        default_value=default_value,
        max_chars=settings.instruction_max_chars,
        updated_at=updated_at,
    )


async def update_instruction(
    db: AsyncSession, user: User, payload: InstructionUpdateRequest
) -> InstructionUpdateResult:
    settings = get_settings()
    if not settings.instructions_enabled:
        raise CapabilityDisabledError("指令能力未启用")
    content = payload.content
    if content is not None and len(content) > settings.instruction_max_chars:
        raise ValidationFailedError(
            f"指令超过 {settings.instruction_max_chars} 字符上限",
            code="instruction_too_long",
        )
    user_setting = await db.get(UserSetting, user.id)
    if user_setting is None:
        user_setting = UserSetting(user_id=user.id, instructions=None)
        db.add(user_setting)
    user_setting.instructions = content
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="instruction.update",
        resource_type="user_setting",
        resource_id=str(user.id),
        detail={
            "length": len(content) if content else 0,
            "fingerprint": content_fingerprint(content) if content else None,
            "restored_default": content is None,
        },
    )
    await db.commit()
    effective = content if content is not None else (await _default_instruction(db))[0]
    source = "user" if content is not None else "system_default"
    return InstructionUpdateResult(
        effective=effective,
        source=source,
        max_chars=settings.instruction_max_chars,
        updated_at=_now(),
    )


async def effective_instruction_text(db: AsyncSession, user: User) -> str:
    """聊天装配用：返回当前生效指令文本（能力关闭时返回空串，不抛错）。"""
    if not get_settings().instructions_enabled:
        return ""
    user_setting = await db.get(UserSetting, user.id)
    if user_setting is not None and user_setting.instructions is not None:
        return user_setting.instructions
    default_value, _ = await _default_instruction(db)
    return default_value
