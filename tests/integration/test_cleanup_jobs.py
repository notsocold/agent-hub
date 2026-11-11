"""数据清理任务。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation
from app.db.models.user import RefreshToken
from app.db.session import get_session_factory
from app.modules.maintenance import cleanup


async def test_cleanup_audit_and_run_events(db, user) -> None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        session.add_all(
            [
                AuditLog(
                    actor_type="system",
                    actor_id="x",
                    action="old",
                    detail={},
                    created_at=now - timedelta(days=200),
                ),
                AuditLog(
                    actor_type="system",
                    actor_id="x",
                    action="new",
                    detail={},
                    created_at=now,
                ),
            ]
        )
        conversation = Conversation(user_id=user.id, title="c")
        session.add(conversation)
        await session.flush()
        run = Run(
            conversation_id=conversation.id,
            status="succeeded",
            model="fake",
            finished_at=now - timedelta(days=100),
        )
        session.add(run)
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=1, type="run", status="ok", payload={}, ts=now))
        await session.commit()

    audit_result = await cleanup.cleanup_audit_logs()
    events_result = await cleanup.cleanup_run_events()
    assert audit_result.deleted == 1
    assert events_result.deleted == 1
    async with get_session_factory()() as session:
        actions = list((await session.execute(select(AuditLog.action))).scalars())
        assert actions == ["new"]


async def test_checkpoint_cleanup_skips_active_and_marks_cleaned(db, user, monkeypatch) -> None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        conversation = Conversation(
            user_id=user.id, title="old", updated_at=now - timedelta(days=60)
        )
        session.add(conversation)
        await session.flush()
        run = Run(conversation_id=conversation.id, status="running", model="fake")
        session.add(run)
        await session.commit()
        conversation_id = conversation.id

    calls: list[uuid.UUID] = []

    async def fake_delete(ids: list[uuid.UUID]) -> None:
        calls.extend(ids)

    monkeypatch.setattr(cleanup, "_delete_checkpoint_threads", fake_delete)
    monkeypatch.setattr(
        cleanup,
        "get_settings",
        lambda: Settings(
            checkpoint_backend="postgres",
            checkpoint_retention_days=30,
            cleanup_batch_size=10,
            _env_file=None,
        ),
    )
    assert (await cleanup.cleanup_checkpoints()).deleted == 0
    assert calls == []

    async with get_session_factory()() as session:
        run = await session.get(Run, run.id)
        run.status = "succeeded"
        await session.commit()
    assert (await cleanup.cleanup_checkpoints()).deleted == 1
    assert calls == [conversation_id]
    async with get_session_factory()() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation.metadata_["checkpoint_cleaned_at"]
        assert conversation.updated_at is not None


async def test_cleanup_refresh_tokens(db, user) -> None:
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        old = RefreshToken(
            jti="old", expires_at=now - timedelta(days=60), user_id=user.id
        )
        session.add(old)
        await session.commit()
    result = await cleanup.cleanup_refresh_tokens()
    assert result.deleted == 1
