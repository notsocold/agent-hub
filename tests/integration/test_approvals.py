"""HITL 审批集成测试：重复决定、取消清理、列表统计与过期。"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.chat import service as chat_service
from app.modules.chat.schemas import ChatStreamRequest
from app.modules.chat.service import start_chat
from app.modules.mcp.service import register_test_transport
from tests.conftest import auth_headers, collect_run_events_live, register_user
from tests.helpers import MOCK_TOOL_WRITE, make_mcp_transport, make_scripted_model

MOCK_URL = "https://mcp.example.com/mcp"


async def _setup(client: httpx.AsyncClient, monkeypatch) -> tuple[dict, dict]:
    """创建带写工具（需审批）的 MCP 服务器，脚本化模型第一步调用该工具。"""
    register_test_transport(MOCK_URL, make_mcp_transport([MOCK_TOOL_WRITE]))
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "测试", "slug": "test-mcp", "transport": "streamable_http", "url": MOCK_URL},
        headers=headers,
    )
    assert created.status_code == 201
    model = make_scripted_model(
        [
            {"tool_calls": [{"name": "mcp__test-mcp__create_issue", "args": {"title": "x"}}]},
            {"text": "收尾"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    return registered, headers


async def _start_run(registered: dict, message: str):
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(User.id == uuid.UUID(registered["user"]["id"]))
        )
        assert user is not None
        return await start_chat(session, user, ChatStreamRequest(message=message))


async def test_approval_duplicate_decision_409(client: httpx.AsyncClient, monkeypatch) -> None:
    registered, headers = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")

    approval_ids: list[str] = []

    async def on_event(name: str, data: dict) -> None:
        if name == "approval.required":
            approval_ids.append(data["approval_id"])
            first = await client.post(
                f"/api/v1/approvals/{data['approval_id']}/decision",
                json={"decision": "reject"},
                headers=headers,
            )
            assert first.status_code == 200
            second = await client.post(
                f"/api/v1/approvals/{data['approval_id']}/decision",
                json={"decision": "approve"},
                headers=headers,
            )
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "approval_already_decided"

    events = await collect_run_events_live(str(result.run_id), on_event=on_event)
    assert approval_ids
    assert events[-1][0] == "message.done"


async def test_cancel_run_cancels_pending_approval(client: httpx.AsyncClient, monkeypatch) -> None:
    registered, headers = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")

    canceled_approval: dict = {}

    async def on_event(name: str, data: dict) -> None:
        if name == "approval.required":
            canceled_approval["id"] = data["approval_id"]
            response = await client.post(
                f"/api/v1/runs/{result.run_id}/cancel", headers=headers
            )
            assert response.status_code == 200

    events = await collect_run_events_live(str(result.run_id), on_event=on_event)
    assert canceled_approval.get("id")
    assert events[-1][0] == "message.done"
    assert events[-1][1]["finish_reason"] == "canceled"

    approval = await client.get(
        f"/api/v1/approvals/{canceled_approval['id']}", headers=headers
    )
    assert approval.json()["status"] == "canceled"


async def test_approval_list_stats_and_filters(client: httpx.AsyncClient, monkeypatch) -> None:
    registered, headers = await _setup(client, monkeypatch)
    result = await _start_run(registered, "创建工单")

    async def on_event(name: str, data: dict) -> None:
        if name == "approval.required":
            response = await client.post(
                f"/api/v1/approvals/{data['approval_id']}/decision",
                json={"decision": "approve"},
                headers=headers,
            )
            assert response.status_code == 200

    await collect_run_events_live(str(result.run_id), on_event=on_event)

    listing = await client.get("/api/v1/approvals?status=all", headers=headers)
    body = listing.json()
    assert body["stats"] == {
        "pending": 0,
        "approved": 1,
        "rejected": 0,
        "expired": 0,
        "canceled": 0,
    }
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["tool_name"] == "mcp__test-mcp__create_issue"
    assert item["risk"] == "unknown"
    assert item["decided_by"]["display_name"]
    assert item["conversation_title"]

    pending = await client.get("/api/v1/approvals", headers=headers)
    assert pending.json()["items"] == []
