"""数据生命周期清理：checkpoint / run_events / audit_logs / 过期附件 / refresh token。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import DateTime, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.metrics import record_cleanup, record_cleanup_run
from app.db.models.attachment import Attachment
from app.db.models.audit import AuditLog
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation, Message
from app.db.models.user import RefreshToken
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

CLEANUP_KINDS = ("checkpoints", "run_events", "audit", "attachments", "refresh_tokens")
_ACTIVE_RUN_STATUSES = ("queued", "running", "interrupted")


@dataclass(frozen=True)
class CleanupResult:
    kind: str
    deleted: int
    status: str
    detail: str = ""


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(0, days))


async def _delete_in_batches(
    db: AsyncSession,
    statement,
    *,
    batch_size: int,
) -> int:
    """按主键子查询分批删除；返回总删除数。"""
    total = 0
    while True:
        result = await db.execute(statement(batch_size))
        await db.commit()
        deleted = int(result.rowcount or 0)
        total += deleted
        if deleted < batch_size:
            return total


async def cleanup_audit_logs(*, dry_run: bool = False) -> CleanupResult:
    settings = get_settings()
    cutoff = _cutoff(settings.audit_retention_days)
    batch = settings.cleanup_batch_size
    async with get_session_factory()() as db:
        if dry_run:
            count = await db.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.created_at < cutoff)
            )
            return CleanupResult("audit", int(count or 0), "skipped" if not count else "ok")
        deleted = await _delete_in_batches(
            db,
            lambda size: delete(AuditLog).where(
                AuditLog.id.in_(
                    select(AuditLog.id)
                    .where(AuditLog.created_at < cutoff)
                    .order_by(AuditLog.id.asc())
                    .limit(size)
                )
            ),
            batch_size=batch,
        )
    return CleanupResult("audit", deleted, "ok")


async def cleanup_run_events(*, dry_run: bool = False) -> CleanupResult:
    settings = get_settings()
    cutoff = _cutoff(settings.run_events_retention_days)
    batch = settings.cleanup_batch_size
    condition = (
        Run.finished_at.is_not(None),
        Run.finished_at < cutoff,
        Run.status.in_(("succeeded", "failed", "canceled")),
    )
    async with get_session_factory()() as db:
        if dry_run:
            count = await db.scalar(
                select(func.count(RunEvent.id)).join(Run, Run.id == RunEvent.run_id).where(*condition)
            )
            return CleanupResult("run_events", int(count or 0), "skipped" if not count else "ok")
        deleted = await _delete_in_batches(
            db,
            lambda size: delete(RunEvent).where(
                RunEvent.id.in_(
                    select(RunEvent.id)
                    .join(Run, Run.id == RunEvent.run_id)
                    .where(*condition)
                    .order_by(RunEvent.id.asc())
                    .limit(size)
                )
            ),
            batch_size=batch,
        )
    return CleanupResult("run_events", deleted, "ok")


async def _delete_checkpoint_threads(conversation_ids: list[uuid.UUID]) -> None:
    settings = get_settings()
    async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_dsn) as saver:
        for conversation_id in conversation_ids:
            await saver.adelete_thread(str(conversation_id))


async def cleanup_checkpoints(*, dry_run: bool = False) -> CleanupResult:
    settings = get_settings()
    if settings.checkpoint_backend != "postgres":
        return CleanupResult("checkpoints", 0, "skipped", "checkpoint 后端为 memory")
    cutoff = _cutoff(settings.checkpoint_retention_days)
    marker = Conversation.metadata_["checkpoint_cleaned_at"].astext
    active_run = (
        select(Run.id)
        .where(
            Run.conversation_id == Conversation.id,
            Run.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .exists()
    )
    statement = (
        select(Conversation)
        .where(
            Conversation.updated_at < cutoff,
            ~active_run,
            or_(
                marker.is_(None),
                Conversation.updated_at > cast(marker, DateTime(timezone=True)),
            ),
        )
        .order_by(Conversation.updated_at.asc())
        .limit(settings.cleanup_batch_size)
    )
    async with get_session_factory()() as db:
        rows = list((await db.execute(statement)).scalars())
        if dry_run or not rows:
            return CleanupResult(
                "checkpoints", len(rows), "skipped" if not rows else "ok"
            )
        await _delete_checkpoint_threads([row.id for row in rows])
        for row in rows:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == row.id)
                .values(
                    metadata_={
                        **(row.metadata_ or {}),
                        "checkpoint_cleaned_at": (row.updated_at or cutoff).isoformat(),
                    },
                    # 显式保留原 updated_at：标记本身不应让会话“重新变新”。
                    updated_at=Conversation.updated_at,
                )
            )
        await db.commit()
    return CleanupResult("checkpoints", len(rows), "ok")


def _unlink_attachment(path: Path, root: Path) -> None:
    """删除附件文件；路径不在附件目录内则拒绝（防任意文件删除）。"""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("附件路径越界")
    resolved.unlink(missing_ok=True)


async def cleanup_attachments(*, dry_run: bool = False) -> CleanupResult:
    settings = get_settings()
    now = datetime.now(UTC)
    batch = settings.cleanup_batch_size
    root = settings.attachment_dir.resolve()
    async with get_session_factory()() as db:
        rows = list(
            (
                await db.execute(
                    select(Attachment)
                    .where(Attachment.expires_at.is_not(None), Attachment.expires_at < now)
                    .order_by(Attachment.expires_at.asc())
                    .limit(batch)
                )
            ).scalars()
        )
        if dry_run:
            return CleanupResult(
                "attachments", len(rows), "skipped" if not rows else "ok"
            )
        deleted = 0
        for row in rows:
            path: Path | None = Path(row.storage_path) if row.storage_path else None
            if path is not None:
                try:
                    _unlink_attachment(path, root)
                except (OSError, ValueError):
                    logger.warning("附件文件删除失败 attachment_id=%s", row.id, exc_info=True)
                    continue
            referenced = bool(
                await db.scalar(
                    select(func.count(Message.id)).where(
                        Message.attachments.contains([str(row.id)])
                    )
                )
            )
            if referenced:
                row.status = "deleted"
                row.storage_path = ""
            else:
                await db.delete(row)
            deleted += 1
        await db.commit()
    return CleanupResult("attachments", deleted, "ok")


async def cleanup_refresh_tokens(*, dry_run: bool = False) -> CleanupResult:
    settings = get_settings()
    now = datetime.now(UTC)
    expired_before = now - timedelta(days=settings.refresh_token_retention_days)
    batch = settings.cleanup_batch_size
    condition = or_(
        RefreshToken.expires_at < expired_before,
        RefreshToken.revoked_at.is_not(None) & (RefreshToken.revoked_at < expired_before),
    )
    async with get_session_factory()() as db:
        if dry_run:
            count = await db.scalar(select(func.count(RefreshToken.id)).where(condition))
            return CleanupResult(
                "refresh_tokens", int(count or 0), "skipped" if not count else "ok"
            )
        deleted = await _delete_in_batches(
            db,
            lambda size: delete(RefreshToken).where(
                RefreshToken.id.in_(
                    select(RefreshToken.id).where(condition).limit(size)
                )
            ),
            batch_size=batch,
        )
    return CleanupResult("refresh_tokens", deleted, "ok")


async def run_all(
    *, only: set[str] | None = None, dry_run: bool = False
) -> list[CleanupResult]:
    handlers = {
        "checkpoints": cleanup_checkpoints,
        "run_events": cleanup_run_events,
        "audit": cleanup_audit_logs,
        "attachments": cleanup_attachments,
        "refresh_tokens": cleanup_refresh_tokens,
    }
    kinds = [kind for kind in CLEANUP_KINDS if only is None or kind in only]
    results: list[CleanupResult] = []
    for kind in kinds:
        try:
            result = await handlers[kind](dry_run=dry_run)
        except Exception as exc:
            logger.exception("清理失败 kind=%s", kind)
            result = CleanupResult(kind, 0, "error", str(exc)[:300])
        results.append(result)
        record_cleanup(kind, result.deleted)
        record_cleanup_run(result.status)
    deleted_total = sum(result.deleted for result in results)
    if deleted_total:
        async with get_session_factory()() as db:
            await write_audit(
                db,
                actor_type="system",
                actor_id="cleanup",
                action="maintenance.cleanup",
                resource_type="maintenance",
                resource_id="",
                detail={
                    "deleted": {result.kind: result.deleted for result in results},
                    "dry_run": dry_run,
                },
            )
            await db.commit()
    return results


async def cleanup_sweeper(interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(max(1, interval_seconds))
        try:
            await run_all()
        except Exception:
            logger.exception("周期清理任务失败")


__all__ = [
    "CLEANUP_KINDS",
    "CleanupResult",
    "cleanup_attachments",
    "cleanup_audit_logs",
    "cleanup_checkpoints",
    "cleanup_refresh_tokens",
    "cleanup_run_events",
    "cleanup_sweeper",
    "run_all",
]
