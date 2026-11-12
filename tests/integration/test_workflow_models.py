"""M9 工作流两表模型：scope 约束、运行映射唯一性与默认值。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.chat import Run
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.db.models.workflow import Workflow, WorkflowRun
from app.db.session import get_session_factory


async def _create_user(session) -> User:
    """直接写库建用户，供 user 模板挂 owner。"""
    user = User(
        email=f"u-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", display_name="u"
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_run(session) -> Run:
    """造一条真实 chat run（workflow_runs.run_id 有 FK，映射必须指向存在的 run）。"""
    user = await _create_user(session)
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
        status="running",
    )
    session.add(run)
    await session.flush()
    return run


async def test_system_and_user_scope_roundtrip(db) -> None:
    """system（owner 为空）与 user（带 owner）模板各插一行，均可查回且默认值正确。"""
    async with get_session_factory()() as session:
        owner = await _create_user(session)
        system = Workflow(scope="system", owner_user_id=None, name="系统模板")
        own = Workflow(scope="user", owner_user_id=owner.id, name="我的模板")
        session.add_all([system, own])
        await session.commit()

        rows = (await session.execute(select(Workflow).order_by(Workflow.name))).scalars().all()
        assert {row.scope for row in rows} == {"system", "user"}
        system_row = next(row for row in rows if row.scope == "system")
        user_row = next(row for row in rows if row.scope == "user")
        assert system_row.owner_user_id is None
        assert user_row.owner_user_id == owner.id
        # 关键默认值：steps 空数组、启用、初始版本 1。
        assert system_row.steps == []
        assert system_row.enabled is True
        assert system_row.version == 1


async def test_bogus_scope_rejected_by_check(db) -> None:
    """scope 越界值触发 CHECK 约束。"""
    async with get_session_factory()() as session:
        await _create_user(session)
        session.add(Workflow(scope="bogus", name="坏模板"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_workflow_run_id_unique(db) -> None:
    """同一 run_id 重复映射触发唯一索引 uq_workflow_runs_run。"""
    async with get_session_factory()() as session:
        run = await _seed_run(session)
        workflow = Workflow(scope="user", owner_user_id=None, name="模板")
        session.add(workflow)
        await session.flush()

        session.add(WorkflowRun(workflow_id=workflow.id, run_id=run.id, status="running"))
        await session.flush()
        session.add(WorkflowRun(workflow_id=workflow.id, run_id=run.id, status="succeeded"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_workflow_run_defaults_and_roundtrip(db) -> None:
    """映射行默认 running、可查回；status 越界触发 CHECK。"""
    async with get_session_factory()() as session:
        run = await _seed_run(session)
        workflow = Workflow(scope="system", owner_user_id=None, name="系统模板")
        session.add(workflow)
        await session.flush()
        session.add(WorkflowRun(workflow_id=workflow.id, run_id=run.id))
        await session.commit()

        row = (
            (await session.execute(select(WorkflowRun).where(WorkflowRun.run_id == run.id)))
            .scalars()
            .one()
        )
        assert row.status == "running"

        row.status = "bogus"
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
