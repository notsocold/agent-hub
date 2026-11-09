"""审计日志写入辅助。

各模块在能力配置变更、记忆增删、技能导入、MCP 连接、审批决定等
动作时调用，统一 actor/action/resource 口径，正文一律不入 detail。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import current_request_id
from app.db.models.audit import AuditLog


async def write_audit(
    db: AsyncSession,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    detail: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """写入一条审计记录；``commit`` 仅用于独立事务场景。"""
    db.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail or {},
            request_id=current_request_id(),
        )
    )
    if commit:
        await db.commit()


def content_fingerprint(text: str) -> str:
    """内容指纹（长度 + sha256 前 16 位），用于审计而不存正文。"""
    return f"{len(text)}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
