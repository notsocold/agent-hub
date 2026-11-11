"""管理台全局 Agent（M8 T6）：全局列表、PATCH 白名单、审计与密钥不回显。

覆盖契约：
- ``GET /api/v1/admin/agents`` 复用 ``GET /agents`` 的查询参数，**不按 owner 过滤**
  （R8：v1 ``remote_agents`` 是全局注册表，管理员创建的行 owner 就是管理员本人）；
- ``PATCH /api/v1/admin/agents/{id}`` 只允许 ``name/enabled/requires_approval/
  auth/headers``；``description/url/slug/card/...`` 等只读字段触发 422；
- 改名后置 ``name_edited=True``，``refresh`` 不再用 Card 覆盖；
- ``secret:`` 平台密钥引用与敏感头只存引用，响应/审计不回显；
- 真实变更写 ``agent.updated``，``detail={"fields": [...], "source": "admin"}``；
- 普通用户 403（``details.required_role="admin"``），未登录 401。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, reset_settings_cache
from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.remote import RemoteAgent
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.a2a.client import register_test_transport
from tests.conftest import auth_headers, register_user
from tests.fixtures.fake_a2a_agent import FakeA2aAgent

ADMIN_URL = "/api/v1/admin/agents"
AGENTS_URL = "/api/v1/agents"
AGENT_URL = "http://a2a.test"


def _register_fake(name: str = "Fake Agent") -> FakeA2aAgent:
    fake = FakeA2aAgent(name=name, url=AGENT_URL)
    register_test_transport(AGENT_URL, httpx.ASGITransport(app=fake.app))
    return fake


async def _create_agent(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides
) -> dict:
    payload = {"url": AGENT_URL, **overrides}
    response = await client.post(AGENTS_URL, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _audit_rows(action: str) -> list[AuditLog]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
                )
            ).scalars()
        )


async def test_admin_agents_covers_global_registry(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    """管理员创建（owner=admin）的 Agent 出现在全局管理列表，stats 一致。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="global-agent", name="全局 Agent")

    async with get_session_factory()() as session:
        row = await session.get(RemoteAgent, uuid.UUID(agent["id"]))
        assert row is not None
        assert row.owner_user_id == admin.id

    listing = await client.get(ADMIN_URL, headers=admin_auth)
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert [item["id"] for item in page["items"]] == [agent["id"]]
    assert page["stats"]["total"] == 1
    assert page["stats"]["enabled"] == 1
    assert page["stats"]["disabled"] == 0

    # 查询参数与 GET /agents 一致。
    matched = await client.get(ADMIN_URL, params={"q": "全局"}, headers=admin_auth)
    assert matched.status_code == 200, matched.text
    assert matched.json()["stats"]["total"] == 1
    unmatched = await client.get(ADMIN_URL, params={"enabled": "false"}, headers=admin_auth)
    assert unmatched.json()["stats"]["total"] == 0

    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    forbidden = await client.get(ADMIN_URL, headers=user_headers)
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["error"]["details"] == {"required_role": "admin"}


async def test_admin_agents_stats_ignore_cursor(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """管理端翻页时 stats 仍是筛选条件下的全量计数；用户侧既有口径不变。"""
    _register_fake()
    for index in range(3):
        await _create_agent(
            client, admin_auth, slug=f"cursor-agent-{index}", name=f"翻页 Agent {index}"
        )

    first = await client.get(ADMIN_URL, params={"limit": 1}, headers=admin_auth)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    assert first_body["stats"]["total"] == 3

    second = await client.get(
        ADMIN_URL,
        params={"limit": 1, "cursor": first_body["next_cursor"]},
        headers=admin_auth,
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["stats"]["total"] == 3
    assert second_body["items"][0]["id"] != first_body["items"][0]["id"]

    # GET /agents 保持原有口径（stats 随游标过滤），本修复不能改变用户侧行为。
    user_first = await client.get(AGENTS_URL, params={"limit": 1}, headers=admin_auth)
    assert user_first.status_code == 200, user_first.text
    user_first_body = user_first.json()
    assert user_first_body["stats"]["total"] == 3
    user_second = await client.get(
        AGENTS_URL,
        params={"limit": 1, "cursor": user_first_body["next_cursor"]},
        headers=admin_auth,
    )
    assert user_second.status_code == 200, user_second.text
    assert user_second.json()["stats"]["total"] == 2


async def test_patch_editable_fields_and_audit(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    """PATCH 生效并保护自定义名称；真实变更写 agent.updated，同值 PATCH 不写。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="admin-patch-agent")
    body = {"name": "管理台改名", "enabled": False, "requires_approval": True}

    patched = await client.patch(f"{ADMIN_URL}/{agent['id']}", json=body, headers=admin_auth)
    assert patched.status_code == 200, patched.text
    result = patched.json()
    assert result["name"] == "管理台改名"
    assert result["enabled"] is False
    assert result["requires_approval"] is True

    async with get_session_factory()() as session:
        row = await session.get(RemoteAgent, uuid.UUID(agent["id"]))
        assert row is not None
        assert row.name_edited is True
        assert row.requires_approval is True

    # refresh 用 Card 名称，但 name_edited 保护管理台改名。
    refreshed = await client.post(
        f"{AGENTS_URL}/{agent['id']}/refresh", headers=admin_auth
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["agent"]["name"] == "管理台改名"
    assert refreshed.json()["agent"]["card"]["name"] == "Fake Agent"

    audit = await client.get(
        "/api/v1/admin/audit-logs",
        params={"action": "agent.updated", "limit": 100},
        headers=admin_auth,
    )
    assert audit.status_code == 200, audit.text
    items = audit.json()["items"]
    assert len(items) == 1
    assert items[0]["actor_id"] == str(admin.id)
    assert items[0]["resource_type"] == "agent"
    assert items[0]["resource_id"] == agent["id"]
    assert set(items[0]["detail"]["fields"]) == {"name", "enabled", "requires_approval"}
    assert items[0]["detail"]["source"] == "admin"

    # 同值 PATCH 直接返回详情，不再写审计。
    same = await client.patch(f"{ADMIN_URL}/{agent['id']}", json=body, headers=admin_auth)
    assert same.status_code == 200, same.text
    assert len(await _audit_rows("agent.updated")) == 1


async def test_admin_agent_secret_ref_and_headers_not_echoed(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """``secret:`` auth 与敏感头只存引用；响应/审计不回显明文。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="secret-agent")
    created_secret = await client.post(
        "/api/v1/admin/secrets",
        json={"name": "AGENT_KEY", "value": "agent-secret-value"},
        headers=admin_auth,
    )
    assert created_secret.status_code == 201, created_secret.text

    response = await client.patch(
        f"{ADMIN_URL}/{agent['id']}",
        json={
            "auth": {"type": "api_key", "secret_ref": "secret:AGENT_KEY"},
            "headers": {"Authorization": "env:AGENT_TOKEN"},
        },
        headers=admin_auth,
    )
    assert response.status_code == 200, response.text
    assert "agent-secret-value" not in response.text
    assert "ciphertext" not in response.text
    body = response.json()
    assert "headers" not in body
    assert body["auth_type"] == "api_key"
    assert "env:AGENT_TOKEN" in body["auth_hint"]

    async with get_session_factory()() as session:
        row = await session.get(RemoteAgent, uuid.UUID(agent["id"]))
        assert row is not None
        assert row.secret_ref == "secret:AGENT_KEY"
        assert row.headers == {"Authorization": {"secret_ref": "env:AGENT_TOKEN"}}

    for audit in await _audit_rows("agent.updated"):
        text = json.dumps(audit.detail, ensure_ascii=False)
        assert "agent-secret-value" not in text
        assert "AGENT_TOKEN" not in text
        assert audit.detail == {"fields": ["auth", "headers"], "source": "admin"}


async def test_patch_readonly_field_rejected(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """只读字段（含 description/url/slug/card）显式 422，不静默忽略。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="readonly-agent")
    readonly = {
        "slug": "other-slug",
        "url": "http://a2a.test:8080",
        "description": "想改",
        "protocol_version": "0.3.0",
        "card": {"name": "x"},
        "skills": [],
        "capabilities": {},
        "health_status": "healthy",
        "owner_user_id": str(uuid.uuid4()),
    }
    for field, value in readonly.items():
        response = await client.patch(
            f"{ADMIN_URL}/{agent['id']}", json={field: value}, headers=admin_auth
        )
        assert response.status_code == 422, (field, response.text)
        assert response.json()["error"]["code"] == "validation_error"


async def test_patch_empty_and_not_found(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="empty-agent")

    empty = await client.patch(f"{ADMIN_URL}/{agent['id']}", json={}, headers=admin_auth)
    assert empty.status_code == 422, empty.text
    assert empty.json()["error"]["code"] == "validation_error"

    blank_name = await client.patch(
        f"{ADMIN_URL}/{agent['id']}", json={"name": "   "}, headers=admin_auth
    )
    assert blank_name.status_code == 422, blank_name.text
    assert blank_name.json()["error"]["code"] == "validation_error"

    missing = await client.patch(
        f"{ADMIN_URL}/{uuid.uuid4()}", json={"name": "不存在"}, headers=admin_auth
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "not_found"


async def test_patch_soft_deleted_not_found(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """软删后的 Agent 对管理端 PATCH 视为不存在。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="soft-deleted-agent")

    deleted = await client.delete(f"{AGENTS_URL}/{agent['id']}", headers=admin_auth)
    assert deleted.status_code == 204, deleted.text

    patched = await client.patch(
        f"{ADMIN_URL}/{agent['id']}", json={"name": "软删后改名"}, headers=admin_auth
    )
    assert patched.status_code == 404, patched.text
    assert patched.json()["error"]["code"] == "not_found"


async def test_non_admin_forbidden_and_unauthorized(
    client: httpx.AsyncClient, user: User, admin_auth: dict[str, str]
) -> None:
    """普通用户 GET/PATCH 403（带 required_role），未登录 401。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="guard-agent")
    headers = auth_headers(create_access_token(user.id).token)
    cases = [
        ("GET", ADMIN_URL, None),
        ("PATCH", f"{ADMIN_URL}/{agent['id']}", {"name": "越权改名"}),
    ]
    for method, path, payload in cases:
        response = await client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, response.text)
        error = response.json()["error"]
        assert error["code"] == "forbidden"
        assert error["details"] == {"required_role": "admin"}

    for method, path, payload in cases:
        response = await client.request(method, path, json=payload)
        assert response.status_code == 401, (method, response.text)
        assert response.json()["error"]["code"] == "unauthorized"


async def test_auth_hint_cleared_when_auth_or_headers_removed(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """清空 auth（none/oauth2）或 headers 时 auth_hint 必须同步清空，不能残留旧凭据提示。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="hint-agent")
    url = f"{ADMIN_URL}/{agent['id']}"

    with_auth = await client.patch(
        url,
        json={"auth": {"type": "api_key", "secret_ref": "secret:HINT_KEY"}},
        headers=admin_auth,
    )
    assert with_auth.status_code == 200, with_auth.text
    assert with_auth.json()["auth_type"] == "api_key"
    assert with_auth.json()["auth_hint"] == "secret:HINT_KEY"

    cleared_auth = await client.patch(
        url, json={"auth": {"type": "none"}}, headers=admin_auth
    )
    assert cleared_auth.status_code == 200, cleared_auth.text
    assert cleared_auth.json()["auth_type"] == "none"
    assert cleared_auth.json()["auth_hint"] == ""

    with_headers = await client.patch(
        url,
        json={"headers": {"Authorization": "env:HINT_TOKEN"}},
        headers=admin_auth,
    )
    assert with_headers.status_code == 200, with_headers.text
    assert "env:HINT_TOKEN" in with_headers.json()["auth_hint"]

    cleared_headers = await client.patch(url, json={"headers": {}}, headers=admin_auth)
    assert cleared_headers.status_code == 200, cleared_headers.text
    assert cleared_headers.json()["auth_hint"] == ""

    oauth = await client.patch(
        url,
        json={"auth": {"type": "api_key", "secret_ref": "secret:HINT_KEY"}},
        headers=admin_auth,
    )
    assert oauth.json()["auth_hint"] == "secret:HINT_KEY"
    cleared_oauth = await client.patch(
        url, json={"auth": {"type": "oauth2"}}, headers=admin_auth
    )
    assert cleared_oauth.status_code == 200, cleared_oauth.text
    assert cleared_oauth.json()["auth_type"] == "oauth2"
    assert cleared_oauth.json()["auth_hint"] == ""


async def test_plaintext_sensitive_header_requires_secret_store(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 CAPABILITY_SECRET_KEY 时敏感头明文 PATCH 返回 503 专用码，且不回显明文。"""
    _register_fake()
    agent = await _create_agent(client, admin_auth, slug="plain-header-agent")

    monkeypatch.delenv("CAPABILITY_SECRET_KEY", raising=False)
    monkeypatch.setattr(
        "app.core.ssrf.get_settings",
        lambda: Settings(capability_secret_key="", _env_file=None),
    )
    reset_settings_cache()
    try:
        response = await client.patch(
            f"{ADMIN_URL}/{agent['id']}",
            json={"headers": {"Authorization": "Bearer plain-agent-secret"}},
            headers=admin_auth,
        )
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "secret_store_unavailable"
        assert "plain-agent-secret" not in response.text
    finally:
        reset_settings_cache()

