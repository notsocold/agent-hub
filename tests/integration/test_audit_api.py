"""审计检索接口：用户自查范围、管理员全局、过滤与游标。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.user import User
from app.db.session import get_session_factory
from tests.conftest import auth_headers

BASE = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


async def _seed(user: User, other: User) -> None:
    async with get_session_factory()() as session:
        session.add_all(
            [
                AuditLog(
                    actor_type="user",
                    actor_id=str(user.id),
                    action="approval.decide",
                    resource_type="approval",
                    resource_id="a-1",
                    detail={"decision": "approve"},
                    created_at=BASE,
                ),
                AuditLog(
                    actor_type="user",
                    actor_id=str(user.id),
                    action="memory.create",
                    resource_type="memory",
                    resource_id="m-1",
                    detail={},
                    created_at=BASE + timedelta(minutes=2),
                ),
                AuditLog(
                    actor_type="user",
                    actor_id=str(other.id),
                    action="agent.refresh",
                    resource_type="agent",
                    resource_id="g-1",
                    detail={},
                    created_at=BASE + timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()


def _headers(user: User) -> dict[str, str]:
    return auth_headers(create_access_token(user.id).token)


async def test_user_sees_only_own_logs(client: httpx.AsyncClient, user: User, db) -> None:
    async with get_session_factory()() as session:
        other = User(
            email="other-audit@example.com",
            password_hash="x",
            display_name="other",
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
    await _seed(user, other)

    response = await client.get("/api/v1/audit-logs", headers=_headers(user))
    assert response.status_code == 200
    body = response.json()
    assert [item["action"] for item in body["items"]] == ["memory.create", "approval.decide"]
    assert body["has_more"] is False


async def test_user_cannot_impersonate_actor(
    client: httpx.AsyncClient, user: User, db
) -> None:
    async with get_session_factory()() as session:
        other = User(email="other-audit-2@example.com", password_hash="x", display_name="other")
        session.add(other)
        await session.commit()
        await session.refresh(other)
    await _seed(user, other)

    response = await client.get(
        f"/api/v1/audit-logs?actor_id={other.id}", headers=_headers(user)
    )
    assert response.status_code == 200
    assert [item["action"] for item in response.json()["items"]] == [
        "memory.create",
        "approval.decide",
    ]


async def test_admin_sees_global_and_filters(
    client: httpx.AsyncClient, user: User, admin: User, db
) -> None:
    await _seed(user, admin)

    response = await client.get("/api/v1/admin/audit-logs", headers=_headers(admin))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3

    filtered = await client.get(
        "/api/v1/admin/audit-logs?action=agent.refresh", headers=_headers(admin)
    )
    assert [item["action"] for item in filtered.json()["items"]] == ["agent.refresh"]

    # 普通用户不能访问管理员接口
    forbidden = await client.get("/api/v1/admin/audit-logs", headers=_headers(user))
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"


async def test_audit_time_range_and_cursor(client: httpx.AsyncClient, user: User, db) -> None:
    async with get_session_factory()() as session:
        other = User(email="other-audit-3@example.com", password_hash="x", display_name="other")
        session.add(other)
        await session.commit()
        await session.refresh(other)
    await _seed(user, other)

    page1 = await client.get("/api/v1/audit-logs?limit=1", headers=_headers(user))
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["has_more"] is True
    assert body1["next_cursor"]
    assert body1["items"][0]["action"] == "memory.create"

    page2 = await client.get(
        f"/api/v1/audit-logs?limit=1&cursor={body1['next_cursor']}", headers=_headers(user)
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert body2["items"][0]["action"] == "approval.decide"
    assert body2["has_more"] is False

    ranged = await client.get(
        "/api/v1/audit-logs?start=2026-11-10T10:01:00Z", headers=_headers(user)
    )
    assert [item["action"] for item in ranged.json()["items"]] == ["memory.create"]

    invalid_cursor = await client.get(
        "/api/v1/audit-logs?cursor=not-a-cursor", headers=_headers(user)
    )
    assert invalid_cursor.status_code == 422
