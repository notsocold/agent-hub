"""平台密钥管理接口：CRUD、不塌陷明文、审计、权限与无密钥降级。"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import reset_settings_cache
from app.core.security import create_access_token
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.secrets import service as secrets_service
from app.modules.secrets.schemas import SecretCreateRequest
from tests.conftest import auth_headers


async def test_admin_crud_and_no_plaintext(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    created = await client.post(
        "/api/v1/admin/secrets",
        json={"name": "DATA_AGENT_KEY", "value": "super-secret-value", "description": "远程代理"},
        headers=admin_auth,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "DATA_AGENT_KEY"
    assert "value" not in body and "ciphertext" not in body
    assert "super-secret-value" not in created.text

    listed = await client.get("/api/v1/admin/secrets", headers=admin_auth)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["name"] == "DATA_AGENT_KEY"
    assert "super-secret-value" not in listed.text

    patched = await client.patch(
        "/api/v1/admin/secrets/DATA_AGENT_KEY",
        json={"value": "rotated-value", "enabled": False},
        headers=admin_auth,
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    async with get_session_factory()() as session:
        assert await secrets_service.resolve_platform_secret(session, "DATA_AGENT_KEY") is None

    deleted = await client.delete("/api/v1/admin/secrets/DATA_AGENT_KEY", headers=admin_auth)
    assert deleted.status_code == 204

    audit = await client.get(
        "/api/v1/admin/audit-logs?action=secret.create", headers=admin_auth
    )
    assert audit.status_code == 200
    assert audit.json()["items"][0]["detail"]["name"] == "DATA_AGENT_KEY"
    assert "super-secret-value" not in audit.text


async def test_duplicate_name_conflict_and_missing(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    payload = {"name": "K1", "value": "v1"}
    assert (await client.post("/api/v1/admin/secrets", json=payload, headers=admin_auth)).status_code == 201
    duplicate = await client.post(
        "/api/v1/admin/secrets", json={"name": "K1", "value": "v2"}, headers=admin_auth
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "secret_exists"
    missing = await client.patch(
        "/api/v1/admin/secrets/NOPE", json={"enabled": True}, headers=admin_auth
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "secret_not_found"


async def test_normal_user_forbidden(client: httpx.AsyncClient, user: User) -> None:
    headers = auth_headers(create_access_token(user.id).token)
    response = await client.get("/api/v1/admin/secrets", headers=headers)
    assert response.status_code == 403


async def test_missing_capability_key_503(
    client: httpx.AsyncClient, admin_auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAPABILITY_SECRET_KEY", raising=False)
    reset_settings_cache()
    try:
        response = await client.post(
            "/api/v1/admin/secrets", json={"name": "K2", "value": "v"}, headers=admin_auth
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "secret_store_unavailable"
    finally:
        reset_settings_cache()


async def test_resolve_platform_secret(db, admin: User) -> None:
    async with get_session_factory()() as session:
        await secrets_service.create_secret(
            session, admin, SecretCreateRequest(name="RESOLVE_ME", value="plain-value")
        )
        assert await secrets_service.resolve_platform_secret(session, "RESOLVE_ME") == "plain-value"


MCP_URL = "https://mcp.example.com/mcp"


async def _register_header_capture(tools: list[dict], seen: dict) -> None:
    import json

    from httpx import MockTransport

    from app.modules.mcp.service import register_test_transport
    from tests.helpers import make_mcp_transport

    transport = make_mcp_transport(tools)
    original = transport.handle_async_request

    async def handler(request):
        try:
            payload = json.loads(request.content)
        except (ValueError, TypeError):
            payload = {}
        if payload.get("method") == "initialize":
            seen["authorization"] = request.headers.get("authorization")
        return await original(request)

    register_test_transport(MCP_URL, MockTransport(handler))


async def test_admin_secret_ref_resolved_for_mcp(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    from tests.helpers import MOCK_TOOL_READ

    created_secret = await client.post(
        "/api/v1/admin/secrets",
        json={"name": "MCP_KEY", "value": "resolved-value"},
        headers=admin_auth,
    )
    assert created_secret.status_code == 201
    seen: dict = {}
    await _register_header_capture([MOCK_TOOL_READ], seen)

    created = await client.post(
        "/api/v1/mcp/servers",
        json={
            "name": "密钥 MCP",
            "slug": "secret-mcp",
            "transport": "streamable_http",
            "url": MCP_URL,
            "headers": {"Authorization": "secret:MCP_KEY"},
        },
        headers=admin_auth,
    )
    assert created.status_code == 201, created.text
    assert "resolved-value" not in created.text

    checked = await client.post(
        f"/api/v1/mcp/servers/{created.json()['id']}/check", headers=admin_auth
    )
    assert checked.status_code == 200
    assert seen["authorization"] == "resolved-value"


async def test_normal_user_cannot_use_secret_ref(
    client: httpx.AsyncClient, user: User
) -> None:
    headers = auth_headers(create_access_token(user.id).token)
    response = await client.post(
        "/api/v1/mcp/servers",
        json={
            "name": "越权 MCP",
            "slug": "bad-secret-mcp",
            "transport": "streamable_http",
            "url": MCP_URL,
            "headers": {"Authorization": "secret:MCP_KEY"},
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "secret_ref_not_allowed"
