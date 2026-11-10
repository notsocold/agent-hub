"""HITL 迁移到 interrupt()/Command(resume) 的集成测试。"""

from __future__ import annotations

import json
import uuid

import httpx
from httpx import MockTransport
from sqlalchemy import select

from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.models.conversation import Message
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.chat import service as chat_service
from app.modules.chat.schemas import ChatStreamRequest
from app.modules.chat.service import start_chat
from app.modules.mcp.service import register_test_transport
from tests.conftest import auth_headers, collect_run_events_live, register_user
from tests.helpers import MOCK_TOOL_WRITE, make_mcp_transport, make_scripted_model

MOCK_URL = "https://mcp.example.com/mcp"


async def _setup(
    client: httpx.AsyncClient,
    monkeypatch,
    *,
    first_text: str = "",
    tool_calls: list[dict] | None = None,
) -> tuple[dict, dict, dict]:
    """带写工具（需审批）的 MCP 服务器；脚本模型第一步调用写工具。"""
    calls: dict[str, int] = {"tools_call": 0}
    transport = make_mcp_transport([MOCK_TOOL_WRITE])
    original_handle = transport.handle_async_request

    async def counting_handle(request):
        try:
            payload = json.loads(request.content)
        except (ValueError, TypeError):
            payload = {}
        if payload.get("method") == "tools/call":
            calls["tools_call"] += 1
        return await original_handle(request)

    register_test_transport(MOCK_URL, MockTransport(counting_handle))
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "测试", "slug": "test-mcp", "transport": "streamable_http", "url": MOCK_URL},
        headers=headers,
    )
    assert created.status_code == 201
    calls_payload = tool_calls or [
        {"name": "mcp__test-mcp__create_issue", "args": {"title": "x"}}
    ]
    step: dict = {"tool_calls": calls_payload}
    if first_text:
        step["text"] = first_text
    model = make_scripted_model([step, {"text": "收尾"}])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    return registered, headers, calls


async def _start_run(registered: dict, message: str):
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(User.id == uuid.UUID(registered["user"]["id"]))
        )
        assert user is not None
        return await start_chat(session, user, ChatStreamRequest(message=message))


async def _decide(client, headers, approval_id: str, decision: str) -> httpx.Response:
    return await client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": decision},
        headers=headers,
    )


async def _stream_frames(run_id: str):
    """逐帧读取运行事件（用于需要中途交互的用例）。"""
    from app.modules.chat.service import stream_run_events

    async for frame in stream_run_events(uuid.UUID(run_id)):
        event_name: str | None = None
        for line in frame.strip().split("\n"):
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event_name:
                yield event_name, json.loads(line.split(":", 1)[1].strip())


async def _wait_terminal(
    run_id: str, *, on_event=None, timeout: float = 30.0
) -> list[tuple[str, dict]]:
    return await collect_run_events_live(run_id, on_event=on_event, timeout=timeout)


async def test_interrupt_then_approve_resumes_and_executes_once(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, headers, calls = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")
    approvals: list[str] = []

    async def on_event(name: str, data: dict) -> None:
        if name == "run.interrupted":
            assert data["status"] == "interrupted"
            approvals.extend(data["approvals"])
            response = await _decide(client, headers, data["approvals"][0], "approve")
            assert response.status_code == 200
            assert response.json()["resumed"] is True

    events = await _wait_terminal(str(result.run_id), on_event=on_event)
    names = [name for name, _ in events]
    assert "run.interrupted" in names
    assert names[-1] == "message.done"
    assert events[-1][1]["finish_reason"] == "stop"
    assert approvals
    # 工具只执行一次（审批通过后），且整轮成功
    assert calls["tools_call"] >= 1
    async with get_session_factory()() as session:
        run = await session.get(Run, result.run_id)
        assert run is not None and run.status == "succeeded"
        approval = await session.scalar(select(Approval).where(Approval.run_id == result.run_id))
        assert approval is not None and approval.status == "approved"
        assert approval.tool_call_id


async def test_interrupt_then_reject_does_not_execute(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, headers, calls = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")
    calls_before = calls["tools_call"]

    async def on_event(name: str, data: dict) -> None:
        if name == "run.interrupted":
            response = await _decide(client, headers, data["approvals"][0], "reject")
            assert response.status_code == 200

    events = await _wait_terminal(str(result.run_id), on_event=on_event)
    assert events[-1][0] == "message.done"
    assert events[-1][1]["finish_reason"] == "stop"
    rejected = [data for name, data in events if name == "tool.result" and data["status"] == "rejected"]
    assert rejected, "拒绝后应产生 rejected 的 tool.result"
    assert calls["tools_call"] == calls_before, "拒绝后不应真正调用 MCP 工具"


async def test_decision_before_suspend_still_completes(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, headers, _ = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")
    decided = False

    async def on_event(name: str, data: dict) -> None:
        nonlocal decided
        if name == "approval.required" and not decided:
            decided = True
            response = await _decide(client, headers, data["approval_id"], "approve")
            assert response.status_code == 200

    events = await _wait_terminal(str(result.run_id), on_event=on_event)
    assert events[-1][0] == "message.done"
    assert events[-1][1]["finish_reason"] == "stop"
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(Approval).where(Approval.run_id == result.run_id)
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].status == "approved"


async def test_interrupted_run_persists_partial_content(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, _headers, _ = await _setup(client, monkeypatch, first_text="先思考一下")
    result = await _start_run(registered, "创建工单")
    interrupted: dict = {}

    async def on_event(name: str, data: dict) -> None:
        if name == "run.interrupted":
            interrupted.update(data)

    events = await collect_run_events_live(
        str(result.run_id), stop_on={"run.interrupted", "error"}, on_event=on_event
    )
    assert events[-1][0] == "run.interrupted"
    assert interrupted["approvals"]
    async with get_session_factory()() as session:
        run = await session.get(Run, result.run_id)
        assert run is not None and run.status == "interrupted"
        message = await session.get(Message, run.assistant_message_id)
        assert message is not None
        assert message.status == "streaming"
        assert "先思考" in message.content
    # 清理挂起状态，避免影响后续用例
    await chat_service.halt_run(result.run_id)


async def _wait_interrupted(run_id: str) -> tuple[list[tuple[str, dict]], dict]:
    interrupted: dict = {}

    async def on_event(name: str, data: dict) -> None:
        if name == "run.interrupted":
            interrupted.update(data)

    events = await collect_run_events_live(
        run_id, stop_on={"run.interrupted", "error"}, on_event=on_event
    )
    assert events[-1][0] == "run.interrupted", events[-1]
    return events, interrupted


async def test_two_approvals_decided_one_by_one(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, headers, calls = await _setup(
        client,
        monkeypatch,
        tool_calls=[
            {"name": "mcp__test-mcp__create_issue", "args": {"title": "a"}},
            {"name": "mcp__test-mcp__create_issue", "args": {"title": "b"}},
        ],
    )
    result = await _start_run(registered, "批量创建工单")
    events: list[tuple[str, dict]] = []
    approval_ids: list[str] = []
    responses: list[dict] = []

    async def on_event(name: str, data: dict) -> None:
        if name == "run.interrupted":
            approval_ids.extend(data["approvals"])
            assert len(data["approvals"]) == 2
            first = await _decide(client, headers, data["approvals"][0], "approve")
            responses.append(first.json())
            assert first.json()["resumed"] is False
            assert "其他审批" in (first.json()["resume_error"] or "")
            second = await _decide(client, headers, data["approvals"][1], "approve")
            responses.append(second.json())
            assert second.json()["resumed"] is True

    async for frame in _stream_frames(str(result.run_id)):
        name, data = frame
        events.append((name, data))
        await on_event(name, data)
        if name in {"message.done", "error"}:
            break
    assert events[-1][0] == "message.done"
    assert len(approval_ids) == 2
    assert calls["tools_call"] == 2
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(select(Approval).where(Approval.run_id == result.run_id))
            ).scalars()
        )
        assert len(rows) == 2
        assert all(row.status == "approved" for row in rows)


async def test_approval_timeout_cancels_run_and_chat_continues(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    from app.modules.approvals.service import expire_pending

    registered, headers, _ = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")
    _, interrupted = await _wait_interrupted(str(result.run_id))
    approval_id = interrupted["approvals"][0]

    # 把审批改为已过期，再触发巡检（避免等待真实 30 分钟）
    async with get_session_factory()() as session:
        from datetime import UTC, datetime, timedelta

        approval = await session.get(Approval, uuid.UUID(approval_id))
        assert approval is not None
        approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    canceled = await expire_pending()
    assert str(result.run_id) in {str(item) for item in canceled}
    async with get_session_factory()() as session:
        run = await session.get(Run, result.run_id)
        assert run is not None and run.status == "canceled"
        approval = await session.get(Approval, uuid.UUID(approval_id))
        assert approval is not None and approval.status == "expired"

    replay = await collect_run_events_live(str(result.run_id), timeout=10)
    assert replay[-1][0] == "message.done"
    assert replay[-1][1]["finish_reason"] == "canceled"
    assert replay[-1][1]["reason"] == "approval_timeout"

    # 会话解锁：同一会话可以继续发消息
    conversation_id = interrupted.get("conversation_id") or replay[0][1].get("conversation_id")
    assert conversation_id
    response = await client.post(
        "/api/v1/chat/stream",
        json={"conversation_id": conversation_id, "message": "继续聊"},
        headers=headers,
    )
    assert response.status_code == 200


async def test_cancel_interrupted_run_then_chat_again(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, headers, _ = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")
    _, interrupted = await _wait_interrupted(str(result.run_id))

    canceled = await client.post(f"/api/v1/runs/{result.run_id}/cancel", headers=headers)
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"

    async with get_session_factory()() as session:
        run = await session.get(Run, result.run_id)
        assert run is not None and run.status == "canceled"
        approval = await session.get(Approval, uuid.UUID(interrupted["approvals"][0]))
        assert approval is not None and approval.status == "canceled"

    conversation_id = interrupted["conversation_id"]
    response = await client.post(
        "/api/v1/chat/stream",
        json={"conversation_id": conversation_id, "message": "继续聊"},
        headers=headers,
    )
    assert response.status_code == 200


async def test_decision_after_cancel_conflicts(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    registered, headers, _ = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")
    _, interrupted = await _wait_interrupted(str(result.run_id))
    canceled = await client.post(f"/api/v1/runs/{result.run_id}/cancel", headers=headers)
    assert canceled.status_code == 200

    decision = await _decide(client, headers, interrupted["approvals"][0], "approve")
    assert decision.status_code == 409
    assert decision.json()["error"]["code"] in {
        "approval_already_decided",
        "approval_expired",
    }
