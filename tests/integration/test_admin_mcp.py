"""管理台系统 MCP（M8 T5）：增删改查、工具发现、密钥不回显与 403。

覆盖契约：
- ``GET/POST /api/v1/admin/mcp/servers`` 与
  ``PATCH/DELETE /api/v1/admin/mcp/servers/{id}`` 仅管理员可用；
- 创建复用用户侧的 SSRF / ``secret:`` / 工具发现语义：可达则 healthy 并同步工具，
  不可达仍 201 且 unhealthy；
- slug 在**任意 scope** 冲突都返回 409；系统项的 ``effective_enabled`` 为全局口径
  ``enabled and not force_disabled``；
- 响应与审计永不回显明文或密文，只保留 ``credentials_hint``/``secret_ref``；
- 删除清理 ``mcp_tools``（FK CASCADE）与用户覆盖；
- 管理端属于配置面：``MCP_ENABLED=false`` 时仍可读写（R1）。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings, reset_settings_cache
from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.mcp import McpServer, McpTool, UserCapabilityOverride
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.mcp.service import register_test_transport
from tests.conftest import auth_headers, register_user
from tests.helpers import MOCK_TOOL_READ, MOCK_TOOL_WRITE, make_mcp_transport

ADMIN_URL = "/api/v1/admin/mcp/servers"
USER_URL = "/api/v1/mcp/servers"
MCP_URL = "https://mcp.example.com/mcp"
MCP_URL_2 = "https://mcp.example.com/mcp2"


def _register_mock(url: str, tools: list[dict]) -> None:
    register_test_transport(url, make_mcp_transport(tools))


def _payload(slug: str, **overrides) -> dict:
    data = {
        "name": "系统 MCP",
        "slug": slug,
        "transport": "streamable_http",
        "url": MCP_URL,
    }
    data.update(overrides)
    return data


async def _create_system(
    client: httpx.AsyncClient, headers: dict[str, str], slug: str = "system-mcp", **overrides
) -> dict:
    response = await client.post(ADMIN_URL, json=_payload(slug, **overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _user_item(client: httpx.AsyncClient, headers: dict[str, str], server_id: str) -> dict:
    response = await client.get(USER_URL, headers=headers)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["items"] if item["id"] == server_id)


async def _audit_rows(action: str) -> list[AuditLog]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
                )
            ).scalars()
        )


async def test_create_list_and_tool_discovery(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """创建后立即发现工具；管理端列表只含系统项，普通用户也能看到。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ, MOCK_TOOL_WRITE])

    server = await _create_system(client, admin_auth, slug="discovery-mcp")
    assert server["scope"] == "system"
    assert server["slug"] == "discovery-mcp"
    assert server["enabled"] is True
    assert server["force_disabled"] is False
    assert server["effective_enabled"] is True
    assert server["health_status"] == "healthy"
    assert server["tool_count"] == 2
    assert server["last_error"] is None

    listing = await client.get(ADMIN_URL, headers=admin_auth)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert [item["id"] for item in body["items"]] == [server["id"]]
    assert body["stats"] == {
        "total": 1,
        "system": 1,
        "user": 0,
        "enabled": 1,
        "healthy": 1,
    }

    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    user_item = await _user_item(client, user_headers, server["id"])
    assert user_item["scope"] == "system"
    assert user_item["effective_enabled"] is True


async def test_create_unreachable_server_keeps_config(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """工具发现失败仍创建成功，状态写为 unhealthy 且保留错误。"""
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    register_test_transport(MCP_URL, httpx.MockTransport(failing))
    server = await _create_system(client, admin_auth, slug="unreachable-mcp")
    assert server["health_status"] == "unhealthy"
    assert server["last_error"]
    assert server["tool_count"] == 0


async def test_system_mcp_secret_ref_never_echoed(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """``secret:`` 引用只落引用；响应/审计都不出现明文与密文。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    created_secret = await client.post(
        "/api/v1/admin/secrets",
        json={"name": "MCP_KEY", "value": "s3cr3t-value"},
        headers=admin_auth,
    )
    assert created_secret.status_code == 201, created_secret.text

    response = await client.post(
        ADMIN_URL,
        json=_payload("secret-mcp", headers={"Authorization": "secret:MCP_KEY"}),
        headers=admin_auth,
    )
    assert response.status_code == 201, response.text
    assert "s3cr3t-value" not in response.text
    body = response.json()
    assert "headers" not in body
    assert "ciphertext" not in response.text
    assert "secret:MCP_KEY" in body["credentials_hint"]

    async with get_session_factory()() as session:
        row = await session.get(McpServer, uuid.UUID(body["id"]))
        assert row is not None
        assert row.headers == {"Authorization": {"secret_ref": "secret:MCP_KEY"}}
        assert row.credentials_hint == "Authorization: secret:MCP_KEY"

    for action in ("mcp.server.create", "mcp.server.check"):
        for audit in await _audit_rows(action):
            text = json.dumps(audit.detail, ensure_ascii=False)
            assert "s3cr3t-value" not in text
            assert "ciphertext" not in text
            assert "headers" not in audit.detail


async def test_ssrf_duplicate_and_validation(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """SSRF/缺省 url 422；同 scope 与跨 scope 的 slug 冲突都是 409。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])

    ssrf = await client.post(
        ADMIN_URL,
        json=_payload("ssrf-mcp", url="http://169.254.169.254/mcp"),
        headers=admin_auth,
    )
    assert ssrf.status_code == 422, ssrf.text
    assert ssrf.json()["error"]["code"] == "mcp_ssrf_blocked"

    # 纯空白 name 不能通过 min_length 后落成空串。
    blank_name = await client.post(
        ADMIN_URL,
        json=_payload("blank-name-mcp", name="   "),
        headers=admin_auth,
    )
    assert blank_name.status_code == 422, blank_name.text
    assert blank_name.json()["error"]["code"] == "validation_error"
    assert (await client.get(ADMIN_URL, headers=admin_auth)).json()["items"] == []

    await _create_system(client, admin_auth, slug="dup-mcp")
    duplicate = await client.post(ADMIN_URL, json=_payload("dup-mcp"), headers=admin_auth)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "conflict"

    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    user_created = await client.post(
        USER_URL,
        json={
            "name": "用户 MCP",
            "slug": "shared-slug",
            "transport": "streamable_http",
            "url": MCP_URL,
        },
        headers=user_headers,
    )
    assert user_created.status_code == 201, user_created.text
    cross_scope = await client.post(
        ADMIN_URL, json=_payload("shared-slug"), headers=admin_auth
    )
    assert cross_scope.status_code == 409, cross_scope.text
    assert cross_scope.json()["error"]["code"] == "conflict"

    missing_url = await client.post(
        ADMIN_URL,
        json={"name": "缺 URL", "slug": "no-url", "transport": "streamable_http"},
        headers=admin_auth,
    )
    assert missing_url.status_code == 422, missing_url.text
    assert missing_url.json()["error"]["code"] == "validation_error"


async def test_patch_fields_force_disable_and_discovery(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """PATCH 修改白名单字段；force_disabled 覆盖用户覆盖；改 URL 重新发现工具。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ, MOCK_TOOL_WRITE])
    _register_mock(MCP_URL_2, [MOCK_TOOL_READ])
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    server = await _create_system(client, admin_auth, slug="patch-mcp")

    # 用户先写覆盖；强制停用后覆盖失效。
    override = await client.put(
        f"{USER_URL}/{server['id']}/enabled", json={"enabled": True}, headers=user_headers
    )
    assert override.status_code == 200, override.text
    forced = await client.patch(
        f"{ADMIN_URL}/{server['id']}", json={"force_disabled": True}, headers=admin_auth
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["force_disabled"] is True
    assert forced.json()["effective_enabled"] is False
    assert (await _user_item(client, user_headers, server["id"]))["effective_enabled"] is False

    restored = await client.patch(
        f"{ADMIN_URL}/{server['id']}",
        json={"force_disabled": False, "enabled": True},
        headers=admin_auth,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["effective_enabled"] is True

    edited = await client.patch(
        f"{ADMIN_URL}/{server['id']}",
        json={
            "name": "管理台改名 MCP",
            "description": "管理台描述",
            "headers": {"X-Trace": "plain"},
            "auto_inject": False,
        },
        headers=admin_auth,
    )
    assert edited.status_code == 200, edited.text
    edited_body = edited.json()
    assert edited_body["name"] == "管理台改名 MCP"
    assert edited_body["description"] == "管理台描述"
    assert edited_body["auto_inject"] is False
    assert edited_body["tool_count"] == 2

    moved = await client.patch(
        f"{ADMIN_URL}/{server['id']}", json={"url": MCP_URL_2}, headers=admin_auth
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["tool_count"] == 1
    assert moved.json()["health_status"] == "healthy"

    empty = await client.patch(f"{ADMIN_URL}/{server['id']}", json={}, headers=admin_auth)
    assert empty.status_code == 422, empty.text
    assert empty.json()["error"]["code"] == "validation_error"

    blank_name = await client.patch(
        f"{ADMIN_URL}/{server['id']}", json={"name": "   "}, headers=admin_auth
    )
    assert blank_name.status_code == 422, blank_name.text
    assert blank_name.json()["error"]["code"] == "validation_error"

    # slug/transport/command/args 不可编辑，显式 422 而不是静默忽略。
    for field, value in (
        ("slug", "renamed-slug"),
        ("transport", "sse"),
        ("command", "cat"),
        ("args", ["--x"]),
    ):
        rejected = await client.patch(
            f"{ADMIN_URL}/{server['id']}", json={field: value}, headers=admin_auth
        )
        assert rejected.status_code == 422, (field, rejected.text)
        assert rejected.json()["error"]["code"] == "validation_error"


async def test_delete_cleans_tools_and_overrides(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """删除清理工具与用户覆盖；再次删除 404。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    server = await _create_system(client, admin_auth, slug="delete-mcp")
    await client.put(
        f"{USER_URL}/{server['id']}/enabled", json={"enabled": True}, headers=user_headers
    )

    deleted = await client.delete(f"{ADMIN_URL}/{server['id']}", headers=admin_auth)
    assert deleted.status_code == 204, deleted.text

    user_listing = await client.get(USER_URL, headers=user_headers)
    assert server["id"] not in {item["id"] for item in user_listing.json()["items"]}

    async with get_session_factory()() as session:
        tools = list(
            (
                await session.execute(
                    select(McpTool).where(McpTool.server_id == uuid.UUID(server["id"]))
                )
            ).scalars()
        )
        overrides = list(
            (
                await session.execute(
                    select(UserCapabilityOverride).where(
                        UserCapabilityOverride.target_id == uuid.UUID(server["id"])
                    )
                )
            ).scalars()
        )
        assert tools == []
        assert overrides == []

    again = await client.delete(f"{ADMIN_URL}/{server['id']}", headers=admin_auth)
    assert again.status_code == 404, again.text
    assert again.json()["error"]["code"] == "not_found"


async def test_non_admin_forbidden_and_unauthorized(
    client: httpx.AsyncClient, user: User, admin_auth: dict[str, str]
) -> None:
    """4 个方法对普通用户 403（带 required_role），无 token 401。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    server = await _create_system(client, admin_auth, slug="guard-mcp")
    headers = auth_headers(create_access_token(user.id).token)
    cases = [
        ("GET", ADMIN_URL, None),
        (
            "POST",
            ADMIN_URL,
            _payload("forbidden-create"),
        ),
        ("PATCH", f"{ADMIN_URL}/{server['id']}", {"name": "越权改名"}),
        ("DELETE", f"{ADMIN_URL}/{server['id']}", None),
    ]
    for method, path, payload in cases:
        response = await client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, (method, path, response.text)
        error = response.json()["error"]
        assert error["code"] == "forbidden"
        assert error["details"] == {"required_role": "admin"}

    for method, path, payload in cases:
        response = await client.request(method, path, json=payload)
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["error"]["code"] == "unauthorized"


async def test_audit(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    """create/update/delete 写稳定 action，detail 只含 slug/字段名/scope。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    server = await _create_system(client, admin_auth, slug="audit-mcp")
    await client.patch(
        f"{ADMIN_URL}/{server['id']}", json={"name": "审计改名"}, headers=admin_auth
    )
    await client.delete(f"{ADMIN_URL}/{server['id']}", headers=admin_auth)

    created = await _audit_rows("mcp.server.create")
    assert len(created) == 1
    create_row = created[0]
    assert create_row.actor_type == "user"
    assert create_row.actor_id == str(admin.id)
    assert create_row.resource_type == "mcp_server"
    assert create_row.resource_id == server["id"]
    assert create_row.detail == {
        "slug": "audit-mcp",
        "transport": "streamable_http",
        "url": MCP_URL,
        "scope": "system",
    }

    updated = await _audit_rows("mcp.server.update")
    assert len(updated) == 1
    update_detail = updated[0].detail
    assert update_detail == {"fields": ["name"], "scope": "system"}
    assert "headers" not in update_detail and "credentials" not in update_detail

    deleted = await _audit_rows("mcp.server.delete")
    assert len(deleted) == 1
    assert deleted[0].detail == {"slug": "audit-mcp", "scope": "system"}
    assert deleted[0].resource_id == server["id"]


async def test_admin_mcp_scope_and_not_found_boundaries(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """管理端看不到也改不到用户自建 MCP；随机 id 的 PATCH/DELETE 均为 404。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    user_created = await client.post(
        USER_URL,
        json={
            "name": "用户自建 MCP",
            "slug": "user-only-mcp",
            "transport": "streamable_http",
            "url": MCP_URL,
        },
        headers=user_headers,
    )
    assert user_created.status_code == 201, user_created.text
    user_server_id = user_created.json()["id"]

    listing = await client.get(ADMIN_URL, headers=admin_auth)
    assert listing.status_code == 200, listing.text
    assert user_server_id not in {item["id"] for item in listing.json()["items"]}

    missing_id = str(uuid.uuid4())
    for server_id in (user_server_id, missing_id):
        patched = await client.patch(
            f"{ADMIN_URL}/{server_id}", json={"name": "越权改名"}, headers=admin_auth
        )
        assert patched.status_code == 404, patched.text
        assert patched.json()["error"]["code"] == "not_found"
        assert patched.json()["error"]["message"] == "系统 MCP 服务器不存在"
        deleted = await client.delete(f"{ADMIN_URL}/{server_id}", headers=admin_auth)
        assert deleted.status_code == 404, deleted.text
        assert deleted.json()["error"]["code"] == "not_found"


async def test_secret_store_unavailable_on_plaintext_sensitive_headers(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 CAPABILITY_SECRET_KEY 时，敏感头明文 POST/PATCH 都是 503 专用码。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    server = await _create_system(client, admin_auth, slug="secret-store-mcp")

    monkeypatch.delenv("CAPABILITY_SECRET_KEY", raising=False)
    # 本地 .env 可能配置了密钥；显式构造空密钥配置，保证本地与 CI 语义一致。
    monkeypatch.setattr(
        "app.core.ssrf.get_settings",
        lambda: Settings(
            capability_secret_key="",
            mcp_allowed_hosts="mcp.example.com",
            _env_file=None,
        ),
    )
    reset_settings_cache()
    try:
        created = await client.post(
            ADMIN_URL,
            json=_payload(
                "secret-store-mcp-2",
                headers={"Authorization": "Bearer plain-secret"},
            ),
            headers=admin_auth,
        )
        assert created.status_code == 503, created.text
        assert created.json()["error"]["code"] == "secret_store_unavailable"
        assert "plain-secret" not in created.text

        patched = await client.patch(
            f"{ADMIN_URL}/{server['id']}",
            json={"headers": {"Authorization": "Bearer plain-secret"}},
            headers=admin_auth,
        )
        assert patched.status_code == 503, patched.text
        assert patched.json()["error"]["code"] == "secret_store_unavailable"
        assert "plain-secret" not in patched.text
    finally:
        reset_settings_cache()


async def test_capability_disabled_does_not_block_admin(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1：MCP_ENABLED=false 时管理端仍可读写，运行态 ``/mcp`` 仍 403。"""
    monkeypatch.setattr(get_settings(), "mcp_enabled", False)
    _register_mock(MCP_URL, [MOCK_TOOL_READ])

    listed = await client.get(ADMIN_URL, headers=admin_auth)
    assert listed.status_code == 200, listed.text
    created = await _create_system(client, admin_auth, slug="switch-off-mcp")
    assert created["tool_count"] == 1

    runtime = await client.get(USER_URL, headers=admin_auth)
    assert runtime.status_code == 403, runtime.text
    assert runtime.json()["error"]["code"] == "capability_disabled"


async def test_patch_same_values_is_noop(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同值 PATCH 不写审计、不触发工具发现；真实变更才落库并 sync 一次。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    server = await _create_system(client, admin_auth, slug="noop-mcp")
    sync_calls: list[str] = []

    async def _spy(db, target, *, force: bool = False) -> None:
        sync_calls.append("sync")

    monkeypatch.setattr("app.modules.mcp.service._check_and_sync", _spy)
    assert await _audit_rows("mcp.server.update") == []

    same = await client.patch(
        f"{ADMIN_URL}/{server['id']}",
        json={
            "name": server["name"],
            "description": server["description"],
            "url": server["url"],
            "headers": {},
            "auto_inject": server["auto_inject"],
            "enabled": server["enabled"],
            "force_disabled": server["force_disabled"],
        },
        headers=admin_auth,
    )
    assert same.status_code == 200, same.text
    assert same.json()["name"] == server["name"]
    assert same.json()["tool_count"] == 1
    assert await _audit_rows("mcp.server.update") == []
    assert sync_calls == []

    changed = await client.patch(
        f"{ADMIN_URL}/{server['id']}", json={"name": "真实改名"}, headers=admin_auth
    )
    assert changed.status_code == 200, changed.text
    updated = await _audit_rows("mcp.server.update")
    assert len(updated) == 1
    assert updated[0].detail == {"fields": ["name"], "scope": "system"}
    assert sync_calls == ["sync"]


async def test_patch_url_ssrf_blocked(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """PATCH 修改 url 同样走 SSRF 校验；被拦截时不落库、不写审计。"""
    _register_mock(MCP_URL, [MOCK_TOOL_READ])
    server = await _create_system(client, admin_auth, slug="patch-ssrf-mcp")

    blocked = await client.patch(
        f"{ADMIN_URL}/{server['id']}",
        json={"url": "http://169.254.169.254/mcp"},
        headers=admin_auth,
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "mcp_ssrf_blocked"
    assert await _audit_rows("mcp.server.update") == []

    listing = await client.get(ADMIN_URL, headers=admin_auth)
    assert listing.status_code == 200, listing.text
    assert [item["url"] for item in listing.json()["items"]] == [MCP_URL]
