"""A2A SDK 客户端单元测试：版本协商、1.0 响应解析、错误映射。

M2 集成测试通过假 Agent 走 0.3 兼容传输；本文件固定 1.0 传输路径，
避免迁移后「只兼容 0.3」的隐性回归。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from app.modules.a2a.client import (
    A2aClient,
    A2aError,
    clear_test_transports,
    register_test_transport,
)

_AGENT_URL = "http://a2a.test"

_CARD_10: dict = {
    "protocolVersion": "1.0.0",
    "name": "SDK Test Agent",
    "description": "unit",
    "url": _AGENT_URL,
    "capabilities": {"streaming": True, "pushNotifications": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{"id": "test", "name": "测试", "description": "测试技能"}],
}

_CARD_03: dict = {**_CARD_10, "protocolVersion": "0.3.0"}


@pytest.fixture(autouse=True)
def _clean_transports() -> Iterator[None]:
    clear_test_transports()
    yield
    clear_test_transports()


def _task_payload() -> dict:
    return {
        "id": "task-1",
        "contextId": "ctx-1",
        "status": {
            "state": "TASK_STATE_COMPLETED",
            "message": {
                "messageId": "msg-1",
                "role": "ROLE_AGENT",
                "parts": [{"text": "完成"}],
                "metadata": {"progress": 1.0},
            },
        },
        "artifacts": [
            {
                "artifactId": "art-1",
                "name": "result",
                "parts": [{"text": "结果文本"}],
                "metadata": {"type": "text"},
            }
        ],
    }


async def test_transport_selection_by_card_version() -> None:
    from a2a.client.transports.jsonrpc import JsonRpcTransport
    from a2a.compat.v0_3.jsonrpc_transport import CompatJsonRpcTransport

    async with httpx.AsyncClient(trust_env=False) as http_client:
        client_10 = A2aClient(_AGENT_URL, card=_CARD_10)
        card_10 = await client_10._resolve_card(http_client)
        assert isinstance(client_10._build_transport(http_client, card_10), JsonRpcTransport)

        client_03 = A2aClient(_AGENT_URL, card=_CARD_03)
        card_03 = await client_03._resolve_card(http_client)
        assert isinstance(
            client_03._build_transport(http_client, card_03), CompatJsonRpcTransport
        )


async def test_send_message_parses_1_0_task() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"task": _task_payload()}},
        )

    register_test_transport(_AGENT_URL, httpx.MockTransport(handler))
    client = A2aClient(_AGENT_URL, card=_CARD_10, timeout=5)
    updates = await client.send_message(text="hello", context_id="ctx-1")

    assert calls[0]["method"] == "SendMessage"
    assert calls[0]["params"]["message"]["parts"][0]["text"] == "hello"
    assert calls[0]["params"]["message"]["contextId"] == "ctx-1"

    update = updates[0]
    assert update.kind == "task"
    assert update.task_id == "task-1"
    assert update.context_id == "ctx-1"
    assert update.state == "completed"
    assert update.final is True
    assert update.progress == 1.0
    assert update.status_message is not None
    assert update.status_message["parts"][0]["text"] == "完成"
    assert update.artifacts[0]["parts"][0]["text"] == "结果文本"


async def test_jsonrpc_error_maps_to_remote_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "method not found"},
            },
        )

    register_test_transport(_AGENT_URL, httpx.MockTransport(handler))
    with pytest.raises(A2aError) as exc_info:
        await A2aClient(_AGENT_URL, card=_CARD_10).send_message(text="hello")
    assert exc_info.value.code == "a2a_remote_error"
    assert exc_info.value.retryable is False


async def test_http_401_maps_to_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    register_test_transport(_AGENT_URL, httpx.MockTransport(handler))
    with pytest.raises(A2aError) as exc_info:
        await A2aClient(_AGENT_URL, card=_CARD_10).send_message(text="hello")
    assert exc_info.value.code == "a2a_auth_failed"
    assert exc_info.value.retryable is False


async def test_stream_message_parses_1_0_events() -> None:
    """1.0 流式：SSE data 为 JSON-RPC 信封，result 为 StreamResponse。"""
    frames = [
        {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "statusUpdate": {
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                    "status": {"state": "TASK_STATE_WORKING"},
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "artifactUpdate": {
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                    "artifact": {
                        "artifactId": "art-1",
                        "name": "result",
                        "parts": [{"text": "流式结果"}],
                    },
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "statusUpdate": {
                    "taskId": "task-1",
                    "contextId": "ctx-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                }
            },
        },
    ]
    body = "".join(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    register_test_transport(_AGENT_URL, httpx.MockTransport(handler))
    updates = [
        update
        async for update in A2aClient(_AGENT_URL, card=_CARD_10).stream_message(text="hello")
    ]

    assert [update.kind for update in updates] == ["task", "artifact", "task"]
    assert updates[0].state == "working"
    assert updates[0].task_id == "task-1"
    assert updates[1].artifact is not None
    assert updates[1].artifact["parts"][0]["text"] == "流式结果"
    assert updates[2].state == "completed"
    assert updates[2].final is True
