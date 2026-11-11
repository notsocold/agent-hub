"""启动/周期恢复：让崩溃后的 run 与审批不再永久悬挂。

单实例语义（M6 用户拍板）：
- ``running`` / ``queued`` 只可能来自上一个进程，直接写 ``failed``；
- ``interrupted`` 有未过期 pending 审批时保留，交给审批巡检；
- ``interrupted`` 无 pending 时调度 resume；checkpoint 无挂起由 resume 任务按
  ``restart_lost`` 取消（见 chat.service.resume_after_decision）；
- 周期巡检排除本进程 registry 里的活跃 run，并只处理超过 grace 的记录。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.metrics import record_recovery
from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.session import get_session_factory
from app.modules.chat.registry import registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryStats:
    scanned: int = 0
    resumed: int = 0
    canceled: int = 0
    failed: int = 0
    skipped: int = 0


async def _candidate_ids(*, statuses: tuple[str, ...], cutoff: datetime | None, limit: int) -> list:
    async with get_session_factory()() as db:
        stmt = select(Run.id).where(Run.status.in_(statuses))
        if cutoff is not None:
            stmt = stmt.where(Run.created_at < cutoff)
        stmt = stmt.order_by(Run.created_at.asc()).limit(limit)
        return list((await db.execute(stmt)).scalars())


async def recover(*, startup: bool) -> RecoveryStats:
    settings = get_settings()
    if not settings.startup_recovery_enabled:
        return RecoveryStats()

    scanned = resumed = canceled = failed = skipped = 0
    if startup:
        from app.modules.approvals.service import expire_pending

        expired_runs = await expire_pending()
        canceled += len(expired_runs)

    limit = settings.startup_recovery_max_runs if startup else settings.recovery_batch_size
    cutoff = (
        None
        if startup
        else datetime.now(UTC) - timedelta(seconds=settings.recovery_grace_seconds)
    )

    running_ids = await _candidate_ids(
        statuses=("queued", "running"), cutoff=cutoff, limit=limit
    )
    scanned += len(running_ids)
    for run_id in running_ids:
        if registry.is_running(run_id):
            skipped += 1
            continue
        try:
            from app.modules.chat import service as chat_service

            if await chat_service.fail_orphaned_run(run_id):
                failed += 1
                record_recovery("failed")
            else:
                skipped += 1
        except Exception:
            logger.exception("恢复孤儿 run 失败 run_id=%s", run_id)
            skipped += 1

    interrupted_ids = await _candidate_ids(
        statuses=("interrupted",), cutoff=cutoff, limit=limit
    )
    scanned += len(interrupted_ids)
    for run_id in interrupted_ids:
        if registry.is_running(run_id):
            skipped += 1
            continue
        try:
            async with get_session_factory()() as db:
                pending = await db.scalar(
                    select(func.count(Approval.id)).where(
                        Approval.run_id == run_id, Approval.status == "pending"
                    )
                )
            if pending:
                # 未过期交给审批巡检；过期会在下一轮 expire_pending 取消。
                skipped += 1
                continue
            from app.modules.chat import service as chat_service

            if await chat_service.schedule_resume(run_id, recovery=True):
                resumed += 1
                record_recovery("resumed")
            else:
                skipped += 1
        except Exception:
            logger.exception("恢复 interrupted run 失败 run_id=%s", run_id)
            skipped += 1

    stats = RecoveryStats(
        scanned=scanned,
        resumed=resumed,
        canceled=canceled,
        failed=failed,
        skipped=skipped,
    )
    if scanned:
        logger.info(
            "恢复巡检完成 startup=%s scanned=%s resumed=%s canceled=%s failed=%s skipped=%s",
            startup,
            scanned,
            resumed,
            canceled,
            failed,
            skipped,
        )
    return stats


async def recovery_sweeper(interval_seconds: int) -> None:
    """周期巡检；异常只记日志，不退出循环。"""
    while True:
        await asyncio.sleep(max(1, interval_seconds))
        try:
            await recover(startup=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("周期恢复巡检失败")


__all__ = ["RecoveryStats", "recover", "recovery_sweeper"]
