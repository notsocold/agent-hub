"""聊天流、运行与取消的契约测试。"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.chat.registry import registry
from app.modules.chat.schemas import ChatStreamRequest
from app.modules.chat.service import start_chat
from tests.conftest import auth_headers, collect_sse, register_user


async def test_stream_event_sequence(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    run_id, events = await collect_sse(
        client, headers=headers, payload={"message": "你好，介绍一下你自己"}
    )
    assert run_id
    names = [name for name, _ in events]
    assert names[0] == "conversation.created"
    assert names[1] == "run.started"
    assert names[2] == "message.created"
    assert names[3] == "message.started"
    assert "message.delta" in names
    assert names[-1] == "message.done"

    seqs = [data["seq"] for _, data in events]
    assert seqs == sorted(seqs)
    run_started = events[1][1]
    assert run_started["run_id"] == run_id
    assert run_started["conversation_id"]
    assert run_started["user_message_id"]

    done = events[-1][1]
    assert done["finish_reason"] == "stop"
    assert done["usage"]["total_tokens"] >= done["usage"]["output_tokens"] >= 1
    assert done["first_token_ms"] is not None
    assert done["duration_ms"] >= 0

    run = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "succeeded"
    assert body["message_ids"]["user"]
    assert body["message_ids"]["assistant"]
    assert body["usage"]["total_tokens"] > 0

    events_api = await client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
    assert events_api.status_code == 200
    page = events_api.json()
    assert page["items"]
    assert page["items"][0]["seq"] == 1
    assert page["items"][0]["id"] == f"{run_id}:1"
    assert {item["type"] for item in page["items"]} >= {"request", "message"}

    only_request = await client.get(f"/api/v1/runs/{run_id}/events?types=request", headers=headers)
    assert all(item["type"] == "request" for item in only_request.json()["items"])


async def test_idempotent_replay(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    payload = {"message": "重复发送测试", "client_message_id": "c-001"}

    first_run_id, events = await collect_sse(client, headers=headers, payload=payload)
    conversation_id = events[0][1]["conversation_id"]

    second_run_id, replay_events = await collect_sse(
        client,
        headers=headers,
        payload={**payload, "conversation_id": conversation_id},
    )
    assert second_run_id == first_run_id
    assert replay_events[0][0] == "conversation.created"
    assert replay_events[-1][0] == "message.done"

    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert len(messages.json()["items"]) == 2


async def _start_run(registered: dict, message: str):
    async with get_session_factory()() as session:
        user = await session.get(User, uuid.UUID(registered["user"]["id"]))
        assert user is not None
        return await start_chat(session, user, ChatStreamRequest(message=message))


async def _wait_terminal(client: httpx.AsyncClient, headers: dict[str, str], run_id: str) -> dict:
    for _ in range(100):
        response = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
        body = response.json()
        if body["status"] not in {"queued", "running", "interrupted"}:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("运行未在预期时间内结束")


async def test_concurrent_run_returns_409(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    result = await _start_run(registered, "第一条长消息")

    conflict = await client.post(
        "/api/v1/chat/stream",
        json={"conversation_id": str(result.conversation_id), "message": "第二条消息"},
        headers=headers,
    )
    assert conflict.status_code == 409
    error = conflict.json()["error"]
    assert error["code"] == "run_in_progress"
    assert error["details"]["run_id"] == str(result.run_id)

    canceled = await client.post(f"/api/v1/runs/{result.run_id}/cancel", headers=headers)
    assert canceled.status_code == 200
    await _wait_terminal(client, headers, str(result.run_id))
    for _ in range(100):
        if not registry.is_running(result.run_id):
            break
        await asyncio.sleep(0.02)


async def test_cancel_preserves_partial_content(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    result = await _start_run(registered, "取消测试")
    # 让假模型先输出一段内容，再取消，验证部分内容会保留。
    await asyncio.sleep(0.15)

    canceled = await client.post(f"/api/v1/runs/{result.run_id}/cancel", headers=headers)
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"

    await _wait_terminal(client, headers, str(result.run_id))
    for _ in range(100):
        if not registry.is_running(result.run_id):
            break
        await asyncio.sleep(0.02)

    messages = await client.get(
        f"/api/v1/conversations/{result.conversation_id}/messages", headers=headers
    )
    assistant = next(item for item in messages.json()["items"] if item["role"] == "assistant")
    assert assistant["status"] == "canceled"
    assert assistant["content"]

    events = await client.get(f"/api/v1/runs/{result.run_id}/events?types=message", headers=headers)
    assert any(item["status"] == "canceled" for item in events.json()["items"])

    again = await client.post(f"/api/v1/runs/{result.run_id}/cancel", headers=headers)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "run_not_cancelable"


async def test_validation_and_capability_errors(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    model_error = await client.post(
        "/api/v1/chat/stream",
        json={"message": "hi", "model": "not-allowed"},
        headers=headers,
    )
    assert model_error.status_code == 422
    assert model_error.json()["error"]["code"] == "model_not_allowed"

    capability_error = await client.post(
        "/api/v1/chat/stream",
        json={"message": "hi", "skills": ["00000000-0000-4000-8000-000000000000"]},
        headers=headers,
    )
    assert capability_error.status_code == 403
    assert capability_error.json()["error"]["code"] == "capability_disabled"

    effort_error = await client.post(
        "/api/v1/chat/stream",
        json={"message": "hi", "reasoning_effort": "extreme"},
        headers=headers,
    )
    assert effort_error.status_code == 422
    assert effort_error.json()["error"]["code"] == "reasoning_effort_invalid"

    workflow_conflict = await client.post(
        "/api/v1/chat/stream",
        json={
            "message": "hi",
            "workflow_id": "00000000-0000-4000-8000-000000000000",
            "skills": ["00000000-0000-4000-8000-000000000001"],
        },
        headers=headers,
    )
    assert workflow_conflict.status_code == 422
    assert workflow_conflict.json()["error"]["code"] == "workflow_context_conflict"

    empty = await client.post("/api/v1/chat/stream", json={"message": "  "}, headers=headers)
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "validation_error"


async def test_system_info_and_readyz(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    info = await client.get("/api/v1/system/info", headers=headers)
    assert info.status_code == 200
    body = info.json()
    assert body["default_model"]
    assert body["models"][0]["capabilities"]["streaming"] is True
    assert body["capabilities"]["approvals"] is True
    assert body["capabilities"]["skills"] is False
    assert body["limits"]["message_max_chars"] == 8000
    assert body["a2a"]["card_url"].endswith("/a2a/.well-known/agent-card.json")

    health = await client.get("/healthz")
    assert health.status_code == 200
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
