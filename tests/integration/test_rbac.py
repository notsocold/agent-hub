"""M7 RBAC 收紧：agents/tasks 写操作仅管理员，读与预览保持可用。

覆盖：
- 普通用户调用全部写端点 → 403 ``forbidden``，``details.required_role="admin"``；
- 拒绝写操作落审计 ``security.rbac_denied``（actor/method/path/required_role）；
- 管理员调用写端点不被 403 拦截，且至少一条真实成功链路（注册/PATCH/刷新/健康/删除 Agent）；
- 普通用户读列表、读任务、预览 Agent 仍可用；
- 无 token 的写请求 → 401。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.modules.a2a.client import register_test_transport
from tests.conftest import auth_headers, register_user
from tests.fixtures.fake_a2a_agent import FakeA2aAgent

AGENT_URL = "http://a2a.test"
# 合法但必然不存在的 UUID：写接口应进入依赖/业务层，而不是先被 422 拦截。
MISSING_ID = uuid.UUID("00000000-0000-4000-8000-000000000000")

_WRITE_CASES = [
    pytest.param("post", "/api/v1/agents", {"url": AGENT_URL}, id="agents-create"),
    pytest.param(
        "patch", f"/api/v1/agents/{MISSING_ID}", {"name": "改名"}, id="agents-patch"
    ),
    pytest.param("delete", f"/api/v1/agents/{MISSING_ID}", None, id="agents-delete"),
    pytest.param(
        "post", f"/api/v1/agents/{MISSING_ID}/refresh", None, id="agents-refresh"
    ),
    pytest.param(
        "post", f"/api/v1/agents/{MISSING_ID}/health", None, id="agents-health"
    ),
    pytest.param("post", f"/api/v1/tasks/{MISSING_ID}/cancel", None, id="tasks-cancel"),
    pytest.param(
        "post",
        f"/api/v1/tasks/{MISSING_ID}/resume",
        {"input": "继续"},
        id="tasks-resume",
    ),
    pytest.param(
        "post",
        f"/api/v1/tasks/{MISSING_ID}/authorize",
        {"scopes": ["repo:read"]},
        id="tasks-authorize",
    ),
    pytest.param(
        "post", f"/api/v1/tasks/{MISSING_ID}/reject", {"reason": "不授权"}, id="tasks-reject"
    ),
]


def _register_fake() -> FakeA2aAgent:
    fake = FakeA2aAgent(url=AGENT_URL)
    register_test_transport(AGENT_URL, httpx.ASGITransport(app=fake.app))
    return fake


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
) -> httpx.Response:
    return await client.request(method, path, headers=headers, json=payload)


@pytest.mark.parametrize(("method", "path", "payload"), _WRITE_CASES)
async def test_normal_user_write_is_forbidden(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict | None,
) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    response = await _request(client, method, path, headers=headers, payload=payload)

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "forbidden"
    assert error["message"] == "需要管理员权限"
    assert error["details"] == {"required_role": "admin"}


async def test_denied_write_is_audited(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    registered = await register_user(client)
    user_id = registered["user"]["id"]
    headers = auth_headers(registered["access_token"])

    denied = await client.post("/api/v1/agents", json={"url": AGENT_URL}, headers=headers)
    assert denied.status_code == 403

    logs = await client.get(
        "/api/v1/admin/audit-logs",
        params={"action": "security.rbac_denied"},
        headers=admin_auth,
    )
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]
    entry = next(item for item in items if item["actor_id"] == user_id)
    assert entry["actor_type"] == "user"
    assert entry["action"] == "security.rbac_denied"
    assert entry["resource_type"] == "endpoint"
    assert entry["resource_id"] == "/api/v1/agents"
    assert entry["detail"]["method"] == "POST"
    assert entry["detail"]["path"] == "/api/v1/agents"
    assert entry["detail"]["required_role"] == "admin"


async def test_admin_agent_write_success_path(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    _register_fake()

    created = await client.post(
        "/api/v1/agents",
        json={"url": AGENT_URL, "slug": "admin-agent"},
        headers=admin_auth,
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/agents/{agent_id}", json={"name": "管理员改名"}, headers=admin_auth
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "管理员改名"

    refreshed = await client.post(
        f"/api/v1/agents/{agent_id}/refresh", headers=admin_auth
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["agent"]["health_status"] == "healthy"

    health = await client.post(f"/api/v1/agents/{agent_id}/health", headers=admin_auth)
    assert health.status_code == 200, health.text
    assert health.json()["health_status"] == "healthy"

    deleted = await client.delete(f"/api/v1/agents/{agent_id}", headers=admin_auth)
    assert deleted.status_code == 204, deleted.text


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        pytest.param(f"/api/v1/tasks/{MISSING_ID}/cancel", None, id="tasks-cancel"),
        pytest.param(
            f"/api/v1/tasks/{MISSING_ID}/resume", {"input": "继续"}, id="tasks-resume"
        ),
        pytest.param(
            f"/api/v1/tasks/{MISSING_ID}/authorize", {}, id="tasks-authorize"
        ),
        pytest.param(
            f"/api/v1/tasks/{MISSING_ID}/reject", {"reason": "不授权"}, id="tasks-reject"
        ),
    ],
)
async def test_admin_task_action_not_forbidden(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    path: str,
    payload: dict | None,
) -> None:
    response = await client.post(path, json=payload, headers=admin_auth)

    # 管理员已通过门禁，随机 UUID 落到业务 404（不允许被 403 提前拦截）。
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


async def test_reads_and_preview_stay_available_to_normal_user(
    client: httpx.AsyncClient,
) -> None:
    _register_fake()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    agents = await client.get("/api/v1/agents", headers=headers)
    assert agents.status_code == 200, agents.text

    tasks = await client.get("/api/v1/tasks", headers=headers)
    assert tasks.status_code == 200, tasks.text

    preview = await client.post(
        "/api/v1/agents/preview", json={"url": AGENT_URL}, headers=headers
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["valid"] is True


async def test_write_without_token_is_unauthorized(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/agents", json={"url": AGENT_URL})

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"
