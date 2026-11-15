"""LangChain 模拟智能体集群的离线单测（不连 LLM、不连数据库、不发网络请求）。

覆盖两层：

- A2A 协议壳：Agent Card、``message/send``、``message/stream``、``tasks/get``、
  ``tasks/cancel``，以及健康探针依赖的「任务不存在返回 JSON-RPC error」；
- 与项目真实出站客户端 ``A2aClient``（a2a-sdk 1.3）的兼容性，避免 mock 只对自己
  的请求格式成立。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.modules.a2a import A2aClient, A2aError
from app.modules.a2a.client import clear_test_transports, register_test_transport
from scripts.mock_langchain_agents import (
    AgentSpec,
    MockAgentCluster,
    MockTask,
    _agent_urls,
    _default_public_url,
    _normalize_base_path,
    _register_one,
    calculator_tool,
    load_agent_specs,
)

AGENT_URL = "http://127.0.0.1:9199/agents/demo"
RPC_PATH = "/agents/demo"


def _spec(**overrides: object) -> AgentSpec:
    data: dict[str, object] = {
        "slug": "demo",
        "name": "演示助手",
        "description": "离线单测用的演示智能体",
        "prompt": "你是演示助手，回答简短。",
        "skills": [
            {
                "id": "demo_task",
                "name": "演示任务",
                "description": "执行演示任务",
                "tags": ["demo"],
                "examples": ["你好"],
            }
        ],
        "temperature": 0.2,
    }
    data.update(overrides)
    return AgentSpec(**data)  # type: ignore[arg-type]


@pytest.fixture
def cluster() -> MockAgentCluster:
    return MockAgentCluster(
        [_spec()],
        public_url="http://127.0.0.1:9199",
        use_fake_llm=True,
        fake_delay_ms=0,
        slow_seconds=0.05,
    )


@pytest.fixture
async def client(cluster: MockAgentCluster):
    transport = ASGITransport(app=cluster.app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:9199") as http:
        yield http


@pytest.fixture
def sdk_url(cluster: MockAgentCluster):
    """把 A2aClient 对该 URL 的出站请求接到 ASGI app 上。"""
    register_test_transport(AGENT_URL, ASGITransport(app=cluster.app))
    try:
        yield AGENT_URL
    finally:
        clear_test_transports()


async def _rpc(
    client: AsyncClient, method: str, params: dict | None = None, request_id: str = "1"
):
    return await client.post(
        RPC_PATH,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
    )


def _send_params(text: str) -> dict:
    return {
        "message": {
            "role": "user",
            "messageId": "m-1",
            "parts": [{"kind": "text", "text": text}],
        }
    }


# --------------------------------------------------------------------------- Card


async def test_agent_card_is_a2a_03_and_same_origin(client: AsyncClient) -> None:
    response = await client.get(f"{RPC_PATH}/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    assert card["protocolVersion"] == "0.3.0"
    assert card["url"] == AGENT_URL
    assert card["name"] == "演示助手"
    assert card["skills"][0]["id"] == "demo_task"
    assert card["capabilities"]["streaming"] is True


async def test_unknown_agent_card_returns_404(client: AsyncClient) -> None:
    response = await client.get("/agents/missing/.well-known/agent-card.json")
    assert response.status_code == 404


# --------------------------------------------------------------------------- JSON-RPC


async def test_message_send_returns_completed_task(client: AsyncClient) -> None:
    response = await _rpc(client, "message/send", _send_params("你好"))
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    task = body["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"]
    assert task["artifacts"][0]["metadata"]["agent"] == "demo"

    again = await _rpc(client, "tasks/get", {"id": task["id"]})
    assert again.json()["result"]["status"]["state"] == "completed"


async def test_unknown_task_is_rpc_error_for_health_probe(client: AsyncClient) -> None:
    response = await _rpc(client, "tasks/get", {"id": "__agent_hub_health_probe__"})
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == -32001


async def test_invalid_json_returns_parse_error(client: AsyncClient) -> None:
    response = await client.post(
        RPC_PATH, content="{not json", headers={"Content-Type": "application/json"}
    )
    assert response.json()["error"]["code"] == -32700


async def test_non_object_request_returns_invalid_request(client: AsyncClient) -> None:
    response = await client.post(RPC_PATH, json=[{"jsonrpc": "2.0"}])
    assert response.json()["error"]["code"] == -32600

@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {"message": "oops"},
        {"message": {"parts": []}},
        {"message": {"role": "user"}},
    ],
)
async def test_message_send_invalid_params_returns_32602(
    client: AsyncClient, params: object
) -> None:
    response = await _rpc(client, "message/send", params)
    assert response.json()["error"]["code"] == -32602


async def test_message_stream_invalid_params_returns_32602(client: AsyncClient) -> None:
    response = await _rpc(client, "message/stream", {"message": "oops"})
    assert response.json()["error"]["code"] == -32602


async def test_tasks_cancel_marks_running_task_canceled(
    cluster: MockAgentCluster, client: AsyncClient
) -> None:
    task = MockTask(
        id="task_running", context_id="ctx", state="working", status_text="处理中…"
    )
    cluster.tasks[task.id] = task
    canceled = await _rpc(client, "tasks/cancel", {"id": task.id})
    assert canceled.json()["result"]["status"]["state"] == "canceled"
    assert task.cancel_event.is_set()


async def test_cancel_terminal_task_returns_not_cancelable(client: AsyncClient) -> None:
    sent = await _rpc(client, "message/send", _send_params("你好"))
    task_id = sent.json()["result"]["id"]
    response = await _rpc(client, "tasks/cancel", {"id": task_id})
    assert response.json()["error"]["code"] == -32002


async def test_message_stream_emits_working_artifact_and_final(client: AsyncClient) -> None:
    response = await _rpc(client, "message/stream", _send_params("流式"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert '"kind": "status-update"' in text
    assert '"kind": "artifact-update"' in text
    assert '"final": true' in text
    # 每个 data 帧都必须是合法 JSON（否则 a2a-sdk 解析会失败）。
    for line in text.splitlines():
        if line.startswith("data: "):
            json.loads(line[len("data: ") :])


async def test_fail_marker_returns_failed_task(client: AsyncClient) -> None:
    response = await _rpc(client, "message/send", _send_params("#fail 故意失败"))
    assert response.json()["result"]["status"]["state"] == "failed"

async def test_message_stream_fail_marker_emits_failed_final(client: AsyncClient) -> None:
    response = await _rpc(client, "message/stream", _send_params("#fail 故意失败"))
    assert '"state": "failed"' in response.text
    assert '"final": true' in response.text


async def test_slow_marker_honors_configured_delay(client: AsyncClient) -> None:
    started = time.monotonic()
    response = await _rpc(client, "message/send", _send_params("#slow 你好"))
    elapsed = time.monotonic() - started
    assert response.json()["result"]["status"]["state"] == "completed"
    assert elapsed >= 0.04


# --------------------------------------------------------------------------- 真实 SDK 兼容


async def test_real_a2a_client_send_and_stream(
    cluster: MockAgentCluster, sdk_url: str
) -> None:
    card = cluster.specs["demo"].card(url=sdk_url)
    updates = await A2aClient(sdk_url, card=card).send_message(text="你好")
    assert updates[-1].state == "completed"
    assert updates[-1].artifacts
    assert updates[-1].artifacts[0]["parts"][0]["text"]

    streamed = [
        update
        async for update in A2aClient(sdk_url, card=card).stream_message(text="流式")
    ]
    assert any(update.kind == "artifact" for update in streamed)
    assert any(update.kind == "task" and update.state == "completed" for update in streamed)


async def test_real_a2a_client_maps_health_probe_error(
    cluster: MockAgentCluster, sdk_url: str
) -> None:
    card = cluster.specs["demo"].card(url=sdk_url)
    with pytest.raises(A2aError) as excinfo:
        await A2aClient(sdk_url, card=card).get_task("__agent_hub_health_probe__")
    # 健康检查把「任务不存在」视为服务可达，因此必须映射成 a2a_remote_error。
    assert excinfo.value.code == "a2a_remote_error"


# --------------------------------------------------------------------------- 配置与工具


def test_load_agent_specs_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n"
        "  - slug: alpha\n"
        "    name: Alpha\n"
        "    description: 第一个智能体\n"
        "    prompt: 你是 Alpha\n"
        "    tools: [calculator]\n"
        "    temperature: 0.1\n",
        encoding="utf-8",
    )
    specs = load_agent_specs(str(path))
    assert [spec.slug for spec in specs] == ["alpha"]
    assert specs[0].tools == ["calculator"]
    assert specs[0].temperature == pytest.approx(0.1)
    # 没写 skills 时回退成一条以 slug 命名的技能，保证 Card 合法。
    assert specs[0].skills[0]["id"] == "alpha"


def test_load_agent_specs_rejects_duplicate_slug(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n"
        "  - slug: dup\n"
        "    prompt: 你是第一个\n"
        "  - slug: dup\n"
        "    prompt: 你是第二个\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="slug 重复"):
        load_agent_specs(str(path))


def test_calculator_tool_is_safe() -> None:
    assert calculator_tool.invoke({"expression": "(1+2)*3"}) == "9"
    assert calculator_tool.invoke({"expression": "7/2"}) == "3.5"
    rejected = calculator_tool.invoke({"expression": "__import__('os').system('true')"})
    assert rejected.startswith("计算失败")


def test_requires_approval_marks_skill_metadata() -> None:
    card = _spec(requires_approval=True).card(url=AGENT_URL)
    assert card["skills"][0]["metadata"] == {"requiresApproval": True}
    assert "metadata" not in _spec().card(url=AGENT_URL)["skills"][0]


# --------------------------------------------------------------------------- 配置边界与注册辅助


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"slug": "bad_slug", "prompt": "x"}, "slug"),
        ({"slug": "ok-temp", "prompt": "x", "temperature": "hot"}, "temperature"),
        ({"slug": "ok-tools", "prompt": "x", "tools": "calculator"}, "tools"),
        ({"slug": "ok-stream", "prompt": "x", "streaming": "false"}, "streaming"),
    ],
)
def test_load_agent_specs_rejects_bad_entry(
    tmp_path: Path, entry: dict, match: str
) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"agents": [entry]}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match=match):
        load_agent_specs(str(path))


def test_load_agent_specs_rejects_non_object_entry(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"agents": ["oops"]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="必须是对象"):
        load_agent_specs(str(path))


def test_url_helpers() -> None:
    assert _default_public_url("0.0.0.0", 9101) == "http://127.0.0.1:9101"
    assert _default_public_url("::1", 9101) == "http://[::1]:9101"
    assert _normalize_base_path("/") == ""
    assert _normalize_base_path("/agents/") == "/agents"
    urls = _agent_urls([_spec()], "http://127.0.0.1:9199/", "/agents/")
    assert urls["demo"] == "http://127.0.0.1:9199/agents/demo"


async def test_register_one_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer t"
        return httpx.Response(201, json={"id": "abc"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hub"
    ) as client:
        result = await _register_one(client, "t", slug="demo", name="Demo", url="http://a")
    assert result == {"id": "abc"}


async def test_register_one_handles_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hub"
    ) as client:
        assert (
            await _register_one(client, "t", slug="demo", name="Demo", url="http://a")
            is None
        )


async def test_register_one_forbidden_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "forbidden"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hub"
    ) as client:
        assert (
            await _register_one(client, "t", slug="demo", name="Demo", url="http://a")
            is None
        )
