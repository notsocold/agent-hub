"""管理台用户管理：列表/筛选/统计/游标、详情、角色与状态更新及审计。

覆盖 M8 T1 契约：
- 列表按 ``created_at DESC, id DESC`` 排序，``q`` 转义 LIKE 通配符，
  ``stats`` 为当前筛选条件下的全量计数；
- 详情与列表项同形且永不返回 ``password_hash``；
- PATCH 自操作 422、最后活跃管理员 409、无变化不写审计；
- 普通用户 3 个端点均 403 ``forbidden`` + ``details.required_role="admin"``，无 token 401。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import encode_cursor
from app.core.errors import ConflictError
from app.core.security import create_access_token, hash_password
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.admin import service as admin_service
from app.modules.admin.schemas import AdminUserPatchRequest
from tests.conftest import auth_headers

_USER_FIELDS = {
    "id",
    "email",
    "display_name",
    "role",
    "status",
    "last_login_at",
    "created_at",
    "updated_at",
}


async def _create_user_row(
    db: AsyncSession,
    *,
    role: str = "user",
    status: str = "active",
    display_name: str = "测试用户",
    email: str | None = None,
) -> User:
    """直接落库造用户（比走注册接口更可控），提交后返回已刷新行。"""
    row = User(
        email=email or f"{role}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password123"),
        display_name=display_name,
        role=role,
        status=status,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _user_headers(user: User) -> dict[str, str]:
    return auth_headers(create_access_token(user.id).token)


async def test_list_users_filters_stats_and_cursor(
    client: httpx.AsyncClient,
    admin: User,
    admin_auth: dict[str, str],
    user: User,
    db: AsyncSession,
) -> None:
    """默认列表、stats、role/status/q 过滤与游标翻页。"""
    disabled = await _create_user_row(db, status="disabled", display_name="停用用户")

    response = await client.get("/api/v1/admin/users", headers=admin_auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stats"] == {"total": 3, "active": 2, "disabled": 1, "admins": 1}
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert {item["id"] for item in body["items"]} == {
        str(admin.id),
        str(user.id),
        str(disabled.id),
    }

    by_role = await client.get(
        "/api/v1/admin/users", params={"role": "user"}, headers=admin_auth
    )
    assert by_role.status_code == 200, by_role.text
    role_body = by_role.json()
    assert {item["id"] for item in role_body["items"]} == {str(user.id), str(disabled.id)}
    assert all(item["role"] == "user" for item in role_body["items"])
    # stats 是当前筛选条件下的计数：role=user 时管理员数为 0。
    assert role_body["stats"] == {"total": 2, "active": 1, "disabled": 1, "admins": 0}

    by_status = await client.get(
        "/api/v1/admin/users", params={"status": "disabled"}, headers=admin_auth
    )
    assert by_status.status_code == 200, by_status.text
    status_body = by_status.json()
    assert [item["id"] for item in status_body["items"]] == [str(disabled.id)]
    assert status_body["stats"] == {"total": 1, "active": 0, "disabled": 1, "admins": 0}

    by_email = await client.get(
        "/api/v1/admin/users",
        params={"q": user.email.split("@")[0]},
        headers=admin_auth,
    )
    assert by_email.status_code == 200, by_email.text
    assert [item["id"] for item in by_email.json()["items"]] == [str(user.id)]

    by_name = await client.get(
        "/api/v1/admin/users", params={"q": "停用"}, headers=admin_auth
    )
    assert by_name.status_code == 200, by_name.text
    assert [item["id"] for item in by_name.json()["items"]] == [str(disabled.id)]

    first = await client.get(
        "/api/v1/admin/users", params={"limit": 1}, headers=admin_auth
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    seen = [first_body["items"][0]["id"]]
    cursor = first_body["next_cursor"]
    # 连续翻页直到结束，确认游标不重复、不漏项。
    for _ in range(5):
        page = await client.get(
            "/api/v1/admin/users",
            params={"limit": 1, "cursor": cursor},
            headers=admin_auth,
        )
        assert page.status_code == 200, page.text
        page_body = page.json()
        seen.extend(item["id"] for item in page_body["items"])
        if not page_body["has_more"]:
            assert page_body["next_cursor"] is None
            break
        cursor = page_body["next_cursor"]
    assert len(seen) == 3
    assert len(set(seen)) == 3


async def test_q_escapes_like_wildcards(
    client: httpx.AsyncClient, admin_auth: dict[str, str], db: AsyncSession
) -> None:
    """q 必须转义 %/_/\，否则用户输入会变成通配符。"""
    percent = await _create_user_row(
        db, email="pct%user@example.com", display_name="百分号用户"
    )
    underscore = await _create_user_row(
        db, email="under_score@example.com", display_name="下划线用户"
    )

    by_percent = await client.get(
        "/api/v1/admin/users", params={"q": "%"}, headers=admin_auth
    )
    assert by_percent.status_code == 200, by_percent.text
    assert [item["id"] for item in by_percent.json()["items"]] == [str(percent.id)]

    by_underscore = await client.get(
        "/api/v1/admin/users", params={"q": "_"}, headers=admin_auth
    )
    assert by_underscore.status_code == 200, by_underscore.text
    assert [item["id"] for item in by_underscore.json()["items"]] == [
        str(underscore.id)
    ]


async def test_get_user_detail_and_not_found(
    client: httpx.AsyncClient, admin_auth: dict[str, str], user: User
) -> None:
    response = await client.get(
        f"/api/v1/admin/users/{user.id}", headers=admin_auth
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == _USER_FIELDS
    assert body["id"] == str(user.id)
    assert body["email"] == user.email
    assert body["display_name"] == user.display_name
    assert body["role"] == "user"
    assert body["status"] == "active"
    assert body["last_login_at"] is None
    assert "password_hash" not in body

    missing = await client.get(
        f"/api/v1/admin/users/{uuid.uuid4()}", headers=admin_auth
    )
    assert missing.status_code == 404, missing.text
    error = missing.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "用户不存在"

    missing_patch = await client.patch(
        f"/api/v1/admin/users/{uuid.uuid4()}",
        json={"status": "disabled"},
        headers=admin_auth,
    )
    assert missing_patch.status_code == 404, missing_patch.text
    assert missing_patch.json()["error"]["code"] == "not_found"


async def test_patch_role_status_and_audit(
    client: httpx.AsyncClient,
    admin: User,
    admin_auth: dict[str, str],
    user: User,
    db: AsyncSession,
) -> None:
    second_admin = await _create_user_row(db, role="admin", display_name="第二管理员")

    demoted = await client.patch(
        f"/api/v1/admin/users/{second_admin.id}",
        json={"role": "user"},
        headers=admin_auth,
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["role"] == "user"
    assert demoted.json()["status"] == "active"

    disabled = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        json={"status": "disabled"},
        headers=admin_auth,
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    # 无实际变化的 PATCH 返回 200 但不写审计。
    noop = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        json={"status": "disabled"},
        headers=admin_auth,
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["status"] == "disabled"

    audit = await client.get(
        "/api/v1/admin/audit-logs",
        params={"action": "admin.user.update", "limit": 100},
        headers=admin_auth,
    )
    assert audit.status_code == 200, audit.text
    items = audit.json()["items"]
    assert len(items) == 2
    by_resource = {item["resource_id"]: item for item in items}
    assert set(by_resource) == {str(second_admin.id), str(user.id)}
    assert by_resource[str(second_admin.id)]["detail"] == {
        "fields": ["role"],
        "role": "user",
        "status": "active",
    }
    assert by_resource[str(user.id)]["detail"] == {
        "fields": ["status"],
        "role": "user",
        "status": "disabled",
    }
    for item in items:
        assert item["actor_type"] == "user"
        assert item["actor_id"] == str(admin.id)
        assert item["action"] == "admin.user.update"
        assert item["resource_type"] == "user"


async def test_cannot_change_own_role(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str], db: AsyncSession
) -> None:
    response = await client.patch(
        f"/api/v1/admin/users/{admin.id}",
        json={"role": "user"},
        headers=admin_auth,
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "cannot_change_own_role"
    assert error["message"] == "不能修改自己的角色"

    row = await db.get(User, admin.id)
    assert row is not None
    assert row.role == "admin"


async def test_cannot_disable_self(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str], db: AsyncSession
) -> None:
    response = await client.patch(
        f"/api/v1/admin/users/{admin.id}",
        json={"status": "disabled"},
        headers=admin_auth,
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "cannot_disable_self"
    assert error["message"] == "不能停用自己的账号"

    row = await db.get(User, admin.id)
    assert row is not None
    assert row.status == "active"


async def test_last_active_admin_is_protected(
    db: AsyncSession, admin: User, user: User
) -> None:
    """服务层绕过自操作规则：用普通用户对象作 actor 触发最后管理员保护。"""
    actor = await db.get(User, user.id)
    assert actor is not None

    with pytest.raises(ConflictError) as exc_info:
        await admin_service.update_user(
            db, actor, admin.id, AdminUserPatchRequest(role="user")
        )

    assert exc_info.value.code == "last_admin_protected"
    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"active_admins": 1}
    await db.rollback()

    row = await db.get(User, admin.id)
    assert row is not None
    assert row.role == "admin"
    assert row.status == "active"


async def test_can_disable_other_admin_when_another_active_admin_remains(
    client: httpx.AsyncClient,
    admin: User,
    admin_auth: dict[str, str],
    db: AsyncSession,
) -> None:
    second_admin = await _create_user_row(db, role="admin", display_name="第二管理员")

    response = await client.patch(
        f"/api/v1/admin/users/{second_admin.id}",
        json={"status": "disabled"},
        headers=admin_auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"

    detail = await client.get(
        f"/api/v1/admin/users/{second_admin.id}", headers=admin_auth
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "disabled"


async def test_non_admin_forbidden_and_unauthorized(
    client: httpx.AsyncClient, user: User
) -> None:
    cases = [
        ("GET", "/api/v1/admin/users", None),
        ("GET", f"/api/v1/admin/users/{user.id}", None),
        ("PATCH", f"/api/v1/admin/users/{user.id}", {"status": "disabled"}),
    ]

    headers = _user_headers(user)
    for method, path, payload in cases:
        response = await client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 403, response.text
        error = response.json()["error"]
        assert error["code"] == "forbidden"
        assert error["message"] == "需要管理员权限"
        assert error["details"] == {"required_role": "admin"}

    for method, path, payload in cases:
        response = await client.request(method, path, json=payload)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "unauthorized"


async def test_invalid_cursor_and_patch_validation(
    client: httpx.AsyncClient, admin: User, admin_auth: dict[str, str]
) -> None:
    malformed = await client.get(
        "/api/v1/admin/users", params={"cursor": "not-a-cursor"}, headers=admin_auth
    )
    assert malformed.status_code == 422, malformed.text
    assert malformed.json()["error"]["code"] == "validation_error"
    assert malformed.json()["error"]["message"] == "分页游标无效"

    # 可解码但字段缺失/类型错误的游标同样 422。
    for payload in (
        {"id": str(uuid.uuid4())},
        {"created_at": "not-a-datetime", "id": str(uuid.uuid4())},
        {"created_at": datetime.now(UTC).isoformat(), "id": "not-a-uuid"},
    ):
        response = await client.get(
            "/api/v1/admin/users",
            params={"cursor": encode_cursor(payload)},
            headers=admin_auth,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "validation_error"

    for payload in ({}, {"display_name": "x"}, {"role": None}):
        response = await client.patch(
            f"/api/v1/admin/users/{admin.id}", json=payload, headers=admin_auth
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "validation_error"

    for params in (
        {"role": "superuser"},
        {"status": "banned"},
        {"limit": 0},
        {"limit": 101},
    ):
        response = await client.get(
            "/api/v1/admin/users", params=params, headers=admin_auth
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "validation_error"


async def test_concurrent_promote_then_disable_keeps_an_active_admin(
    db: AsyncSession, admin: User, user: User
) -> None:
    """并发「提升 T 为 admin」+「停用 T」：锁后 refresh 必须挡住 0 活跃管理员。

    复现旧缺口：停用方会话在提升提交前已把 T 读成普通用户；随后另一个事务把原管理员
    降级，T 成为唯一活跃管理员。修复前停用方不重新加锁/refresh，会直接写
    ``status=disabled`` 导致 0 个活跃管理员；修复后应按锁后状态返回 409。
    """
    factory = get_session_factory()
    async with factory() as stale_db:
        # B 会话先读到 T 是普通用户，identity map 保留该陈旧快照（模拟交错）。
        stale_target = await stale_db.get(User, user.id)
        stale_actor = await stale_db.get(User, admin.id)
        assert stale_target is not None and stale_target.role == "user"
        assert stale_actor is not None

        # A：把 T 提升为活跃管理员。
        async with factory() as promote_db:
            promote_actor = await promote_db.get(User, admin.id)
            assert promote_actor is not None
            promoted = await admin_service.update_user(
                promote_db, promote_actor, user.id, AdminUserPatchRequest(role="admin")
            )
            assert promoted.role == "admin"

        # C：把原管理员降级，此时 T 是唯一活跃管理员。
        async with factory() as demote_db:
            demote_actor = await demote_db.get(User, user.id)
            assert demote_actor is not None
            demoted = await admin_service.update_user(
                demote_db, demote_actor, admin.id, AdminUserPatchRequest(role="user")
            )
            assert demoted.role == "user"

        # B：用陈旧会话停用 T；锁后 refresh 发现 T 已是最后一个活跃管理员。
        with pytest.raises(ConflictError) as exc_info:
            await admin_service.update_user(
                stale_db, stale_actor, user.id, AdminUserPatchRequest(status="disabled")
            )
        assert exc_info.value.code == "last_admin_protected"
        assert exc_info.value.details == {"active_admins": 1}
        await stale_db.rollback()

    active_admins = int(
        await db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.status == "active"
            )
        )
        or 0
    )
    assert active_admins == 1
    remaining = await db.get(User, user.id)
    assert remaining is not None
    assert remaining.role == "admin"
    assert remaining.status == "active"


async def test_concurrent_mutual_demotion_keeps_one_active_admin(
    db: AsyncSession, admin: User
) -> None:
    """两个管理员并发互降：固定加锁顺序串行化，恰好一个成功、一个 409。"""
    second_admin = await _create_user_row(db, role="admin", display_name="第二管理员")
    factory = get_session_factory()

    async def _demote(actor_id: uuid.UUID, target_id: uuid.UUID):
        async with factory() as session:
            actor = await session.get(User, actor_id)
            assert actor is not None
            return await admin_service.update_user(
                session, actor, target_id, AdminUserPatchRequest(role="user")
            )

    results = await asyncio.gather(
        _demote(admin.id, second_admin.id),
        _demote(second_admin.id, admin.id),
        return_exceptions=True,
    )

    successes = [item for item in results if not isinstance(item, BaseException)]
    conflicts = [
        item
        for item in results
        if isinstance(item, ConflictError) and item.code == "last_admin_protected"
    ]
    assert len(successes) == 1, results
    assert len(conflicts) == 1, results

    active_admins = int(
        await db.scalar(
            select(func.count(User.id)).where(
                User.role == "admin", User.status == "active"
            )
        )
        or 0
    )
    assert active_admins == 1


async def test_stats_admins_includes_disabled_admins(
    client: httpx.AsyncClient,
    admin: User,
    admin_auth: dict[str, str],
    db: AsyncSession,
) -> None:
    """``stats.admins`` 按 role 计数，包含已停用的管理员。"""
    disabled_admin = await _create_user_row(
        db, role="admin", status="disabled", display_name="停用管理员"
    )

    body = (await client.get("/api/v1/admin/users", headers=admin_auth)).json()
    assert body["stats"] == {"total": 2, "active": 1, "disabled": 1, "admins": 2}

    filtered = await client.get(
        "/api/v1/admin/users",
        params={"role": "admin", "status": "disabled"},
        headers=admin_auth,
    )
    assert filtered.status_code == 200, filtered.text
    filtered_body = filtered.json()
    assert [item["id"] for item in filtered_body["items"]] == [str(disabled_admin.id)]
    assert filtered_body["stats"] == {
        "total": 1,
        "active": 0,
        "disabled": 1,
        "admins": 1,
    }


async def test_q_escapes_backslash(
    client: httpx.AsyncClient, admin_auth: dict[str, str], db: AsyncSession
) -> None:
    """q 中的反斜杠必须按字面量匹配，不能破坏 ESCAPE 语义或扩大匹配。"""
    await _create_user_row(db, display_name="普通用户")
    slash_user = await _create_user_row(db, display_name="反\\斜杠用户")

    response = await client.get(
        "/api/v1/admin/users", params={"q": "\\"}, headers=admin_auth
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(slash_user.id)]
