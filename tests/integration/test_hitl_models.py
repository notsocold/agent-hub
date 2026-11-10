"""审批幂等键：同一 run 的同一 tool_call_id 只能有一条审批。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.db.session import get_session_factory


async def _seed_run(session) -> Run:
    user = User(email=f"u-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", display_name="u")
    session.add(user)
    await session.flush()
    conversation = Conversation(user_id=user.id, title="t")
    session.add(conversation)
    await session.flush()
    user_message = Message(
        conversation_id=conversation.id, role="user", content="hi", status="completed"
    )
    session.add(user_message)
    await session.flush()
    run = Run(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        source="user",
        status="interrupted",
    )
    session.add(run)
    await session.flush()
    return run


async def test_tool_call_id_unique_per_run(db) -> None:
    async with get_session_factory()() as session:
        run = await _seed_run(session)
        common = {
            "run_id": run.id,
            "tool_name": "mcp__x__write",
            "status": "pending",
            "tool_call_id": "call_1",
        }
        session.add(Approval(**common))
        await session.flush()
        session.add(Approval(**common))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_null_tool_call_id_allows_multiple(db) -> None:
    async with get_session_factory()() as session:
        run = await _seed_run(session)
        session.add(Approval(run_id=run.id, tool_name="old", status="pending"))
        session.add(Approval(run_id=run.id, tool_name="old", status="pending"))
        await session.commit()
        assert True
