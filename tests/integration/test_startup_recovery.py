"""启动恢复：孤儿 run 终态、审批中的 run 不被误伤。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models.approval import Approval
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.maintenance.recovery import recover


async def _seed_run(*, status: str, with_pending: bool = False) -> uuid.UUID:
    async with get_session_factory()() as db:
        user = await db.scalar(select(User).limit(1))
        assert user is not None
        conversation = Conversation(user_id=user.id, title="恢复测试")
        db.add(conversation)
        await db.flush()
        user_message = Message(conversation_id=conversation.id, role="user", content="hi")
        assistant = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="部分输出",
            status="streaming",
        )
        db.add_all([user_message, assistant])
        await db.flush()
        run = Run(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            status=status,
            model="fake-chat",
        )
        db.add(run)
        await db.flush()
        if with_pending:
            db.add(
                Approval(
                    run_id=run.id,
                    tool_name="mcp__x__write",
                    status="pending",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await db.commit()
        return run.id


async def test_orphan_running_run_failed(db, user) -> None:
    run_id = await _seed_run(status="running")
    stats = await recover(startup=True)
    assert stats.failed == 1
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"
        assert run.error_code == "run_interrupted"
        assert run.assistant_message_id is not None
        assistant = await session.get(Message, run.assistant_message_id)
        assert assistant.status == "failed"
        events = list(
            (
                await session.execute(select(RunEvent).where(RunEvent.run_id == run_id))
            ).scalars()
        )
        assert [event.payload["_event"] for event in events] == ["error"]


async def test_interrupted_with_pending_approval_kept(db, user) -> None:
    run_id = await _seed_run(status="interrupted", with_pending=True)
    stats = await recover(startup=True)
    assert stats.skipped >= 1
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        assert run.status == "interrupted"


async def test_recover_is_idempotent(db, user) -> None:
    await _seed_run(status="running")
    first = await recover(startup=True)
    second = await recover(startup=True)
    assert first.failed == 1
    assert second.failed == 0


async def test_interrupted_without_checkpoint_canceled_restart_lost(db, user) -> None:
    run_id = await _seed_run(status="interrupted")
    stats = await recover(startup=True)
    assert stats.resumed == 1
    from app.modules.chat.registry import registry

    assert await registry.drain(10.0) is True
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
    assert run.status == "canceled"


async def test_fail_orphaned_run_does_not_overwrite_succeeded(db, user) -> None:
    from app.modules.chat.service import fail_orphaned_run

    run_id = await _seed_run(status="succeeded")
    assert await fail_orphaned_run(run_id) is False
    async with get_session_factory()() as session:
        assert (await session.get(Run, run_id)).status == "succeeded"
