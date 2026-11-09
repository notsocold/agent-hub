"""附件业务：上传（sha256 去重）、列表、详情、删除与受控下载。"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import (
    CapabilityDisabledError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
)
from app.db.models.attachment import Attachment
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.modules.attachments.schemas import AttachmentOut, AttachmentPage

logger = logging.getLogger(__name__)

# 可以被解码注入模型上下文的文本类型（其余只发快照）。
_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/x-yaml")


def _now() -> datetime:
    return datetime.now(UTC)


def _attachment_out(attachment: Attachment, *, duplicate: bool = False) -> AttachmentOut:
    return AttachmentOut(
        id=attachment.id,
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=attachment.size_bytes,
        checksum=attachment.checksum,
        status=attachment.status,  # type: ignore[arg-type]
        purpose=attachment.purpose,
        conversation_id=attachment.conversation_id,
        created_at=attachment.created_at,
        expires_at=attachment.expires_at,
        duplicate=duplicate,
    )


def _storage_dir(settings, user_id: uuid.UUID) -> Path:
    path = Path(settings.attachment_dir) / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _extract_text(mime_type: str, raw: bytes, max_chars: int) -> str | None:
    if not mime_type.startswith(_TEXT_MIME_PREFIXES):
        return None
    if len(raw) > 4 * 1024 * 1024:
        # 大文本不逐字解码（超长文本可能不是有效 UTF-8，也避免内存放大）。
        return raw.decode("utf-8", errors="replace")[:max_chars]
    return raw.decode("utf-8", errors="replace")[:max_chars]


async def create_attachment(
    db: AsyncSession,
    user: User,
    file: UploadFile,
    *,
    conversation_id: uuid.UUID | None,
    purpose: str,
) -> AttachmentOut:
    settings = get_settings()
    if not settings.attachments_enabled:
        raise CapabilityDisabledError("附件能力未启用")
    filename = (file.filename or "unnamed").strip()
    if not filename or len(filename) > 255:
        raise ValidationFailedError("文件名不合法")
    mime_type = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
    if mime_type not in settings.allowed_mime_type_list:
        raise ValidationFailedError(
            f"不支持的文件类型：{mime_type}", code="attachment_type_not_allowed"
        )
    if conversation_id is not None:
        conversation = await db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
                Conversation.status != "deleted",
            )
        )
        if conversation is None:
            raise NotFoundError("会话不存在")

    raw = await file.read(settings.attachment_max_bytes + 1)
    if len(raw) > settings.attachment_max_bytes:
        raise PayloadTooLargeError(
            f"文件超过 {settings.attachment_max_bytes // (1024 * 1024)}MB 上限"
        )
    checksum = hashlib.sha256(raw).hexdigest()

    # 同用户 + 同 checksum 去重：直接复用已有记录，不重复落盘。
    existing = await db.scalar(
        select(Attachment)
        .where(
            Attachment.user_id == user.id,
            Attachment.checksum == checksum,
            Attachment.status != "deleted",
        )
        .order_by(Attachment.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="attachment.upload_deduplicated",
            resource_type="attachment",
            resource_id=str(existing.id),
            detail={"filename": filename[:200], "size": len(raw)},
        )
        await db.commit()
        return _attachment_out(existing, duplicate=True)

    attachment = Attachment(
        user_id=user.id,
        conversation_id=conversation_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(raw),
        checksum=checksum,
        status="processing",
        purpose=purpose,
        text_excerpt=_extract_text(mime_type, raw, settings.attachment_text_max_chars),
        expires_at=_now() + timedelta(hours=settings.attachment_retention_hours),
    )
    db.add(attachment)
    await db.flush()

    path = _storage_dir(settings, user.id) / f"{attachment.id}-{filename}"
    try:
        await _write_file(path, raw)
    except OSError:
        attachment.status = "failed"
        await db.commit()
        logger.exception("附件写盘失败 attachment_id=%s", attachment.id)
        raise
    attachment.storage_path = str(path)
    attachment.status = "ready"
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="attachment.upload",
        resource_type="attachment",
        resource_id=str(attachment.id),
        detail={"filename": filename[:200], "size": len(raw), "checksum": checksum[:16]},
    )
    await db.commit()
    return _attachment_out(attachment)


async def _write_file(path: Path, raw: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)


async def list_attachments(
    db: AsyncSession,
    user: User,
    *,
    conversation_id: uuid.UUID | None,
    limit: int,
    cursor: str | None,
) -> AttachmentPage:
    stmt = select(Attachment).where(
        Attachment.user_id == user.id, Attachment.status != "deleted"
    )
    if conversation_id is not None:
        stmt = stmt.where(Attachment.conversation_id == conversation_id)
    cursor_data = decode_cursor(cursor)
    if cursor_data:
        raw_time = cursor_data.get("t")
        raw_id = cursor_data.get("id")
        try:
            last_time = datetime.fromisoformat(raw_time) if raw_time else None
            last_id = uuid.UUID(str(raw_id)) if raw_id else None
        except (ValueError, TypeError) as exc:
            raise ValidationFailedError("分页游标无效") from exc
        if last_time is None or last_id is None:
            raise ValidationFailedError("分页游标无效")
        stmt = stmt.where(
            or_(
                Attachment.created_at < last_time,
                and_(Attachment.created_at == last_time, Attachment.id < last_id),
            )
        )
    stmt = stmt.order_by(Attachment.created_at.desc(), Attachment.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor({"t": rows[-1].created_at.isoformat(), "id": rows[-1].id})
    return AttachmentPage(
        items=[_attachment_out(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


async def _load_owned(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> Attachment:
    attachment = await db.get(Attachment, attachment_id)
    if attachment is None or attachment.user_id != user.id or attachment.status == "deleted":
        raise NotFoundError("附件不存在")
    return attachment


async def get_attachment(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> AttachmentOut:
    return _attachment_out(await _load_owned(db, user, attachment_id))


async def delete_attachment(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> None:
    attachment = await _load_owned(db, user, attachment_id)
    referenced = bool(
        await db.scalar(
            select(func.count(Message.id)).where(
                Message.attachments.contains([str(attachment_id)])
            )
        )
    )
    path = Path(attachment.storage_path) if attachment.storage_path else None
    if referenced:
        # 被消息引用的附件走软删：历史消息快照仍可展示元数据，文件从磁盘移除。
        attachment.status = "deleted"
        attachment.storage_path = ""
    else:
        await db.delete(attachment)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="attachment.delete",
        resource_type="attachment",
        resource_id=str(attachment_id),
        detail={"soft_delete": referenced},
    )
    await db.commit()
    if path is not None and path.exists():
        path.unlink(missing_ok=True)


async def open_attachment_file(db: AsyncSession, user: User, attachment_id: uuid.UUID) -> Path:
    """返回可下载的本地文件路径；归属或状态不合法抛 404。"""
    attachment = await _load_owned(db, user, attachment_id)
    if attachment.status != "ready":
        raise NotFoundError("附件不可下载")
    path = Path(attachment.storage_path)
    if not path.exists():
        raise NotFoundError("附件文件已丢失")
    return path
