"""远程 Agent 集成测试：preview / 注册 / 列表 / 详情 / PATCH / refresh / health / 删除 + SSRF。"""

from __future__ import annotations

import httpx

from app.modules.a2a.client import register_test_transport
from tests.conftest import auth_headers, register_user
from tests.fixtures.fake_a2a_agent import FakeA2aAgent

AGENT_URL = "http://a2a.test"


def _register_fake(
    *, streaming: bool = False, requires_approval: bool = False, name: str = "Fake Agent"
) -> FakeA2aAgent:
    fake = FakeA2aAgent(
        name=name,
        url=AGENT_URL,
        streaming=streaming,
        requires_approval=requires_approval,
    )
    register_test_transport(AGENT_URL, httpx.ASGITransport(app=fake.app))
    return fake


async def _create_agent(client: httpx.AsyncClient, headers: dict, **overrides) -> dict:
    payload = {"url": AGENT_URL, **overrides}
    response = await client.post("/api/v1/agents", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_preview_valid_and_reject_bad_urls(client: httpx.AsyncClient, monkeypatch) -> None:
    _register_fake(streaming=True)
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    preview = await client.post(
        "/api/v1/agents/preview", json={"url": AGENT_URL}, headers=headers
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["valid"] is True
    # 假 Agent 实现的是 0.3 JSON-RPC 方法名，Card 如实声明 0.3.0。
    assert body["card"]["protocolVersion"] == "0.3.0"
    assert body["normalized"]["name"] == "Fake Agent"
    assert body["normalized"]["capabilities"] == {
        "streaming": True,
        "push_notifications": False,
    }
    assert body["normalized"]["skills"][0]["id"] == "fake_task"
    assert body["warnings"] == []

    # 落库副作用检查：预览后列表仍为空。
    listing = await client.get("/api/v1/agents", headers=headers)
    assert listing.json()["stats"]["total"] == 0

    # SSRF：私网/链路本地/云元数据/非 http(s) 一律拒绝。
    for bad_url in (
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://169.254.169.254",
        "ftp://example.com",
        "http://user:pass@a2a.test",
    ):
        denied = await client.post(
            "/api/v1/agents/preview", json={"url": bad_url}, headers=headers
        )
        assert denied.status_code == 422, (bad_url, denied.text)
        assert denied.json()["error"]["code"] == "agent_url_not_allowed"

    # loopback：dev/test 默认放行（A2A_ALLOW_LOOPBACK），显式关闭后拒绝。
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "a2a_allow_loopback", False)
    denied = await client.post(
        "/api/v1/agents/preview", json={"url": "http://127.0.0.1:9"}, headers=headers
    )
    assert denied.status_code == 422
    assert denied.json()["error"]["code"] == "agent_url_not_allowed"
    monkeypatch.setattr(settings, "a2a_allow_loopback", None)
    allowed = await client.post(
        "/api/v1/agents/preview", json={"url": "http://127.0.0.1:9"}, headers=headers
    )
    assert allowed.status_code == 422  # 放行后真实连接失败 → agent_card_invalid
    assert allowed.json()["error"]["code"] == "agent_card_invalid"

    # Card 不可达（白名单 host 但服务不存在）。
    missing = await client.post(
        "/api/v1/agents/preview", json={"url": "http://a2a.test:1"}, headers=headers
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "agent_card_invalid"

    # 非法 slug。
    bad_slug = await client.post(
        "/api/v1/agents/preview", json={"url": AGENT_URL, "slug": "Bad Slug!"}, headers=headers
    )
    assert bad_slug.status_code == 422


async def test_create_list_detail_patch_delete(client: httpx.AsyncClient) -> None:
    fake = _register_fake(streaming=True)
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    agent = await _create_agent(client, headers, slug="fake-agent", name="我的假 Agent")
    assert agent["slug"] == "fake-agent"
    assert agent["name"] == "我的假 Agent"
    assert agent["health_status"] == "unknown"
    assert agent["enabled"] is True
    assert agent["skill_count"] == 1
    assert agent["skills"][0]["id"] == "fake_task"
    assert agent["capabilities"]["streaming"] is True
    assert agent["auth_type"] == "none"
    assert agent["auth_scheme"] == "none"
    assert agent["recent_task"] is None

    # 幂等：同 url + 同 slug → 返回已有（200）。
    again = await client.post(
        "/api/v1/agents", json={"url": AGENT_URL, "slug": "fake-agent"}, headers=headers
    )
    assert again.status_code == 201
    assert again.json()["id"] == agent["id"]

    # slug 相同但 URL 不同 → 409。
    conflict = await client.post(
        "/api/v1/agents",
        json={"url": "http://a2a.test:8080", "slug": "fake-agent"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "agent_slug_conflict"

    # 列表：字段齐全 + stats。
    listing = await client.get("/api/v1/agents", headers=headers)
    assert listing.status_code == 200
    page = listing.json()
    assert page["stats"] == {
        "total": 1,
        "healthy": 0,
        "degraded": 0,
        "unhealthy": 0,
        "disabled": 0,
        "enabled": 1,
    }
    item = page["items"][0]
    for key in (
        "id",
        "slug",
        "name",
        "description",
        "url",
        "protocol_version",
        "skills",
        "skill_count",
        "auth_type",
        "auth_scheme",
        "auth_hint",
        "capabilities",
        "health_status",
        "enabled",
        "last_checked_at",
        "last_latency_ms",
        "last_error",
        "recent_task",
        "created_at",
        "updated_at",
    ):
        assert key in item

    # 过滤：health_status / enabled / q。
    filtered = await client.get(
        "/api/v1/agents", params={"health_status": "unknown", "q": "假"}, headers=headers
    )
    assert filtered.json()["stats"]["total"] == 1
    none_hit = await client.get("/api/v1/agents", params={"q": "不存在"}, headers=headers)
    assert none_hit.json()["stats"]["total"] == 0
    disabled = await client.get("/api/v1/agents", params={"enabled": "false"}, headers=headers)
    assert disabled.json()["stats"]["total"] == 0

    # 详情：card / recent_tasks / auth_hint。
    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["card"]["name"] == "Fake Agent"
    assert body["recent_tasks"] == []
    assert body["requires_approval"] is False

    # PATCH：改名 + 停用 + 带 secret_ref 的 auth。
    patched = await client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={
            "name": "改名后的 Agent",
            "enabled": False,
            "auth": {"type": "api_key", "secret_ref": "env:FAKE_AGENT_KEY"},
        },
        headers=headers,
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "改名后的 Agent"
    assert body["enabled"] is False
    assert body["auth_type"] == "api_key"
    assert body["auth_scheme"] == "bearer"
    assert body["auth_hint"] == "env:FAKE_AGENT_KEY"

    # 列表 stats：disabled=1。
    listing = await client.get("/api/v1/agents", headers=headers)
    assert listing.json()["stats"]["disabled"] == 1
    assert listing.json()["stats"]["enabled"] == 0

    # 删除：204；再次查询 404；重新注册同名 slug 成功（部分唯一索引）。
    deleted = await client.delete(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert deleted.status_code == 204
    missing = await client.get(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert missing.status_code == 404
    recreated = await _create_agent(client, headers, slug="fake-agent")
    assert recreated["id"] != agent["id"]
    assert fake.call_count == 0  # 注册只拉 Card，不发消息。


async def test_refresh_and_health(client: httpx.AsyncClient) -> None:
    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    agent = await _create_agent(client, headers)

    refreshed = await client.post(f"/api/v1/agents/{agent['id']}/refresh", headers=headers)
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["changed"] is False
    assert body["changed_fields"] == []
    assert body["agent"]["health_status"] == "healthy"

    health = await client.post(f"/api/v1/agents/{agent['id']}/health", headers=headers)
    assert health.status_code == 200
    result = health.json()
    assert result["health_status"] == "healthy"
    assert result["latency_ms"] is not None
    assert result["checked_at"]
    assert result["error"] is None

    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert detail.json()["last_checked_at"] is not None


async def test_refresh_failure_keeps_old_card(client: httpx.AsyncClient) -> None:
    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    agent = await _create_agent(client, headers)

    # 卡不可达：422 + 标记 unhealthy，旧 card 保留。
    register_test_transport(AGENT_URL, httpx.MockTransport(lambda request: httpx.Response(500)))
    failed = await client.post(f"/api/v1/agents/{agent['id']}/refresh", headers=headers)
    assert failed.status_code == 422
    assert failed.json()["error"]["code"] == "agent_card_invalid"

    detail = await client.get(f"/api/v1/agents/{agent['id']}", headers=headers)
    body = detail.json()
    assert body["health_status"] == "degraded"
    assert body["card"]["name"] == "Fake Agent"


async def test_delete_with_active_tasks_conflict(client: httpx.AsyncClient) -> None:
    """软删：有运行中任务时 409（active 任务由直接落库模拟）。"""
    import uuid as uuid_module

    from app.db.models.remote import A2aTask
    from app.db.session import get_session_factory

    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    agent = await _create_agent(client, headers)

    async with get_session_factory()() as db:
        db.add(
            A2aTask(
                a2a_task_id="task_active",
                remote_agent_id=uuid_module.UUID(agent["id"]),
                state="working",
                request={"tool": "send_message", "args": {}, "summary": "x"},
            )
        )
        await db.commit()

    denied = await client.delete(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "agent_has_active_tasks"

    # 清理后删除成功。
    async with get_session_factory()() as db:
        from sqlalchemy import update

        await db.execute(
            update(A2aTask)
            .where(A2aTask.remote_agent_id == uuid_module.UUID(agent["id"]))
            .values(state="completed")
        )
        await db.commit()
    deleted = await client.delete(f"/api/v1/agents/{agent['id']}", headers=headers)
    assert deleted.status_code == 204


async def test_preview_and_create_with_auth(client: httpx.AsyncClient) -> None:
    """api_key + secret_ref 注册：auth_hint 只回显引用。"""
    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    created = await client.post(
        "/api/v1/agents",
        json={"url": AGENT_URL, "auth": {"type": "api_key", "secret_ref": "env:A2A_KEY"}},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["auth_type"] == "api_key"
    assert body["auth_hint"] == "env:A2A_KEY"
    assert "A2A_KEY" not in body.get("headers", "")

    # PATCH headers：敏感头只回 hint。
    patched = await client.patch(
        f"/api/v1/agents/{body['id']}",
        json={"headers": {"X-Custom": "plain", "Authorization": "env:TOKEN"}},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["auth_hint"] == "Authorization: env:TOKEN"
