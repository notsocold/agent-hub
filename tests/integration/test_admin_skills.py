"""管理台系统技能（M8 T4）：列表、启停、强制停用、审计与能力开关。

覆盖契约：
- ``GET /api/v1/admin/skills?q=`` 只返回 ``scope='system'`` 的技能，按名称升序，
  ``q`` 转义 LIKE 通配符并最长 100；``stats`` 基于当前筛选结果；
- 管理端展示全局有效态 ``enabled and not force_disabled``，不合并用户覆盖；
- 启停/强制停用写 ``skill.enable``/``skill.force_disable`` 审计；
- 私有技能与不存在 id 都是 404 ``not_found``；
- 非管理员 403；能力总开关关闭不阻断管理端（R1），运行态仍 403。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.mcp import UserCapabilityOverride
from app.db.models.skill import Skill
from app.db.models.user import User
from app.db.session import get_session_factory
from tests.conftest import auth_headers, register_user

ADMIN_LIST_URL = "/api/v1/admin/skills"
SKILLS_URL = "/api/v1/skills"
CATALOG_URL = "/api/v1/skills/catalog"


async def _seed_system_skill(
    *,
    slug: str,
    name: str,
    enabled: bool = True,
    force_disabled: bool = False,
) -> uuid.UUID:
    """直接落库造系统技能（内置只有 weekly-report，用于排序/统计用例）。"""
    async with get_session_factory()() as session:
        row = Skill(
            scope="system",
            slug=slug,
            name=name,
            description="用于管理端排序与统计的测试系统技能",
            content="# 测试技能",
            source="manual",
            enabled=enabled,
            force_disabled=force_disabled,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _admin_item(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    """取内置 ``weekly-report`` 的管理端列表项。"""
    response = await client.get(ADMIN_LIST_URL, headers=headers)
    assert response.status_code == 200, response.text
    return next(
        item for item in response.json()["items"] if item["slug"] == "weekly-report"
    )


async def _user_item(
    client: httpx.AsyncClient, headers: dict[str, str], skill_id: str
) -> dict:
    response = await client.get(SKILLS_URL, headers=headers)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["items"] if item["id"] == skill_id)


async def _audit_details(action: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == action)
                )
            ).scalars()
        )
    return [row.detail for row in rows]


async def test_list_system_skills_only(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """只含系统技能、排除私有技能、名称升序且 stats 与筛选结果一致。"""
    registered = await register_user(client)
    private = await client.post(
        SKILLS_URL,
        json={"name": "私有技能", "description": "私有", "content": "私有正文"},
        headers=auth_headers(registered["access_token"]),
    )
    assert private.status_code == 201, private.text
    private_id = private.json()["id"]

    await _seed_system_skill(
        slug="zzz-extra", name="ZZZ 排序最后", enabled=True, force_disabled=True
    )
    await _seed_system_skill(
        slug="aaa-extra", name="AAA 排序最前", enabled=False, force_disabled=False
    )

    response = await client.get(ADMIN_LIST_URL, headers=admin_auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "stats"}
    items = body["items"]
    assert items
    assert all(item["scope"] == "system" for item in items)
    assert private_id not in {item["id"] for item in items}
    assert {item["slug"] for item in items} == {
        "weekly-report",
        "aaa-extra",
        "zzz-extra",
    }
    names = [item["name"] for item in items]
    assert names[:2] == ["AAA 排序最前", "ZZZ 排序最后"]
    assert "周报生成" in names

    # stats：AAA 被全局停用、ZZZ 被强制停用，只有内置 weekly-report 有效启用。
    assert body["stats"] == {"total": 3, "enabled": 1, "force_disabled": 1}
    assert body["stats"]["total"] == len(items)

    # q 匹配名称/描述，并转义 LIKE 通配符。
    weekly = await client.get(
        ADMIN_LIST_URL, params={"q": "周报"}, headers=admin_auth
    )
    assert weekly.status_code == 200, weekly.text
    assert [item["slug"] for item in weekly.json()["items"]] == ["weekly-report"]
    assert weekly.json()["stats"] == {"total": 1, "enabled": 1, "force_disabled": 0}

    wildcard = await client.get(
        ADMIN_LIST_URL, params={"q": "%"}, headers=admin_auth
    )
    assert wildcard.status_code == 200, wildcard.text
    assert wildcard.json()["items"] == []
    assert wildcard.json()["stats"] == {"total": 0, "enabled": 0, "force_disabled": 0}


async def test_q_too_long_is_validation_error(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    response = await client.get(
        ADMIN_LIST_URL, params={"q": "字" * 101}, headers=admin_auth
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_admin_enable_disable(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """全局启停立即影响普通用户看到的 ``effective_enabled``。"""
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    system_skill = await _admin_item(client, admin_auth)
    skill_id = system_skill["id"]
    assert system_skill["effective_enabled"] is True

    disabled = await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/enabled",
        json={"enabled": False},
        headers=admin_auth,
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["force_disabled"] is False
    assert disabled.json()["effective_enabled"] is False

    user_view = await _user_item(client, user_headers, skill_id)
    assert user_view["enabled"] is False
    assert user_view["effective_enabled"] is False

    enabled = await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/enabled",
        json={"enabled": True},
        headers=admin_auth,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    assert enabled.json()["effective_enabled"] is True
    assert (await _user_item(client, user_headers, skill_id))["effective_enabled"] is True


async def test_force_disable_beats_user_override(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """强制停用覆盖用户级 true；恢复后用户覆盖未被破坏、仍然生效。"""
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    system_skill = await _admin_item(client, admin_auth)
    skill_id = system_skill["id"]

    override = await client.put(
        f"{SKILLS_URL}/{skill_id}/enabled",
        json={"enabled": True},
        headers=user_headers,
    )
    assert override.status_code == 200, override.text
    assert override.json()["effective_enabled"] is True

    forced = await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/force-disable",
        json={"force_disabled": True},
        headers=admin_auth,
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["force_disabled"] is True
    assert forced.json()["effective_enabled"] is False
    admin_row = next(
        item
        for item in (
            await client.get(ADMIN_LIST_URL, headers=admin_auth)
        ).json()["items"]
        if item["id"] == skill_id
    )
    assert admin_row["effective_enabled"] is False

    user_view = await _user_item(client, user_headers, skill_id)
    assert user_view["force_disabled"] is True
    assert user_view["effective_enabled"] is False
    catalog = await client.get(CATALOG_URL, headers=user_headers)
    assert catalog.status_code == 200, catalog.text
    assert skill_id not in {item["id"] for item in catalog.json()["items"]}

    restored = await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/force-disable",
        json={"force_disabled": False},
        headers=admin_auth,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["force_disabled"] is False
    assert restored.json()["effective_enabled"] is True

    user_view = await _user_item(client, user_headers, skill_id)
    assert user_view["effective_enabled"] is True
    async with get_session_factory()() as session:
        row = await session.get(
            UserCapabilityOverride,
            (uuid.UUID(registered["user"]["id"]), "skill", uuid.UUID(skill_id)),
        )
        assert row is not None
        assert row.enabled is True


async def test_admin_skill_not_found_for_user_scope(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """私有技能与随机 id 对两个写端点都返回 404 ``not_found``。"""
    registered = await register_user(client)
    private = await client.post(
        SKILLS_URL,
        json={"name": "私有技能", "description": "私有", "content": "私有正文"},
        headers=auth_headers(registered["access_token"]),
    )
    assert private.status_code == 201, private.text
    private_id = private.json()["id"]

    for skill_id in (private_id, str(uuid.uuid4())):
        for suffix, payload in (
            ("enabled", {"enabled": False}),
            ("force-disable", {"force_disabled": True}),
        ):
            response = await client.put(
                f"{ADMIN_LIST_URL}/{skill_id}/{suffix}",
                json=payload,
                headers=admin_auth,
            )
            assert response.status_code == 404, response.text
            error = response.json()["error"]
            assert error["code"] == "not_found"
            assert error["message"] == "系统技能不存在"


async def test_write_validation_errors(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    system_skill = await _admin_item(client, admin_auth)
    skill_id = system_skill["id"]

    missing = await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/enabled", json={}, headers=admin_auth
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "validation_error"

    extra = await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/force-disable",
        json={"force_disabled": True, "extra": 1},
        headers=admin_auth,
    )
    assert extra.status_code == 422, extra.text
    assert extra.json()["error"]["code"] == "validation_error"

    bad_uuid = await client.put(
        f"{ADMIN_LIST_URL}/not-a-uuid/enabled",
        json={"enabled": False},
        headers=admin_auth,
    )
    assert bad_uuid.status_code == 422, bad_uuid.text
    assert bad_uuid.json()["error"]["code"] == "validation_error"


async def test_audit(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    """启停/强制停用写稳定 action 与 detail（scope=system）。"""
    system_skill = await _admin_item(client, admin_auth)
    skill_id = system_skill["id"]

    await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/enabled",
        json={"enabled": False},
        headers=admin_auth,
    )
    await client.put(
        f"{ADMIN_LIST_URL}/{skill_id}/force-disable",
        json={"force_disabled": True},
        headers=admin_auth,
    )

    enable_audit = await client.get(
        "/api/v1/admin/audit-logs",
        params={"action": "skill.enable", "limit": 100},
        headers=admin_auth,
    )
    assert enable_audit.status_code == 200, enable_audit.text
    enable_items = enable_audit.json()["items"]
    assert len(enable_items) == 1
    enable_item = enable_items[0]
    assert enable_item["actor_type"] == "user"
    assert enable_item["actor_id"] == str(admin.id)
    assert enable_item["action"] == "skill.enable"
    assert enable_item["resource_type"] == "skill"
    assert enable_item["resource_id"] == skill_id
    assert enable_item["detail"] == {"enabled": False, "scope": "system"}

    force_audit = await client.get(
        "/api/v1/admin/audit-logs",
        params={"action": "skill.force_disable", "limit": 100},
        headers=admin_auth,
    )
    assert force_audit.status_code == 200, force_audit.text
    force_items = force_audit.json()["items"]
    assert len(force_items) == 1
    force_item = force_items[0]
    assert force_item["actor_id"] == str(admin.id)
    assert force_item["resource_type"] == "skill"
    assert force_item["resource_id"] == skill_id
    assert force_item["detail"] == {"force_disabled": True}

    assert await _audit_details("skill.enable") == [
        {"enabled": False, "scope": "system"}
    ]
    assert await _audit_details("skill.force_disable") == [{"force_disabled": True}]


async def test_non_admin_forbidden_and_unauthorized(
    client: httpx.AsyncClient, user: User
) -> None:
    headers = auth_headers(create_access_token(user.id).token)
    system_skill = next(
        item
        for item in (await client.get(SKILLS_URL, headers=headers)).json()["items"]
        if item["scope"] == "system"
    )
    skill_id = system_skill["id"]
    cases = [
        ("GET", ADMIN_LIST_URL, None),
        ("PUT", f"{ADMIN_LIST_URL}/{skill_id}/enabled", {"enabled": False}),
        ("PUT", f"{ADMIN_LIST_URL}/{skill_id}/force-disable", {"force_disabled": True}),
    ]
    for method, path, payload in cases:
        response = await client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, response.text
        error = response.json()["error"]
        assert error["code"] == "forbidden"
        assert error["message"] == "需要管理员权限"
        assert error["details"] == {"required_role": "admin"}

    for method, path, payload in cases:
        response = await client.request(method, path, json=payload)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "unauthorized"


async def test_capability_disabled_does_not_block_admin(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1：SKILLS_ENABLED=false 时管理端仍可读写，运行态 ``/skills`` 仍 403。"""
    monkeypatch.setattr(get_settings(), "skills_enabled", False)

    listed = await client.get(ADMIN_LIST_URL, headers=admin_auth)
    assert listed.status_code == 200, listed.text
    slugs = {item["slug"] for item in listed.json()["items"]}
    assert "weekly-report" in slugs

    system_skill = await _admin_item(client, admin_auth)
    updated = await client.put(
        f"{ADMIN_LIST_URL}/{system_skill['id']}/enabled",
        json={"enabled": False},
        headers=admin_auth,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["enabled"] is False

    runtime = await client.get(SKILLS_URL, headers=admin_auth)
    assert runtime.status_code == 403, runtime.text
    assert runtime.json()["error"]["code"] == "capability_disabled"
