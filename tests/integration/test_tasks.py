"""A2A 任务集成测试：send_message 全场景、SSE 事件、任务 API 与动作。"""

from __future__ import annotations

import uuid

import httpx

from app.modules.a2a.client import register_test_transport
from app.modules.chat import service as chat_service
from tests.conftest import (
    auth_headers,
    collect_run_events_live,
    collect_sse,
    register_user,
)
from tests.fixtures.fake_a2a_agent import FakeA2aAgent
from tests.helpers import make_scripted_model

AGENT_URL = "http://a2a.test"


def _register_fake(**kwargs) -> FakeA2aAgent:
    fake = FakeA2aAgent(url=AGENT_URL, **kwargs)
    register_test_transport(AGENT_URL, httpx.ASGITransport(app=fake.app))
    return fake


async def _create_agent(client: httpx.AsyncClient, headers: dict, **overrides) -> dict:
    payload = {"url": AGENT_URL, **overrides}
    response = await client.post("/api/v1/agents", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _start_chat(
    client: httpx.AsyncClient, registered: dict, monkeypatch, steps: list[dict]
) -> str:
    """注入脚本化模型并启动一轮对话，返回 run_id（后台继续执行）。

    服务层直连启动：ASGITransport 会缓冲整个 SSE 流，实时交互必须走服务层。
    """
    del client
    from sqlalchemy import select

    from app.db.models.user import User
    from app.db.session import get_session_factory
    from app.modules.chat.schemas import ChatStreamRequest
    from app.modules.chat.service import start_chat

    model = make_scripted_model(steps)
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(User.id == uuid.UUID(registered["user"]["id"]))
        )
        assert user is not None
        result = await start_chat(session, user, ChatStreamRequest(message="帮我委派任务"))
    return str(result.run_id)


async def test_send_message_success_flow(client: httpx.AsyncClient, monkeypatch) -> None:
    """成功场景：事件顺序、a2a_tasks 落库、任务列表与详情。"""
    fake = _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "name": "send_message",
                        "args": {"agent": "fake", "task": "核对 9 月退款工单"},
                    }
                ]
            },
            {"text": "远程结果已整合"},
        ],
    )
    assert run_id

    # SSE 事件顺序：tool.start → a2a.task(submitted/working) → a2a.artifact
    # → a2a.task(completed) → tool.result → message.done。
    collected = await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=30)
    names = [name for name, _ in collected]
    assert names.index("tool.start") < names.index("a2a.task")
    assert names.index("a2a.artifact") < names.index("a2a.task", names.index("a2a.artifact"))
    assert names[-1] == "message.done"
    assert names.index("tool.result") < names.index("message.delta")
    run = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
    assert run.status_code == 200
    assert run.json()["status"] == "succeeded"
    # 远程确实收到一次调用。
    assert fake.call_count == 1
    assert "核对 9 月退款工单" in fake.text_of_last_call()
    # a2a.task 序列：submitted → working → completed；末条带 result_summary。
    a2a_events = [data for name, data in collected if name == "a2a.task"]
    states = [item["state"] for item in a2a_events]
    assert states[:3] == ["submitted", "working", "completed"]
    assert a2a_events[-1]["result_summary"]
    assert a2a_events[-1]["agent"] == "fake"
    assert a2a_events[-1]["agent_detail"]["slug"] == "fake"
    tool_result = next(data for name, data in collected if name == "tool.result")
    assert tool_result["status"] == "ok"
    assert tool_result["source"] == "a2a"
    assert tool_result["source_id"]

    # 任务列表：字段齐全、stats、过滤。
    listing = await client.get("/api/v1/tasks", headers=headers)
    assert listing.status_code == 200
    page = listing.json()
    assert page["stats"]["total"] == 1
    assert page["stats"]["completed"] == 1
    item = page["items"][0]
    for key in (
        "id",
        "a2a_task_id",
        "agent",
        "run_id",
        "conversation",
        "state",
        "progress",
        "duration_ms",
        "request_summary",
        "error_summary",
        "available_actions",
        "created_at",
        "updated_at",
    ):
        assert key in item, key
    assert item["state"] == "completed"
    assert item["available_actions"] == []
    assert item["agent"]["slug"] == "fake"
    assert item["request_summary"] == "核对 9 月退款工单"
    assert item["error_summary"] is None

    # 过滤：state / run_id / q / conversation_id。
    by_state = await client.get("/api/v1/tasks", params={"state": "completed"}, headers=headers)
    assert by_state.json()["stats"]["completed"] == 1
    by_run = await client.get("/api/v1/tasks", params={"run_id": run_id}, headers=headers)
    assert by_run.json()["stats"]["total"] == 1
    by_q = await client.get("/api/v1/tasks", params={"q": "退款工单"}, headers=headers)
    assert by_q.json()["stats"]["total"] == 1
    no_hit = await client.get("/api/v1/tasks", params={"q": "不存在"}, headers=headers)
    assert no_hit.json()["stats"]["total"] == 0

    # 详情：request/result/timeline/started_at/finished_at。
    detail = await client.get(f"/api/v1/tasks/{item['id']}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["tool_call_id"]
    assert body["request"]["tool"] == "send_message"
    assert body["request"]["args"]["agent"] == "fake"
    assert body["result"]["artifact"]["type"] == "text"
    assert body["result"]["artifact"]["data"]["checks_passed"] == 12
    assert body["result"]["summary"]
    assert body["error"] is None
    assert body["timeline"][0]["state"] == "submitted"
    assert body["timeline"][-1]["state"] == "completed"
    assert body["started_at"]
    assert body["finished_at"]

    # Agent 详情 recent_tasks 与列表 recent_task。
    agent_detail = await client.get(
        f"/api/v1/agents/{item['agent']['id']}", headers=headers
    )
    assert len(agent_detail.json()["recent_tasks"]) == 1
    agent_list = await client.get("/api/v1/agents", headers=headers)
    assert agent_list.json()["items"][0]["recent_task"]["state"] == "completed"


async def test_send_message_failure_and_timeout(client: httpx.AsyncClient, monkeypatch) -> None:
    """失败：整轮不崩，任务 failed + 可读错误；超时：a2a_timeout。"""
    fake = _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {"name": "send_message", "args": {"agent": "fake", "task": "fail 场景"}}
                ]
            },
            {"text": "失败已提示用户"},
        ],
    )
    collected = await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=30)
    names = [name for name, _ in collected]
    assert "message.done" in names
    done = next(data for name, data in collected if name == "message.done")
    assert done["finish_reason"] == "stop"  # 工具错误不崩整轮
    failed_events = [
        data for name, data in collected
        if name == "a2a.task" and data["state"] == "failed"
    ]
    assert failed_events
    assert failed_events[-1]["error"]["code"] == "a2a_remote_error"

    listing = await client.get("/api/v1/tasks", headers=headers)
    item = listing.json()["items"][0]
    assert item["state"] == "failed"
    assert item["available_actions"] == []
    assert item["error_summary"]
    detail = await client.get(f"/api/v1/tasks/{item['id']}", headers=headers)
    assert detail.json()["error"]["code"] == "a2a_remote_error"
    assert detail.json()["error"]["retryable"] is False

    # 超时：slow 场景永不完成 → 轮询超时 a2a_timeout。
    fake.slow_seconds = 0
    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {"name": "send_message", "args": {"agent": "fake", "task": "slow 场景"}}
                ]
            },
            {"text": "已提示超时"},
        ],
    )
    collected = await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=60)
    failed = [
        data for name, data in collected
        if name == "a2a.task" and data["state"] == "failed"
    ]
    assert failed, [name for name, _ in collected]
    assert failed[-1]["error"]["code"] == "a2a_timeout"
    tool_result = next(data for name, data in collected if name == "tool.result")
    assert tool_result["status"] == "ok"  # 工具错误以 ToolMessage 返回


async def test_input_required_resume_flow(client: httpx.AsyncClient, monkeypatch) -> None:
    """input_required：事件 → resume → 同一 run 继续 → completed。"""
    fake = _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "name": "send_message",
                        "args": {"agent": "fake", "task": "input_required 场景"},
                    }
                ]
            },
            {"text": "补充后已完成"},
        ],
    )

    task_id: str | None = None

    async def on_event(name: str, data: dict) -> None:
        nonlocal task_id
        if name == "a2a.input_required":
            task_id = data["task_id"]
            resume = await client.post(
                f"/api/v1/tasks/{task_id}/resume",
                json={"input": "按创建时间统计"},
                headers=headers,
            )
            assert resume.status_code == 200, resume.text
            body = resume.json()
            assert body["task"]["state"] == "input_required"
            assert body["run"]["id"] == run_id
            assert body["run"]["status"] == "running"

    collected = await collect_run_events_live(
        run_id, on_event=on_event, stop_on={"message.done"}, timeout=30
    )
    names = [name for name, _ in collected]
    assert "a2a.input_required" in names
    assert names[-1] == "message.done"
    assert task_id

    # 同一 a2a_task_id 完成；假 Agent 收到同 contextId 的第二次调用。
    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    body = detail.json()
    assert body["state"] == "completed"
    assert body["timeline"][-1]["state"] == "completed"
    assert any("已补充" in item["note"] for item in body["timeline"])
    assert fake.resume_inputs() == ["按创建时间统计"]

    # 已终态任务 resume → 409 task_not_resumable。
    again = await client.post(
        f"/api/v1/tasks/{task_id}/resume", json={"input": "x"}, headers=headers
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "task_not_resumable"


async def test_auth_required_authorize_and_reject(client: httpx.AsyncClient, monkeypatch) -> None:
    """auth_required：authorize 继续完成；reject 置 rejected。"""
    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "name": "send_message",
                        "args": {"agent": "fake", "task": "auth_required 场景"},
                    }
                ]
            },
            {"text": "授权后已完成"},
        ],
    )
    task_id: str | None = None

    async def on_event(name: str, data: dict) -> None:
        nonlocal task_id
        if name == "a2a.auth_required":
            task_id = data["task_id"]
            authorize = await client.post(
                f"/api/v1/tasks/{task_id}/authorize",
                json={"scopes": ["repo:read"], "note": "已授权"},
                headers=headers,
            )
            assert authorize.status_code == 200, authorize.text

    collected = await collect_run_events_live(
        run_id, on_event=on_event, stop_on={"message.done"}, timeout=30
    )
    assert "a2a.auth_required" in [name for name, _ in collected]
    assert task_id
    detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert detail.json()["state"] == "completed"
    assert any("已授权" in item["note"] for item in detail.json()["timeline"])

    # 终态 authorize → 409。
    again = await client.post(
        f"/api/v1/tasks/{task_id}/authorize", json={}, headers=headers
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "task_not_authorizable"

    # 第二个任务：auth_required → reject。
    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "name": "send_message",
                        "args": {"agent": "fake", "task": "auth_required 场景"},
                    }
                ]
            },
            {"text": "已拒绝"},
        ],
    )
    reject_task_id: str | None = None

    async def on_reject(name: str, data: dict) -> None:
        nonlocal reject_task_id
        if name == "a2a.auth_required":
            reject_task_id = data["task_id"]
            rejected = await client.post(
                f"/api/v1/tasks/{reject_task_id}/reject",
                json={"reason": "不授权"},
                headers=headers,
            )
            assert rejected.status_code == 200
            assert rejected.json()["state"] == "rejected"

    await collect_run_events_live(
        run_id, on_event=on_reject, stop_on={"message.done"}, timeout=30
    )
    assert reject_task_id
    detail = await client.get(f"/api/v1/tasks/{reject_task_id}", headers=headers)
    body = detail.json()
    assert body["state"] == "rejected"
    assert body["available_actions"] == []
    assert "不授权" in body["timeline"][-1]["note"]

    # rejected 再 reject → 409 task_not_rejectable。
    again = await client.post(
        f"/api/v1/tasks/{reject_task_id}/reject", json={}, headers=headers
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "task_not_rejectable"


async def test_cancel_action_and_error_codes(client: httpx.AsyncClient, monkeypatch) -> None:
    """cancel 动作：working 任务尽力远程取消 + 错误码覆盖。"""
    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {"tool_calls": [{"name": "send_message", "args": {"agent": "fake", "task": "success"}}]},
            {"text": "完成"},
        ],
    )
    collected = await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=30)
    assert collected

    # 已 completed 的任务 cancel → 409 task_not_cancelable。
    listing = await client.get("/api/v1/tasks", headers=headers)
    task_id = listing.json()["items"][0]["id"]
    denied = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "task_not_cancelable"

    # 404。
    missing = await client.get(
        f"/api/v1/tasks/{uuid.uuid4()}", headers=headers
    )
    assert missing.status_code == 404


async def test_waiting_expiry_lazy(client: httpx.AsyncClient, monkeypatch) -> None:
    """等待态过期：懒过期置 canceled + timeline 追加。"""
    from datetime import UTC, datetime, timedelta

    from app.db.models.remote import A2aTask
    from app.db.session import get_session_factory

    fake = _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    agent = await _create_agent(client, headers, slug="fake")

    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    async with get_session_factory()() as db:
        db.add(
            A2aTask(
                a2a_task_id="task_expired",
                remote_agent_id=uuid.UUID(agent["id"]),
                state="input_required",
                input_request={
                    "question": "请补充",
                    "input_schema": {},
                    "expires_at": expired,
                },
                request={"tool": "send_message", "args": {}, "summary": "x"},
                timeline=[{"state": "input_required", "at": expired, "duration_ms": None, "note": ""}],
            )
        )
        await db.commit()

    listing = await client.get("/api/v1/tasks", headers=headers)
    item = listing.json()["items"][0]
    assert item["state"] == "canceled"
    detail = await client.get(f"/api/v1/tasks/{item['id']}", headers=headers)
    body = detail.json()
    assert body["timeline"][-1]["state"] == "canceled"
    assert body["timeline"][-1]["note"] == "等待超时"
    # 过期任务不可 resume。
    denied = await client.post(
        f"/api/v1/tasks/{item['id']}/resume", json={"input": "x"}, headers=headers
    )
    assert denied.status_code == 409
    assert fake.call_count == 0


async def test_streaming_agent_uses_sse(client: httpx.AsyncClient, monkeypatch) -> None:
    """streaming 能力：走 message/stream，收到 artifact-update 事件。"""
    fake = _register_fake(streaming=True)
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {
                "tool_calls": [
                    {"name": "send_message", "args": {"agent": "fake", "task": "streaming 场景"}}
                ]
            },
            {"text": "流式完成"},
        ],
    )
    collected = await collect_run_events_live(run_id, stop_on={"message.done"}, timeout=30)
    payloads = [data for name, data in collected if name == "a2a.artifact"]
    assert payloads
    assert "流式结果" in payloads[0]["artifact"]["summary"]
    assert fake.last_call and fake.last_call["method"] == "message/stream"


async def test_no_agent_no_tool_and_system_capabilities(client: httpx.AsyncClient, monkeypatch) -> None:
    """无可选 Agent 时不注入 send_message；系统能力 agents/tasks 恒 true。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    info = await client.get("/api/v1/system/info", headers=headers)
    assert info.json()["capabilities"]["agents"] is True
    assert info.json()["capabilities"]["tasks"] is True

    model = make_scripted_model([{"text": "没有工具可调用"}])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    run_id, events = await collect_sse(
        client, headers=headers, payload={"message": "你好"}
    )
    assert run_id
    # 模型输入不含 send_message（无 Agent 时不注入）。
    system_text = str(model.captured[0][0].content)
    assert "send_message" not in system_text
    names = [name for name, _ in events]
    assert "a2a.task" not in names
    assert names[-1] == "message.done"


async def test_chat_approval_for_requires_approval(client: httpx.AsyncClient, monkeypatch) -> None:
    """Agent 声明 requiresApproval：send_message 走 HITL 审批，拒绝不执行。"""
    _register_fake(requires_approval=True)
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_agent(client, headers, slug="fake")

    run_id = await _start_chat(
        client,
        registered,
        monkeypatch,
        [
            {"tool_calls": [{"name": "send_message", "args": {"agent": "fake", "task": "success"}}]},
            {"text": "结束"},
        ],
    )
    approval_id: str | None = None

    async def on_event(name: str, data: dict) -> None:
        nonlocal approval_id
        if name == "approval.required":
            approval_id = data["approval_id"]
            decision = await client.post(
                f"/api/v1/approvals/{approval_id}/decision",
                json={"decision": "reject", "reason": "不执行"},
                headers=headers,
            )
            assert decision.status_code == 200

    collected = await collect_run_events_live(
        run_id, on_event=on_event, stop_on={"message.done"}, timeout=30
    )
    names = [name for name, _ in collected]
    assert "approval.required" in names
    assert "tool.result" in names
    assert approval_id
    # 审批拒绝：不产生 a2a 调用与任务行。
    listing = await client.get("/api/v1/tasks", headers=headers)
    assert listing.json()["stats"]["total"] == 0
