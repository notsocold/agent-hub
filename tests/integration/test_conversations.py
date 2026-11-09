"""会话与消息契约测试。"""

from __future__ import annotations

import httpx

from tests.conftest import auth_headers, collect_sse, register_user


async def test_conversation_crud_and_list_stats(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    created = await client.post(
        "/api/v1/conversations", json={"title": "需求评审"}, headers=headers
    )
    assert created.status_code == 201
    conversation = created.json()
    assert conversation["message_count"] == 0
    assert conversation["status"] == "active"

    listed = await client.get("/api/v1/conversations?status=active&limit=10", headers=headers)
    assert listed.status_code == 200
    page = listed.json()
    assert page["stats"]["total"] == 1
    assert page["items"][0]["id"] == conversation["id"]

    search = await client.get("/api/v1/conversations?q=评审", headers=headers)
    assert search.json()["items"]
    empty_search = await client.get("/api/v1/conversations?q=不存在", headers=headers)
    assert empty_search.json()["items"] == []

    patched = await client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        json={"title": "需求评审（归档）", "status": "archived"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "archived"

    detail = await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["archived_at"] is not None
    assert detail.json()["last_run"] is None

    deleted = await client.delete(f"/api/v1/conversations/{conversation['id']}", headers=headers)
    assert deleted.status_code == 204
    missing = await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
    assert missing.status_code == 404


async def test_messages_history_and_counts(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    run_id, events = await collect_sse(
        client, headers=headers, payload={"message": "帮我写一段项目介绍"}
    )
    assert run_id
    conversation_id = events[0][1]["conversation_id"]

    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert messages.status_code == 200
    items = messages.json()["items"]
    assert [item["role"] for item in items] == ["assistant", "user"]
    assistant = items[0]
    assert assistant["status"] == "completed"
    assert assistant["usage"]["total_tokens"] > 0
    assert assistant["capabilities"]["skills"] == []
    assert assistant["citations"] == []

    listed = await client.get("/api/v1/conversations", headers=headers)
    item = listed.json()["items"][0]
    assert item["message_count"] == 2
    assert item["last_message_preview"]

    # 非所有者访问返回 404，不泄露资源是否存在
    other = await register_user(client)
    forbidden = await client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth_headers(other["access_token"])
    )
    assert forbidden.status_code == 404
