"""长期记忆集成测试：CRUD、stats、检索、导出、清空、越权与 save_memory 工具。"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db.models.memory import ToolInvocation
from app.db.session import get_session_factory
from tests.conftest import auth_headers, collect_sse, register_user


async def test_memory_crud_and_stats(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    created = await client.post(
        "/api/v1/memories",
        json={"kind": "preference", "title": "称呼", "content": "用户希望被称为老赵", "pinned": True},
        headers=headers,
    )
    assert created.status_code == 201
    memory = created.json()
    assert memory["kind"] == "preference"
    assert memory["pinned"] is True

    await client.post(
        "/api/v1/memories",
        json={"kind": "fact", "content": "用户的公司在杭州", "title": "公司"},
        headers=headers,
    )

    listing = await client.get("/api/v1/memories", headers=headers)
    page = listing.json()
    assert len(page["items"]) == 2
    assert page["stats"] == {
        "total": 2,
        "profile": 0,
        "preference": 1,
        "fact": 1,
        "active": 2,
        "archived": 0,
    }

    by_kind = await client.get("/api/v1/memories?kind=fact", headers=headers)
    assert [item["kind"] for item in by_kind.json()["items"]] == ["fact"]

    searched = await client.get("/api/v1/memories?q=杭州", headers=headers)
    assert len(searched.json()["items"]) == 1

    updated = await client.patch(
        f"/api/v1/memories/{memory['id']}",
        json={"status": "archived", "content": "用户希望被称为老赵（已更新）"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "archived"
    assert updated.json()["pinned"] is False  # 归档自动取消置顶

    detail = await client.get(f"/api/v1/memories/{memory['id']}", headers=headers)
    assert detail.json()["content"] == "用户希望被称为老赵（已更新）"

    exported = await client.get("/api/v1/memories/export", headers=headers)
    assert exported.status_code == 200
    assert exported.json()["version"] == 1
    assert len(exported.json()["items"]) == 2


async def test_memory_delete_and_clear(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await client.post(
        "/api/v1/memories", json={"content": "临时记忆"}, headers=headers
    )
    memory_id = created.json()["id"]

    assert (await client.delete(f"/api/v1/memories/{memory_id}", headers=headers)).status_code == 204
    assert (await client.get(f"/api/v1/memories/{memory_id}", headers=headers)).status_code == 404

    await client.post("/api/v1/memories", json={"content": "待清空"}, headers=headers)
    without_confirm = await client.delete("/api/v1/memories", headers=headers)
    assert without_confirm.status_code == 422
    cleared = await client.delete("/api/v1/memories?confirm=true", headers=headers)
    assert cleared.status_code == 204
    assert (await client.get("/api/v1/memories", headers=headers)).json()["items"] == []


async def test_memory_cross_user_isolation(client: httpx.AsyncClient, second_client: httpx.AsyncClient) -> None:
    alice = await register_user(client)
    bob = await register_user(second_client)
    created = await client.post(
        "/api/v1/memories",
        json={"content": "爱丽丝的隐私记忆"},
        headers=auth_headers(alice["access_token"]),
    )
    memory_id = created.json()["id"]
    bob_headers = auth_headers(bob["access_token"])

    assert (
        await second_client.get(f"/api/v1/memories/{memory_id}", headers=bob_headers)
    ).status_code == 404
    assert (
        await second_client.patch(
            f"/api/v1/memories/{memory_id}", json={"content": "x"}, headers=bob_headers
        )
    ).status_code == 404
    assert (
        await second_client.delete(f"/api/v1/memories/{memory_id}", headers=bob_headers)
    ).status_code == 404
    assert (await second_client.get("/api/v1/memories", headers=bob_headers)).json()["items"] == []


async def test_save_memory_tool_writes_and_emits_event(client: httpx.AsyncClient, monkeypatch) -> None:
    """save_memory 原生工具：落库、tool_invocations 记录、memory.saved 事件。"""
    from app.modules.chat import service as chat_service
    from tests.helpers import make_scripted_model

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {
                        "name": "save_memory",
                        "args": {"content": "用户喜欢用要点式回答", "kind": "preference", "title": "回答风格"},
                    }
                ]
            },
            {"text": "已记住你的偏好"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=headers, payload={"message": "记住：我喜欢用要点式回答"})
    names = [name for name, _ in events]
    assert "tool.start" in names
    assert "tool.result" in names
    assert "memory.saved" in names
    assert names[-1] == "message.done"

    saved = next(data for name, data in events if name == "memory.saved")
    assert saved["action"] == "created"
    assert saved["kind"] == "preference"
    assert saved["preview"] == "用户喜欢用要点式回答"

    memories = (await client.get("/api/v1/memories", headers=headers)).json()["items"]
    assert any(item["content"] == "用户喜欢用要点式回答" for item in memories)

    async with get_session_factory()() as db:
        invocations = list(
            (await db.execute(select(ToolInvocation))).scalars()
        )
    assert len(invocations) == 1
    assert invocations[0].tool_name == "save_memory"
    assert invocations[0].source == "native"
    assert invocations[0].status == "ok"
    assert invocations[0].run_id is not None


async def test_save_memory_tool_updates_duplicate(client: httpx.AsyncClient, monkeypatch) -> None:
    """内容完全一致的 active 记忆走合并更新（action=updated）。"""
    from app.modules.chat import service as chat_service
    from tests.helpers import make_scripted_model

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await client.post(
        "/api/v1/memories", json={"content": "已有记忆内容"}, headers=headers
    )
    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {"name": "save_memory", "args": {"content": "已有记忆内容", "kind": "fact"}}
                ]
            },
            {"text": "好的"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(client, headers=headers, payload={"message": "保存"})
    saved = next(data for name, data in events if name == "memory.saved")
    assert saved["action"] == "updated"

    memories = (await client.get("/api/v1/memories", headers=headers)).json()["items"]
    assert len(memories) == 1
