"""MCP 集成测试：CRUD、健康检查与工具发现、审批策略、越权与聊天工具调用。"""

from __future__ import annotations

import json

import httpx

from app.modules.mcp.service import register_test_transport
from tests.conftest import auth_headers, collect_sse, register_user
from tests.helpers import (
    MOCK_TOOL_READ,
    MOCK_TOOL_WRITE,
    make_mcp_transport,
    make_scripted_model,
)

MOCK_URL = "https://mcp.example.com/mcp"


def _register_mock(tools: list[dict]) -> None:
    register_test_transport(MOCK_URL, make_mcp_transport(tools))


async def _create_server(client: httpx.AsyncClient, auth: dict, **overrides) -> dict:
    payload = {
        "name": "测试 MCP",
        "slug": "test-mcp",
        "transport": "streamable_http",
        "url": MOCK_URL,
        **overrides,
    }
    response = await client.post("/api/v1/mcp/servers", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_server_discovers_tools(client: httpx.AsyncClient) -> None:
    _register_mock([MOCK_TOOL_READ, MOCK_TOOL_WRITE])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    server = await _create_server(client, headers)
    assert server["scope"] == "user"
    assert server["health_status"] == "healthy"
    assert server["tool_count"] == 2
    assert server["effective_enabled"] is True

    tools = await client.get(f"/api/v1/mcp/servers/{server['id']}/tools", headers=headers)
    assert tools.status_code == 200
    rows = {item["name"]: item for item in tools.json()}
    assert rows["search"]["risk"] == "read"
    assert rows["search"]["requires_approval"] is False
    assert rows["search"]["exposed_name"] == "mcp__test-mcp__search"
    assert rows["create_issue"]["requires_approval"] is True
    assert rows["create_issue"]["risk"] == "unknown"

    listing = await client.get("/api/v1/mcp/servers", headers=headers)
    assert listing.json()["stats"]["user"] == 1
    assert listing.json()["stats"]["healthy"] == 1

    available = await client.get("/api/v1/mcp/available", headers=headers)
    assert available.json()["items"][0]["slug"] == "test-mcp"


async def test_check_and_refresh_tools(client: httpx.AsyncClient) -> None:
    _register_mock([MOCK_TOOL_READ])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    server = await _create_server(client, headers)

    checked = await client.post(f"/api/v1/mcp/servers/{server['id']}/check", headers=headers)
    assert checked.status_code == 200
    assert checked.json()["health_status"] == "healthy"
    assert checked.json()["tools_discovered"] == 1

    refreshed = await client.post(
        f"/api/v1/mcp/servers/{server['id']}/refresh-tools", headers=headers
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["tools_discovered"] == 1


async def test_server_enable_disable_and_delete(client: httpx.AsyncClient) -> None:
    _register_mock([MOCK_TOOL_READ])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    server = await _create_server(client, headers)

    disabled = await client.put(
        f"/api/v1/mcp/servers/{server['id']}/enabled", json={"enabled": False}, headers=headers
    )
    assert disabled.json()["effective_enabled"] is False
    enabled = await client.put(
        f"/api/v1/mcp/servers/{server['id']}/enabled", json={"enabled": True}, headers=headers
    )
    assert enabled.json()["effective_enabled"] is True

    deleted = await client.delete(f"/api/v1/mcp/servers/{server['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (
        await client.get(f"/api/v1/mcp/servers/{server['id']}", headers=headers)
    ).status_code == 404


async def test_tool_patch_policy(client: httpx.AsyncClient) -> None:
    _register_mock([MOCK_TOOL_READ, MOCK_TOOL_WRITE])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    server = await _create_server(client, headers)
    tools = (await client.get(f"/api/v1/mcp/servers/{server['id']}/tools", headers=headers)).json()
    write_tool = next(item for item in tools if item["name"] == "create_issue")

    # 未知风险工具可关闭审批
    patched = await client.patch(
        f"/api/v1/mcp/tools/{write_tool['id']}",
        json={"requires_approval": False, "enabled": False},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["requires_approval"] is False
    assert patched.json()["enabled"] is False

    # 破坏性工具不能关闭审批
    destructive = [{"name": "drop_all", "description": "清库", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}}]
    _register_mock([MOCK_TOOL_READ, *destructive])
    server2 = await _create_server(client, headers, slug="test-mcp-2")
    tools2 = (await client.get(f"/api/v1/mcp/servers/{server2['id']}/tools", headers=headers)).json()
    drop = next(item for item in tools2 if item["name"] == "drop_all")
    assert drop["risk"] == "destructive"
    locked = await client.patch(
        f"/api/v1/mcp/tools/{drop['id']}", json={"requires_approval": False}, headers=headers
    )
    assert locked.status_code == 422


async def test_mcp_create_validation(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    # stdio 对普通用户禁用
    stdio = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "x", "slug": "stdio-x", "transport": "stdio", "command": "cat"},
        headers=headers,
    )
    assert stdio.status_code == 422
    assert stdio.json()["error"]["code"] == "mcp_transport_not_allowed"

    # SSRF：内网地址拒绝
    ssrf = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "x", "slug": "ssrf-x", "transport": "streamable_http", "url": "http://127.0.0.1:8080/mcp"},
        headers=headers,
    )
    assert ssrf.status_code == 422
    assert ssrf.json()["error"]["code"] == "mcp_ssrf_blocked"

    # slug 非法
    bad_slug = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "x", "slug": "Bad Slug!", "transport": "streamable_http", "url": MOCK_URL},
        headers=headers,
    )
    assert bad_slug.status_code == 422


async def test_mcp_credentials_never_exposed(client: httpx.AsyncClient) -> None:
    _register_mock([MOCK_TOOL_READ])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    server = await _create_server(
        client, headers, **{"headers": {"Authorization": "Bearer ghp_supersecret"}}
    )
    assert "ghp_supersecret" not in json.dumps(server)
    assert server["credentials_hint"]

    detail = await client.get(f"/api/v1/mcp/servers/{server['id']}", headers=headers)
    assert "ghp_supersecret" not in json.dumps(detail.json())


async def test_mcp_cross_user_isolation(client: httpx.AsyncClient, second_client: httpx.AsyncClient) -> None:
    _register_mock([MOCK_TOOL_READ])
    alice = await register_user(client)
    bob = await register_user(second_client)
    server = await _create_server(client, auth_headers(alice["access_token"]))
    bob_headers = auth_headers(bob["access_token"])

    assert (
        await second_client.get(f"/api/v1/mcp/servers/{server['id']}", headers=bob_headers)
    ).status_code == 404
    assert (
        await second_client.delete(f"/api/v1/mcp/servers/{server['id']}", headers=bob_headers)
    ).status_code == 404
    assert (
        await second_client.patch(
            f"/api/v1/mcp/servers/{server['id']}", json={"name": "hack"}, headers=bob_headers
        )
    ).status_code == 404
    assert (
        await second_client.get(f"/api/v1/mcp/servers/{server['id']}/tools", headers=bob_headers)
    ).status_code == 404
    assert (await second_client.get("/api/v1/mcp/servers", headers=bob_headers)).json()["items"] == []


async def test_chat_mcp_read_tool_called(client: httpx.AsyncClient, monkeypatch) -> None:
    """自动注入的只读 MCP 工具被模型调用：tool.start/result(source=mcp) + 落库。"""
    from sqlalchemy import select

    from app.db.models.memory import ToolInvocation
    from app.db.session import get_session_factory
    from app.modules.chat import service as chat_service

    _register_mock([MOCK_TOOL_READ])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_server(client, headers)

    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {"name": "mcp__test-mcp__search", "args": {"query": "退货政策"}}
                ]
            },
            {"text": "搜索结果如上"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(
        client, headers=headers, payload={"message": "查一下退货政策"}
    )
    names = [name for name, _ in events]
    assert "tool.start" in names and "tool.result" in names
    assert names[-1] == "message.done"
    started = next(data for name, data in events if name == "tool.start")
    assert started["source"] == "mcp"
    assert started["server"] == "test-mcp"
    result = next(data for name, data in events if name == "tool.result")
    assert result["status"] == "ok"
    assert "ok:search" in result["summary"]

    async with get_session_factory()() as db:
        invocation = (await db.execute(select(ToolInvocation))).scalars().first()
    assert invocation is not None
    assert invocation.source == "mcp"
    assert invocation.tool_name == "mcp__test-mcp__search"
    assert invocation.status == "ok"


async def _start_chat_service(registered: dict, message: str):
    """服务层启动运行（ASGITransport 会缓冲整个 SSE 流，实时交互必须走服务层）。"""
    import uuid

    from sqlalchemy import select

    from app.db.models.user import User
    from app.db.session import get_session_factory
    from app.modules.chat.schemas import ChatStreamRequest
    from app.modules.chat.service import start_chat

    async with get_session_factory()() as session:
        user = await session.scalar(select(User).where(User.id == uuid.UUID(registered["user"]["id"])))
        assert user is not None
        return await start_chat(session, user, ChatStreamRequest(message=message))


async def test_chat_mcp_write_tool_approval(client: httpx.AsyncClient, monkeypatch) -> None:
    """写工具触发审批：approval.required → 批准 → 执行并继续流（服务层实时验证）。"""
    from app.modules.chat import service as chat_service
    from tests.conftest import collect_run_events_live

    _register_mock([MOCK_TOOL_WRITE])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_server(client, headers)

    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {"name": "mcp__test-mcp__create_issue", "args": {"title": "问题"}}
                ]
            },
            {"text": "工单已创建"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    result = await _start_chat_service(registered, "帮我创建工单：问题")

    decided: dict[str, str] = {}

    async def on_event(name: str, data: dict) -> None:
        if name == "approval.required" and not decided:
            decision = await client.post(
                f"/api/v1/approvals/{data['approval_id']}/decision",
                json={"decision": "approve", "note": "确认"},
                headers=headers,
            )
            decided["response"] = decision.text
            assert decision.status_code == 200, decision.text
            assert decision.json()["resumed"] is True

    events = await collect_run_events_live(str(result.run_id), on_event=on_event)
    assert decided, "未触发 approval.required"
    names = [name for name, _ in events]
    assert "tool.result" in names
    result_event = next(data for name, data in events if name == "tool.result")
    assert result_event["status"] == "ok"
    assert "ok:create_issue" in result_event["summary"]
    assert events[-1][0] == "message.done"
    assert events[-1][1]["finish_reason"] == "stop"

    approvals = await client.get("/api/v1/approvals?status=approved", headers=headers)
    assert approvals.json()["stats"]["approved"] == 1


async def test_chat_mcp_write_tool_rejected(client: httpx.AsyncClient, monkeypatch) -> None:
    """写工具审批被拒绝：不执行，tool.result status=rejected，模型收尾。"""
    from sqlalchemy import select

    from app.db.models.memory import ToolInvocation
    from app.db.session import get_session_factory
    from app.modules.chat import service as chat_service
    from tests.conftest import collect_run_events_live

    _register_mock([MOCK_TOOL_WRITE])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_server(client, headers)

    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {"name": "mcp__test-mcp__create_issue", "args": {"title": "问题"}}
                ]
            },
            {"text": "好的，已取消"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    result = await _start_chat_service(registered, "创建工单")

    async def on_event(name: str, data: dict) -> None:
        if name == "approval.required":
            decision = await client.post(
                f"/api/v1/approvals/{data['approval_id']}/decision",
                json={"decision": "reject"},
                headers=headers,
            )
            assert decision.status_code == 200, decision.text
            assert decision.json()["resumed"] is True

    events = await collect_run_events_live(str(result.run_id), on_event=on_event)
    result_event = next(data for name, data in events if name == "tool.result")
    assert result_event["status"] == "rejected"
    assert events[-1][0] == "message.done"

    async with get_session_factory()() as db:
        invocation = (await db.execute(select(ToolInvocation))).scalars().first()
    assert invocation.status == "rejected"

    approvals = await client.get("/api/v1/approvals?status=rejected", headers=headers)
    assert approvals.json()["stats"]["rejected"] == 1


async def test_chat_mcp_approval_timeout_interrupts(client: httpx.AsyncClient, monkeypatch) -> None:
    """审批超时：message.done.finish_reason=interrupted，run 进入 interrupted。"""
    from app.modules.chat import service as chat_service

    _register_mock([MOCK_TOOL_WRITE])
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await _create_server(client, headers)

    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {"name": "mcp__test-mcp__create_issue", "args": {"title": "问题"}}
                ]
            },
            {"text": "已创建"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    run_id, events = await collect_sse(client, headers=headers, payload={"message": "创建工单"})
    names = [name for name, _ in events]
    assert "approval.required" in names
    done = events[-1][1]
    assert done["finish_reason"] == "interrupted"
    assert done["reason"] == "approval_timeout"

    run = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
    assert run.json()["status"] == "interrupted"

    # 超时审批再决定 → 409 approval_expired
    approval = next(data for name, data in events if name == "approval.required")
    late = await client.post(
        f"/api/v1/approvals/{approval['approval_id']}/decision",
        json={"decision": "approve"},
        headers=headers,
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "approval_expired"


async def test_chat_mcp_explicit_unreachable_degrades(client: httpx.AsyncClient, monkeypatch) -> None:
    """显式选择不可达 MCP：发 notice 降级，对话不中断。"""
    from app.modules.chat import service as chat_service

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    register_test_transport(MOCK_URL, httpx.MockTransport(failing))
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    # check 失败但配置保存为 unhealthy。
    response = await client.post(
        "/api/v1/mcp/servers",
        json={"name": "不可达", "slug": "unreachable", "transport": "streamable_http", "url": MOCK_URL},
        headers=headers,
    )
    assert response.status_code == 201
    server = response.json()
    assert server["health_status"] == "unhealthy"

    model = make_scripted_model([{"text": "MCP 不可用，我直接回答"}])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(
        client,
        headers=headers,
        payload={"message": "查数据", "mcp_servers": [server["id"]]},
    )
    names = [name for name, _ in events]
    assert names[-1] == "message.done"
    assert events[-1][1]["finish_reason"] == "stop"
    assert "notice" in names
    notice = next(data for name, data in events if name == "notice")
    assert notice["code"] == "mcp_unavailable"
