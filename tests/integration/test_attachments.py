"""附件集成测试：上传/去重/越权/下载/删除/列表与聊天快照。"""

from __future__ import annotations

import io

import httpx

from tests.conftest import auth_headers, collect_sse, register_user


def _file(content: bytes, filename: str = "note.txt", mime: str = "text/plain"):
    return {"file": (filename, io.BytesIO(content), mime)}


async def test_upload_and_download_attachment(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    response = await client.post("/api/v1/attachments", files=_file("你好，附件".encode()), headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["filename"] == "note.txt"
    assert body["mime_type"] == "text/plain"
    assert body["duplicate"] is False
    assert len(body["checksum"]) == 64

    detail = await client.get(f"/api/v1/attachments/{body['id']}", headers=headers)
    assert detail.status_code == 200

    download = await client.get(f"/api/v1/attachments/{body['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.content == "你好，附件".encode()
    assert "attachment" in download.headers["content-disposition"]

    listing = await client.get("/api/v1/attachments", headers=headers)
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


async def test_upload_deduplicates_by_checksum(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    first = await client.post("/api/v1/attachments", files=_file(b"same-content"), headers=headers)
    assert first.json()["duplicate"] is False
    second = await client.post(
        "/api/v1/attachments", files=_file(b"same-content", "other.txt"), headers=headers
    )
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]


async def test_upload_rejects_bad_type_and_oversize(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    bad_type = await client.post(
        "/api/v1/attachments", files=_file(b"x", "evil.exe", "application/x-msdownload"),
        headers=headers,
    )
    assert bad_type.status_code == 422
    assert bad_type.json()["error"]["code"] == "attachment_type_not_allowed"

    from app.core.config import get_settings

    limit = get_settings().attachment_max_bytes
    too_big = await client.post(
        "/api/v1/attachments", files=_file(b"0" * (limit + 1)), headers=headers
    )
    assert too_big.status_code == 413


async def test_cross_user_isolation(client: httpx.AsyncClient, second_client: httpx.AsyncClient) -> None:
    alice = await register_user(client)
    bob = await register_user(second_client)
    alice_headers = auth_headers(alice["access_token"])
    bob_headers = auth_headers(bob["access_token"])

    created = await client.post(
        "/api/v1/attachments", files=_file(b"private"), headers=alice_headers
    )
    attachment_id = created.json()["id"]
    assert created.status_code == 201

    # GET 详情、GET 下载、DELETE 对他人一律 404（资源隐藏）。
    for path in (
        f"/api/v1/attachments/{attachment_id}",
        f"/api/v1/attachments/{attachment_id}/download",
    ):
        assert (await second_client.get(path, headers=bob_headers)).status_code == 404
    assert (
        await second_client.delete(f"/api/v1/attachments/{attachment_id}", headers=bob_headers)
    ).status_code == 404
    assert (await second_client.get("/api/v1/attachments", headers=bob_headers)).json()["items"] == []


async def test_delete_soft_when_referenced(client: httpx.AsyncClient) -> None:
    """附件被消息引用后删除走软删；未被引用的走物理删除。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await client.post("/api/v1/attachments", files=_file("引用我".encode()), headers=headers)
    attachment_id = created.json()["id"]

    run_id, events = await collect_sse(
        client,
        headers=headers,
        payload={"message": "看附件", "attachments": [attachment_id]},
    )
    assert run_id
    assert events[-1][0] == "message.done"

    deleted = await client.delete(f"/api/v1/attachments/{attachment_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/attachments/{attachment_id}", headers=headers)).status_code == 404

    # 消息历史中的快照仍保留附件元数据。
    conversation_id = events[0][1]["conversation_id"]
    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    user_message = next(item for item in messages.json()["items"] if item["role"] == "user")
    assert user_message["attachments"][0]["id"] == attachment_id
    assert user_message["attachments"][0]["filename"] == "note.txt"


async def test_attachment_content_injected_into_model(client: httpx.AsyncClient, monkeypatch) -> None:
    """文本附件内容注入模型上下文：脚本化模型能收到附件正文与用户消息。"""
    from app.modules.chat import service as chat_service
    from tests.helpers import make_scripted_model

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await client.post(
        "/api/v1/attachments", files=_file("附件正文内容".encode()), headers=headers
    )
    attachment_id = created.json()["id"]

    model = make_scripted_model([{"text": "收到附件"}])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(
        client,
        headers=headers,
        payload={"message": "总结附件", "attachments": [attachment_id]},
    )
    assert events[-1][0] == "message.done"
    assert model.captured
    all_text = "\n".join(str(item.content) for item in model.captured[0])
    assert "总结附件" in all_text
    assert "附件正文内容" in all_text
    assert "note.txt" in all_text
