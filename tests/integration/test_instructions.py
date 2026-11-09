"""指令集成测试：默认值、覆盖、超长、恢复默认与聊天注入。"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db.models.audit import AuditLog
from app.db.models.system import SystemSetting
from app.db.session import get_session_factory
from tests.conftest import auth_headers, collect_sse, register_user


async def test_instruction_default_flow(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    response = await client.get("/api/v1/instructions", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "system_default"
    assert body["user_value"] is None
    assert body["default_value"]
    assert body["effective"] == body["default_value"]
    assert body["max_chars"] == 4000


async def test_instruction_override_and_restore(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    updated = await client.put(
        "/api/v1/instructions", json={"content": "请用要点式回答，先结论后依据。"}, headers=headers
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["source"] == "user"
    assert body["effective"] == "请用要点式回答，先结论后依据。"

    fetched = await client.get("/api/v1/instructions", headers=headers)
    assert fetched.json()["source"] == "user"
    assert fetched.json()["user_value"] == "请用要点式回答，先结论后依据。"

    # null 恢复默认
    restored = await client.put("/api/v1/instructions", json={"content": None}, headers=headers)
    assert restored.status_code == 200
    assert restored.json()["source"] == "system_default"
    fetched = await client.get("/api/v1/instructions", headers=headers)
    assert fetched.json()["user_value"] is None


async def test_instruction_too_long(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    too_long = await client.put(
        "/api/v1/instructions", json={"content": "字" * 4001}, headers=headers
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "instruction_too_long"


async def test_instruction_audit_and_injection(client: httpx.AsyncClient, monkeypatch) -> None:
    """修改指令写审计；聊天时生效指令注入 system prompt。"""
    from app.modules.chat import service as chat_service
    from tests.helpers import make_scripted_model

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await client.put(
        "/api/v1/instructions", json={"content": "测试专用指令：回答必须带 emoji"}, headers=headers
    )

    model = make_scripted_model([{"text": "收到"}])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(client, headers=headers, payload={"message": "你好"})
    assert events[-1][0] == "message.done"
    assert model.captured
    system_text = str(model.captured[0][0].content)
    assert "测试专用指令：回答必须带 emoji" in system_text
    assert "基本规则" in system_text  # 基础规则仍在前

    user_id = registered["user"]["id"]
    async with get_session_factory()() as db:
        rows = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "instruction.update", AuditLog.actor_id == user_id
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].detail["length"] == len("测试专用指令：回答必须带 emoji")
    assert isinstance(rows[0].detail["fingerprint"], str)
    assert "emoji" not in rows[0].detail["fingerprint"]  # 正文不入审计


async def test_admin_default_instruction_via_system_settings(client: httpx.AsyncClient) -> None:
    """平台默认指令来自 system_settings；管理员修改后继承用户实时生效。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    async with get_session_factory()() as db:
        db.add(SystemSetting(key="instruction.default", value={"content": "平台默认：专业严谨"}))
        await db.commit()

    body = (await client.get("/api/v1/instructions", headers=headers)).json()
    assert body["source"] == "system_default"
    assert body["effective"] == "平台默认：专业严谨"
