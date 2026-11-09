"""分页契约与错误可观测性回归测试。

前端反馈的核心 P0：只要列表需要返回 ``next_cursor``，cursor 编码就会把 ORM 的
``UUID`` 对象交给 ``json.dumps`` 并抛 ``TypeError``，被全局异常处理成 500。
这里直接走真实 API 覆盖会话、消息、审批三条分页链路。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx

from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.session import get_session_factory
from tests.conftest import auth_headers, collect_sse, register_user


async def test_conversation_pagination_has_more(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    created_ids: list[str] = []
    for index in range(3):
        response = await client.post(
            "/api/v1/conversations", json={"title": f"会话 {index}"}, headers=headers
        )
        assert response.status_code == 201, response.text
        created_ids.append(response.json()["id"])

    first = await client.get("/api/v1/conversations?limit=2", headers=headers)
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["has_more"] is True
    assert page["next_cursor"]
    assert len(page["items"]) == 2
    # stats 是当前筛选条件下的全量计数，不是当前页计数
    assert page["stats"]["total"] == 3
    assert {item["id"] for item in page["items"]} < set(created_ids)

    second = await client.get(
        "/api/v1/conversations",
        params={"limit": 2, "cursor": page["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_page = second.json()
    assert second_page["has_more"] is False
    assert second_page["next_cursor"] is None
    assert len(second_page["items"]) == 1
    assert second_page["stats"]["total"] == 3
    # 两页不重复，且第二页严格更旧（这里 last_message_at 均为 null，按 id 倒序）
    assert not {item["id"] for item in page["items"]} & {
        item["id"] for item in second_page["items"]
    }
    assert uuid.UUID(second_page["items"][0]["id"]) < min(
        uuid.UUID(item["id"]) for item in page["items"]
    )


async def test_message_pagination_has_more(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    run_id, events = await collect_sse(client, headers=headers, payload={"message": "分页测试"})
    assert run_id
    conversation_id = events[0][1]["conversation_id"]

    first = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages?limit=1", headers=headers
    )
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["has_more"] is True
    assert page["next_cursor"]
    assert len(page["items"]) == 1

    second = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"limit": 1, "cursor": page["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_page = second.json()
    assert second_page["has_more"] is False
    assert second_page["next_cursor"] is None
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["id"] != page["items"][0]["id"]
    # 新 → 旧：第二页严格更早
    assert second_page["items"][0]["created_at"] <= page["items"][0]["created_at"]

    # 契约：before 与 cursor 同时传时以 cursor 为准，不再叠加 before 条件
    with_before = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={
            "limit": 1,
            "cursor": page["next_cursor"],
            "before": "2000-01-01T00:00:00Z",
        },
        headers=headers,
    )
    assert with_before.status_code == 200, with_before.text
    assert with_before.json()["items"][0]["id"] == second_page["items"][0]["id"]


async def test_approval_pagination_has_more(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    created = await client.post(
        "/api/v1/conversations", json={"title": "审批分页"}, headers=headers
    )
    assert created.status_code == 201, created.text
    conversation_id = uuid.UUID(created.json()["id"])

    now = datetime.now(UTC)
    async with get_session_factory()() as db:
        run = Run(conversation_id=conversation_id, status="succeeded", model="fake")
        db.add(run)
        await db.flush()
        for index in range(3):
            db.add(
                Approval(
                    run_id=run.id,
                    tool_name=f"demo-tool-{index}",
                    tool_display_name=f"示例工具 {index}",
                    created_at=now + timedelta(seconds=index),
                )
            )
        await db.commit()

    first = await client.get(
        "/api/v1/approvals", params={"status": "all", "limit": 2}, headers=headers
    )
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["has_more"] is True
    assert page["next_cursor"]
    assert len(page["items"]) == 2

    second = await client.get(
        "/api/v1/approvals",
        params={"status": "all", "limit": 2, "cursor": page["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    second_page = second.json()
    assert second_page["has_more"] is False
    assert second_page["next_cursor"] is None
    assert len(second_page["items"]) == 1
    assert not {item["id"] for item in page["items"]} & {
        item["id"] for item in second_page["items"]
    }


async def test_invalid_cursor_returns_validation_error(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    response = await client.get(
        "/api/v1/conversations", params={"cursor": "not-a-cursor"}, headers=headers
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"] != "unknown"


async def test_internal_error_returns_real_request_id(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    from app.main import app
    from app.modules.system import service as system_service

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    def _boom() -> None:
        raise RuntimeError("模拟内部错误")

    monkeypatch.setattr(system_service, "build_system_info", _boom)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get(
            "/api/v1/system/info",
            headers={**headers, "X-Request-ID": "trace-internal-error"},
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "trace-internal-error"
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"] == "trace-internal-error"
