"""M9 工作流 CRUD 集成测试：创建/列表游标分页/详情/PATCH 版本/启停/硬删与越权隐藏。

覆盖任务书测试清单：创建 201 落库字段、steps 非法 422、name 规则 422、
列表可见性（系统+本人，不含他人）、他人读写一律 404（不泄露存在性）、
系统模板只读、PATCH 版本自增、启停不改版本、硬删后不可复用 id、未登录 401、
审计动作序列（detail 只放字段名/布尔值）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db.models.audit import AuditLog
from app.db.models.workflow import Workflow
from app.db.session import get_session_factory
from tests.conftest import auth_headers, register_user

# 合法三步模板（同 spike：检索 → a2a → 强制审批），供创建/列表用例复用。
THREE_STEPS: list[dict[str, Any]] = [
    {
        "id": "retrieve",
        "type": "knowledge_search",
        "title": "检索资料",
        "instruction": "检索知识库相关资料",
    },
    {"id": "ask", "type": "a2a", "title": "询问专家", "instruction": "向远程专家确认结论"},
    {"id": "gate", "type": "approval", "title": "人工确认", "config": {"tools": ["send_email"]}},
]

# 单步合法模板（系统模板与 name/description 校验用例够用）。
SIMPLE_STEPS: list[dict[str, Any]] = [{"id": "s1", "type": "knowledge_search"}]


async def _create_via_api(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "  发版工作流  ",
    description: str = "M9 测试模板",
    steps: list[dict[str, Any]] | None = None,
) -> dict:
    """通过 API 创建一个用户模板并断言 201，返回响应 JSON。"""
    response = await client.post(
        "/api/v1/workflows",
        json={"name": name, "description": description, "steps": steps or THREE_STEPS},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_system_workflow(*, name: str = "系统发布模板") -> Workflow:
    """直接写库插一行系统模板（v1 系统模板由 DB/脚本创建，无 CRUD 入口）。"""
    async with get_session_factory()() as session:
        row = Workflow(
            scope="system",
            owner_user_id=None,
            name=name,
            description="内置系统模板",
            steps=SIMPLE_STEPS,
            enabled=True,
            version=1,
        )
        session.add(row)
        await session.commit()
        return row


async def test_create_workflow_persists_fields(client: httpx.AsyncClient) -> None:
    """创建 201：scope=user、version=1、enabled=True，name strip 落库，字段与库一致。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await _create_via_api(client, headers)

    assert created["scope"] == "user"
    assert created["version"] == 1
    assert created["enabled"] is True
    assert created["name"] == "发版工作流"  # strip 后落库
    assert created["description"] == "M9 测试模板"
    assert created["steps"] == THREE_STEPS
    assert created["created_at"] and created["updated_at"]

    async with get_session_factory()() as session:
        row = await session.get(Workflow, uuid.UUID(created["id"]))
        assert row is not None
        assert row.owner_user_id == uuid.UUID(registered["user"]["id"])
        assert row.scope == "user"
        assert row.name == "发版工作流"
        assert row.description == "M9 测试模板"
        assert row.steps == THREE_STEPS
        assert row.version == 1
        assert row.enabled is True


async def test_create_rejects_invalid_steps(client: httpx.AsyncClient) -> None:
    """steps 非法 → 422 ``workflow_steps_invalid``（空数组/未知 type/tool 缺 tools）。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    empty = await client.post(
        "/api/v1/workflows", json={"name": "空步骤", "steps": []}, headers=headers
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "workflow_steps_invalid"
    assert "steps 不能为空数组" in empty.json()["error"]["message"]

    bogus_type = await client.post(
        "/api/v1/workflows",
        json={"name": "未知类型", "steps": [{"id": "s1", "type": "bogus"}]},
        headers=headers,
    )
    assert bogus_type.status_code == 422
    assert bogus_type.json()["error"]["code"] == "workflow_steps_invalid"

    missing_tools = await client.post(
        "/api/v1/workflows",
        json={"name": "缺工具清单", "steps": [{"id": "s1", "type": "tool"}]},
        headers=headers,
    )
    assert missing_tools.status_code == 422
    assert missing_tools.json()["error"]["code"] == "workflow_steps_invalid"


async def test_create_rejects_invalid_name_and_description(client: httpx.AsyncClient) -> None:
    """name strip 后 1–100 字符、不含双引号/换行；description strip 后 ≤500，违规一律 422。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    for bad_name in ("   ", "", '含"双引号', "含\n换行", "含\r回车", "x" * 101):
        response = await client.post(
            "/api/v1/workflows",
            json={"name": bad_name, "steps": SIMPLE_STEPS},
            headers=headers,
        )
        assert response.status_code == 422, (bad_name, response.text)

    too_long_desc = await client.post(
        "/api/v1/workflows",
        json={"name": "合法名", "description": "长" * 501, "steps": SIMPLE_STEPS},
        headers=headers,
    )
    assert too_long_desc.status_code == 422


async def test_list_shows_system_and_own_not_others(client: httpx.AsyncClient) -> None:
    """列表 = 系统模板 + 本人模板；他人模板不可见。"""
    system_row = await _insert_system_workflow(name="系统发布模板")
    mine = await register_user(client)
    headers = auth_headers(mine["access_token"])
    own = await _create_via_api(client, headers, name="本人模板")

    other = await register_user(client)
    other_headers = auth_headers(other["access_token"])
    foreign = await _create_via_api(client, other_headers, name="他人模板")

    listing = await client.get("/api/v1/workflows", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    ids = {item["id"] for item in body["items"]}
    assert str(system_row.id) in ids
    assert own["id"] in ids
    assert foreign["id"] not in ids
    assert all(item["scope"] in {"system", "user"} for item in body["items"])


async def test_list_cursor_pagination_round_trip(client: httpx.AsyncClient) -> None:
    """limit=1 逐页翻取：has_more/next_cursor 正确，翻页结果与全量一致、不重不漏。"""
    mine = await register_user(client)
    headers = auth_headers(mine["access_token"])
    created_ids = {
        (await _create_via_api(client, headers, name=f"翻页模板 {index}"))["id"]
        for index in range(3)
    }

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/v1/workflows", params=params, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 1
        seen.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            break
        assert body["next_cursor"]
        cursor = body["next_cursor"]
    else:  # pragma: no cover - 防御死循环
        pytest.fail("翻页未在预期页数内结束")

    assert set(seen) == created_ids
    assert len(seen) == len(created_ids)


async def test_foreign_workflow_read_and_write_hidden_as_404(client: httpx.AsyncClient) -> None:
    """他人 user 模板的读与写（GET/PATCH/PUT enabled/DELETE）一律 404，与不存在同响应。"""
    owner = await register_user(client)
    owner_headers = auth_headers(owner["access_token"])
    foreign = await _create_via_api(client, owner_headers, name="他人私有模板")
    workflow_id = foreign["id"]

    attacker = await register_user(client)
    attacker_headers = auth_headers(attacker["access_token"])

    detail = await client.get(f"/api/v1/workflows/{workflow_id}", headers=attacker_headers)
    assert detail.status_code == 404
    patched = await client.patch(
        f"/api/v1/workflows/{workflow_id}", json={"name": "篡改"}, headers=attacker_headers
    )
    assert patched.status_code == 404
    enabled = await client.put(
        f"/api/v1/workflows/{workflow_id}/enabled",
        json={"enabled": False},
        headers=attacker_headers,
    )
    assert enabled.status_code == 404
    deleted = await client.delete(f"/api/v1/workflows/{workflow_id}", headers=attacker_headers)
    assert deleted.status_code == 404

    # 越权响应与真正不存在的资源同形（404 not_found 信封），不泄露存在性。
    missing = await client.get(f"/api/v1/workflows/{uuid.uuid4()}", headers=attacker_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert detail.json()["error"]["code"] == missing.json()["error"]["code"]


async def test_system_workflow_readable_but_readonly(client: httpx.AsyncClient) -> None:
    """系统模板人人可读；任何人不可改/启停/删（越权面一律 404）。"""
    system_row = await _insert_system_workflow(name="系统只读模板")
    headers = auth_headers((await register_user(client))["access_token"])
    workflow_id = str(system_row.id)

    detail = await client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["scope"] == "system"
    assert detail.json()["name"] == "系统只读模板"

    patched = await client.patch(
        f"/api/v1/workflows/{workflow_id}", json={"name": "改名"}, headers=headers
    )
    assert patched.status_code == 404
    enabled = await client.put(
        f"/api/v1/workflows/{workflow_id}/enabled", json={"enabled": False}, headers=headers
    )
    assert enabled.status_code == 404
    deleted = await client.delete(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert deleted.status_code == 404


async def test_patch_own_success_increments_version(client: httpx.AsyncClient) -> None:
    """PATCH 本人模板成功必 version+1（含仅改 name/description 与仅改 steps）。"""
    headers = auth_headers((await register_user(client))["access_token"])
    created = await _create_via_api(client, headers)

    renamed = await client.patch(
        f"/api/v1/workflows/{created['id']}",
        json={"name": "改名后的工作流", "description": "新描述"},
        headers=headers,
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["name"] == "改名后的工作流"
    assert body["description"] == "新描述"
    assert body["version"] == 2
    assert body["steps"] == THREE_STEPS  # steps 未提交则不变

    steps_updated = await client.patch(
        f"/api/v1/workflows/{created['id']}", json={"steps": SIMPLE_STEPS}, headers=headers
    )
    assert steps_updated.status_code == 200, steps_updated.text
    assert steps_updated.json()["steps"] == SIMPLE_STEPS
    assert steps_updated.json()["version"] == 3

    # PATCH 内嵌 name 规则同样生效（strip + 字符黑名单）
    blank = await client.patch(
        f"/api/v1/workflows/{created['id']}", json={"name": "   "}, headers=headers
    )
    assert blank.status_code == 422


async def test_patch_invalid_steps_rejected(client: httpx.AsyncClient) -> None:
    """PATCH steps 非法 → 422 ``workflow_steps_invalid``，且失败不落库（version 不变）。"""
    headers = auth_headers((await register_user(client))["access_token"])
    created = await _create_via_api(client, headers)

    response = await client.patch(
        f"/api/v1/workflows/{created['id']}", json={"steps": []}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workflow_steps_invalid"

    detail = await client.get(f"/api/v1/workflows/{created['id']}", headers=headers)
    assert detail.json()["version"] == 1
    assert detail.json()["steps"] == THREE_STEPS


async def test_put_enabled_keeps_version(client: httpx.AsyncClient) -> None:
    """PUT enabled 仅本人可用：启停成功且 version 不变。"""
    headers = auth_headers((await register_user(client))["access_token"])
    created = await _create_via_api(client, headers)

    disabled = await client.put(
        f"/api/v1/workflows/{created['id']}/enabled", json={"enabled": False}, headers=headers
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["version"] == 1  # 启停不改版本

    enabled = await client.put(
        f"/api/v1/workflows/{created['id']}/enabled", json={"enabled": True}, headers=headers
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["version"] == 1


async def test_delete_then_get_404_and_id_not_reusable(client: httpx.AsyncClient) -> None:
    """DELETE 硬删：204 → GET 404；再次创建生成新 UUID，同 id 不可复用。"""
    headers = auth_headers((await register_user(client))["access_token"])
    created = await _create_via_api(client, headers)
    workflow_id = created["id"]

    deleted = await client.delete(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert deleted.status_code == 204
    assert deleted.content == b""

    gone = await client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert gone.status_code == 404

    recreated = await _create_via_api(client, headers, name="重建模板")
    assert recreated["id"] != workflow_id


async def test_unauthenticated_requests_401(client: httpx.AsyncClient) -> None:
    """未登录访问全部端点 → 401。"""
    create = await client.post(
        "/api/v1/workflows", json={"name": "匿名模板", "steps": SIMPLE_STEPS}
    )
    assert create.status_code == 401
    assert (await client.get("/api/v1/workflows")).status_code == 401
    assert (await client.get(f"/api/v1/workflows/{uuid.uuid4()}")).status_code == 401
    assert (
        await client.patch(f"/api/v1/workflows/{uuid.uuid4()}", json={"name": "匿名"})
    ).status_code == 401
    assert (
        await client.put(f"/api/v1/workflows/{uuid.uuid4()}/enabled", json={"enabled": True})
    ).status_code == 401
    assert (await client.delete(f"/api/v1/workflows/{uuid.uuid4()}")).status_code == 401


async def test_audit_actions_written(client: httpx.AsyncClient) -> None:
    """写操作审计：action 序列、resource_id=workflow_id、detail 只放字段名/布尔值。"""
    headers = auth_headers((await register_user(client))["access_token"])
    created = await _create_via_api(client, headers)
    workflow_id = created["id"]

    await client.patch(
        f"/api/v1/workflows/{workflow_id}", json={"name": "审计模板"}, headers=headers
    )
    await client.put(
        f"/api/v1/workflows/{workflow_id}/enabled", json={"enabled": False}, headers=headers
    )
    await client.delete(f"/api/v1/workflows/{workflow_id}", headers=headers)

    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.resource_type == "workflow")
                    .order_by(AuditLog.id)
                )
            ).scalars()
        )

    assert [row.action for row in rows] == [
        "workflow.created",
        "workflow.updated",
        "workflow.enabled",
        "workflow.deleted",
    ]
    assert all(row.resource_id == workflow_id for row in rows)
    assert all(row.actor_type == "user" for row in rows)

    # detail 只放字段名/布尔值：不含 name 正文、description/steps 正文。
    by_action = {row.action: row for row in rows}
    assert sorted(by_action["workflow.created"].detail["fields"]) == [
        "description",
        "name",
        "steps",
    ]
    assert by_action["workflow.updated"].detail["fields"] == ["name"]
    assert by_action["workflow.enabled"].detail == {"enabled": False}
    serialized = json.dumps(rows[0].detail, ensure_ascii=False)
    assert "发版工作流" not in serialized
    assert "M9 测试模板" not in serialized
