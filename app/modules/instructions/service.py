"""指令业务：读取系统默认与用户覆盖，合并出生效指令。"""

from __future__ import annotations

from datetime import UTC, datetime

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


async def _default_instruction(db: AsyncSession) -> tuple[str, datetime | None]:
    row = await db.get(SystemSetting, SYSTEM_KEY_INSTRUCTION)
    if row is None:
        return DEFAULT_INSTRUCTION, None
    value = row.value
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str):
            return content, row.updated_at
    if isinstance(value, str):
        return value, row.updated_at
    return DEFAULT_INSTRUCTION, None


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
