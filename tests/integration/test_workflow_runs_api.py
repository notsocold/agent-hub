"""M9 运行历史查询与终态同步集成测试：GET runs 分页/权限与 chat 终态回写。

覆盖任务书测试清单：两次 start_chat（成功/失败模型异常）后 GET runs 返回 2 行
且终态与 run 一致、排序 created_at DESC、cursor 翻页不重不漏、系统模板 403
forbidden、他人 user 模板 404、无 runs 空列表、cancel 接口驱动 canceled 同步；
另含 ``sync_workflow_run_status`` 服务层规则直测（终态同名/幂等/非终态不落库/
无对应行 no-op）。

``workflow_runs`` 的插行属于 T5 的 chat 链路：测试用服务层直启 + 手动插行模拟
T5 的映射行（run 已提交后插入 running 行），T4 的同步逻辑负责把行状态追平。
插行必须发生在 run 收尾前——gated 模型把 run 卡在模型调用上，测试先插行再放行。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db.models.chat import Run
from app.db.models.conversation import Conversation
from app.db.models.user import User
from app.db.models.workflow import Workflow, WorkflowRun
from app.db.session import get_session_factory
from tests.conftest import auth_headers, collect_run_events_live, register_user
from tests.helpers import ScriptedToolModel

# 合法单步模板：创建 API 会走装配器校验。
SIMPLE_STEPS: list[dict[str, Any]] = [{"id": "s1", "type": "knowledge_search"}]


def _make_gated_model(gate: threading.Event, *, fail: bool = False) -> ScriptedToolModel:
    """首轮 ``_generate`` 等待 gate 放行：run 停在模型调用上，测试先插行再继续。

    langchain 把同步 ``_generate`` 放进 executor 线程执行，阻塞线程不会卡事件
    循环；``fail=True`` 时放行后抛异常，驱动 ``_finalize_failed`` 链路。
    """

    class _Gated(ScriptedToolModel):
        def _generate(
            self, messages: list[Any], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
        ) -> Any:
            gate.wait(timeout=30)
            if fail:
                raise RuntimeError("模型调用失败（测试注入）")
            return super()._generate(messages, stop, run_manager, **kwargs)

    return _Gated(steps=[{"text": "工作流首轮回复"}])


async def _start_gated_chat(
    monkeypatch: pytest.MonkeyPatch, registered: dict, *, fail: bool = False
) -> tuple[str, threading.Event]:
    """服务层直启一轮对话（模型首轮在 gate 上等待），返回 (run_id, gate)。"""
    from app.modules.chat import service as chat_service
    from app.modules.chat.schemas import ChatStreamRequest
    from app.modules.chat.service import start_chat

    gate = threading.Event()
    model = _make_gated_model(gate, fail=fail)
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(User.id == uuid.UUID(registered["user"]["id"]))
        )
        assert user is not None
        result = await start_chat(session, user, ChatStreamRequest(message="跑一次工作流"))
    return str(result.run_id), gate


async def _insert_workflow_run(workflow_id: uuid.UUID, run_id: uuid.UUID) -> WorkflowRun:
    """模拟 T5 的映射行插入：run 已提交后写一行 ``running`` 的 workflow_runs。"""
    async with get_session_factory()() as session:
        row = WorkflowRun(workflow_id=workflow_id, run_id=run_id, status="running")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _create_workflow(
    client: httpx.AsyncClient, headers: dict[str, str], *, name: str = "运行历史模板"
) -> dict:
    """通过 API 创建本人模板并断言 201，返回响应 JSON。"""
    response = await client.post(
        "/api/v1/workflows", json={"name": name, "steps": SIMPLE_STEPS}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_system_workflow(*, name: str = "系统运行模板") -> Workflow:
    """直接写库插一行系统模板（运行数据属各用户，无 runs 查询入口）。"""
    async with get_session_factory()() as session:
        row = Workflow(
            scope="system",
            owner_user_id=None,
            name=name,
            description="内置系统模板",
            steps=SIMPLE_STEPS,
            enabled=True,
            version=1,
        )
        session.add(row)
        await session.commit()
        return row


async def test_runs_after_success_and_failure_synced(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次 start_chat（成功/失败）后 GET runs：2 行、终态与 run 一致、created_at DESC。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    workflow = await _create_workflow(client, headers, name="运行历史模板")
    workflow_id = uuid.UUID(workflow["id"])

    # 第一次：成功路径。插行放在 run 收尾前（gated 模型暂停），finalize 时同步追平。
    run_id_ok, gate_ok = await _start_gated_chat(monkeypatch, registered)
    await _insert_workflow_run(workflow_id, uuid.UUID(run_id_ok))
    gate_ok.set()
    await collect_run_events_live(run_id_ok, stop_on={"message.done"}, timeout=30)

    # 第二次：失败路径（模型异常 → _finalize_failed → failed 同步）。
    run_id_fail, gate_fail = await _start_gated_chat(monkeypatch, registered, fail=True)
    await _insert_workflow_run(workflow_id, uuid.UUID(run_id_fail))
    gate_fail.set()
    await collect_run_events_live(run_id_fail, stop_on={"error"}, timeout=30)

    listing = await client.get(f"/api/v1/workflows/{workflow['id']}/runs", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert len(body["items"]) == 2
    for item in body["items"]:
        for key in (
            "id",
            "run_id",
            "status",
            "created_at",
            "run_status",
            "run_finished_at",
            "run_duration_ms",
            "run_error_code",
        ):
            assert key in item, key

    # 排序 created_at DESC：后创建的失败 run 在前。
    items = body["items"]
    assert [item["run_id"] for item in items] == [run_id_fail, run_id_ok]

    by_run = {item["run_id"]: item for item in items}
    succeeded = by_run[run_id_ok]
    assert succeeded["status"] == "succeeded"
    assert succeeded["run_status"] == "succeeded"
    assert succeeded["run_finished_at"] is not None
    assert succeeded["run_duration_ms"] is not None
    assert succeeded["run_error_code"] is None

    failed = by_run[run_id_fail]
    assert failed["status"] == "failed"
    assert failed["run_status"] == "failed"
    assert failed["run_finished_at"] is not None
    assert failed["run_error_code"] == "llm_error"


async def test_runs_cursor_pagination_round_trip(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limit=1 逐页翻取：has_more/next_cursor 正确，翻页结果与全量一致、不重不漏。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    workflow = await _create_workflow(client, headers, name="翻页模板")

    run_ids: list[str] = []
    for _ in range(3):
        run_id, gate = await _start_gated_chat(monkeypatch, registered)
        await _insert_workflow_run(uuid.UUID(workflow["id"]), uuid.UUID(run_id))
        gate.set()
        await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=30)
        run_ids.append(run_id)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(
            f"/api/v1/workflows/{workflow['id']}/runs", params=params, headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 1
        seen.extend(item["run_id"] for item in body["items"])
        if not body["has_more"]:
            break
        assert body["next_cursor"]
        cursor = body["next_cursor"]
    else:  # pragma: no cover - 防御死循环
        pytest.fail("翻页未在预期页数内结束")

    assert set(seen) == set(run_ids)
    assert len(seen) == len(run_ids)


async def test_system_workflow_runs_forbidden(client: httpx.AsyncClient) -> None:
    """系统模板的运行数据属各用户：任何人 GET runs 一律 403 forbidden。"""
    system_row = await _insert_system_workflow()
    headers = auth_headers((await register_user(client))["access_token"])

    response = await client.get(f"/api/v1/workflows/{system_row.id}/runs", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"


async def test_foreign_workflow_runs_hidden_as_404(client: httpx.AsyncClient) -> None:
    """他人 user 模板的 runs 一律 404，与不存在同响应（不泄露存在性）。"""
    owner = await register_user(client)
    owner_headers = auth_headers(owner["access_token"])
    foreign = await _create_workflow(client, owner_headers, name="他人模板")

    attacker = await register_user(client)
    attacker_headers = auth_headers(attacker["access_token"])

    foreign_runs = await client.get(
        f"/api/v1/workflows/{foreign['id']}/runs", headers=attacker_headers
    )
    assert foreign_runs.status_code == 404
    missing = await client.get(
        f"/api/v1/workflows/{uuid.uuid4()}/runs", headers=attacker_headers
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert foreign_runs.json()["error"]["code"] == missing.json()["error"]["code"]


async def test_runs_empty_when_no_runs(client: httpx.AsyncClient) -> None:
    """本人模板尚无运行记录：items 空数组、has_more=False。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    workflow = await _create_workflow(client, headers, name="无运行模板")

    response = await client.get(f"/api/v1/workflows/{workflow['id']}/runs", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["next_cursor"] is None


async def test_cancel_syncs_workflow_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel 接口驱动 canceled 同步：run 被 registry.cancel 后 _finalize_canceled 追平。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    workflow = await _create_workflow(client, headers, name="取消同步模板")

    run_id, gate = await _start_gated_chat(monkeypatch, registered)
    await _insert_workflow_run(uuid.UUID(workflow["id"]), uuid.UUID(run_id))

    canceled = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert canceled.status_code == 200, canceled.text
    # message.done（finish_reason=canceled）发出时 _finalize_canceled 已提交。
    await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=30)
    gate.set()  # 放行 executor 线程，尽快释放

    async with get_session_factory()() as session:
        row = await session.scalar(
            select(WorkflowRun).where(WorkflowRun.run_id == uuid.UUID(run_id))
        )
    assert row is not None
    assert row.status == "canceled"


async def test_sync_workflow_run_status_rules(db: Any) -> None:
    """sync 规则：终态同名直写、同值幂等、非终态不落库、无对应行 no-op。"""
    from app.core.security import hash_password
    from app.modules.workflows.service import sync_workflow_run_status

    user = User(
        email=f"sync-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password123"),
        display_name="同步用户",
    )
    db.add(user)
    # UUID 主键由 Python 端 default 生成：逐级 flush 后才能引用外键值。
    await db.flush()
    conversation = Conversation(user_id=user.id, title="同步测试会话")
    db.add(conversation)
    await db.flush()
    run = Run(
        conversation_id=conversation.id, source="user", status="running", model="fake-model"
    )
    db.add(run)
    workflow = Workflow(scope="user", owner_user_id=user.id, name="同步模板", steps=SIMPLE_STEPS)
    db.add(workflow)
    await db.flush()
    row = WorkflowRun(workflow_id=workflow.id, run_id=run.id, status="running")
    db.add(row)
    await db.commit()

    # 终态同名直写。
    await sync_workflow_run_status(db, run.id, "succeeded")
    await db.commit()
    await db.refresh(row)
    assert row.status == "succeeded"

    # 同值重复调用幂等。
    await sync_workflow_run_status(db, run.id, "succeeded")
    await db.commit()
    await db.refresh(row)
    assert row.status == "succeeded"

    # 非终态（如 interrupted）不落库：挂起审批期间映射行保持原值，等终态追平。
    await sync_workflow_run_status(db, run.id, "interrupted")
    await db.commit()
    await db.refresh(row)
    assert row.status == "succeeded"

    # 无对应映射行（非工作流 run）：no-op，不抛异常。
    await sync_workflow_run_status(db, uuid.uuid4(), "failed")
    await db.commit()
