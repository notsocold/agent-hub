"""技能集成测试：手写创建、zip 导入、文件读取、启停、系统技能只读、越权、聊天激活。"""

from __future__ import annotations

import io
import zipfile

import httpx

from app.db.models.mcp import UserCapabilityOverride
from app.db.session import get_session_factory
from tests.conftest import auth_headers, collect_sse, register_user
from tests.helpers import make_scripted_model


def _skill_zip(slug: str = "my-skill", files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {slug}\ndescription: 测试技能\nversion: \"1.0\"\n---\n# 测试技能\n\n1. 第一步；\n2. 第二步。\n".encode(),
        )
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


async def test_manual_skill_create_and_update(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    created = await client.post(
        "/api/v1/skills",
        json={"name": "会议纪要", "description": "生成会议纪要", "content": "1. 记录议题；2. 输出行动项。"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    skill = created.json()
    assert skill["scope"] == "user"
    assert skill["source"] == "manual"
    assert skill["effective_enabled"] is True
    assert skill["slug"]

    listing = await client.get("/api/v1/skills", headers=headers)
    body = listing.json()
    assert body["stats"]["user"] == 1
    assert body["stats"]["system"] == 1  # 内置 weekly-report
    assert body["stats"]["enabled"] == 2

    updated = await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"description": "生成结构化会议纪要"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "生成结构化会议纪要"


async def test_skill_zip_import_with_files(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    raw = _skill_zip("weekly-notes", {"references/template.md": "# 模板\n\n- 议题\n- 结论".encode()})
    imported = await client.post(
        "/api/v1/skills/import",
        files={"file": ("weekly-notes.zip", io.BytesIO(raw), "application/zip")},
        headers=headers,
    )
    assert imported.status_code == 201, imported.text
    skill = imported.json()
    assert skill["slug"] == "weekly-notes"
    assert skill["source"] == "upload"
    assert skill["file_count"] == 1
    assert skill["name"] == "测试技能"  # 正文首个 H1
    assert [item["path"] for item in skill["files"]] == ["references/template.md"]

    files = await client.get(f"/api/v1/skills/{skill['id']}/files", headers=headers)
    assert files.status_code == 200
    assert files.json()[0]["path"] == "references/template.md"

    content = await client.get(
        f"/api/v1/skills/{skill['id']}/files/references/template.md", headers=headers
    )
    assert content.status_code == 200
    assert "模板" in content.json()["content"]

    traversal = await client.get(
        f"/api/v1/skills/{skill['id']}/files/..%2F..%2Fetc%2Fpasswd", headers=headers
    )
    assert traversal.status_code in (404, 422)

    # 同名 slug 再导 → 409
    duplicate = await client.post(
        "/api/v1/skills/import",
        files={"file": ("weekly-notes.zip", io.BytesIO(raw), "application/zip")},
        headers=headers,
    )
    assert duplicate.status_code == 409


async def test_skill_import_rejects_invalid_zip(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    bad = await client.post(
        "/api/v1/skills/import",
        files={"file": ("bad.zip", io.BytesIO(b"not a zip"), "application/zip")},
        headers=headers,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "skill_invalid"


async def test_system_skill_readonly_and_override(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    listing = await client.get("/api/v1/skills", headers=headers)
    system_skill = next(item for item in listing.json()["items"] if item["scope"] == "system")
    assert system_skill["slug"] == "weekly-report"

    # 系统技能不可改/删
    assert (
        await client.patch(
            f"/api/v1/skills/{system_skill['id']}", json={"name": "hack"}, headers=headers
        )
    ).status_code == 403
    assert (
        await client.delete(f"/api/v1/skills/{system_skill['id']}", headers=headers)
    ).status_code == 403

    # 停用系统技能 → 写用户覆盖表，effective_enabled=false
    disabled = await client.put(
        f"/api/v1/skills/{system_skill['id']}/enabled", json={"enabled": False}, headers=headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["effective_enabled"] is False

    async with get_session_factory()() as db:
        override = await db.get(
            UserCapabilityOverride, (registered["user"]["id"], "skill", system_skill["id"])
        )
        assert override is not None and override.enabled is False

        # 覆盖只影响本人；重新启用后回到 enabled
    enabled = await client.put(
        f"/api/v1/skills/{system_skill['id']}/enabled", json={"enabled": True}, headers=headers
    )
    assert enabled.json()["effective_enabled"] is True


async def test_skill_cross_user_isolation(client: httpx.AsyncClient, second_client: httpx.AsyncClient) -> None:
    alice = await register_user(client)
    bob = await register_user(second_client)
    created = await client.post(
        "/api/v1/skills",
        json={"name": "私有技能", "description": "私有", "content": "内容"},
        headers=auth_headers(alice["access_token"]),
    )
    skill_id = created.json()["id"]
    bob_headers = auth_headers(bob["access_token"])

    assert (
        await second_client.get(f"/api/v1/skills/{skill_id}", headers=bob_headers)
    ).status_code == 404
    assert (
        await second_client.patch(f"/api/v1/skills/{skill_id}", json={"name": "x"}, headers=bob_headers)
    ).status_code == 404
    assert (
        await second_client.delete(f"/api/v1/skills/{skill_id}", headers=bob_headers)
    ).status_code == 404
    own = (await second_client.get("/api/v1/skills", headers=bob_headers)).json()["items"]
    assert all(item["scope"] == "system" for item in own)


async def test_chat_skill_activation_events(client: httpx.AsyncClient, monkeypatch) -> None:
    """聊天携带 skills：skill.activated 在首个 delta 之前，正文进入模型上下文。"""
    from app.modules.chat import service as chat_service

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    created = await client.post(
        "/api/v1/skills",
        json={"name": "周报", "description": "周报生成", "content": "1. 收集进展；2. 三段式输出。"},
        headers=headers,
    )
    skill_id = created.json()["id"]

    model = make_scripted_model([{"text": "按技能输出"}])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(
        client, headers=headers, payload={"message": "写周报", "skills": [skill_id]}
    )
    names = [name for name, _ in events]
    assert "skill.activated" in names
    assert names[-1] == "message.done"
    activated_index = names.index("skill.activated")
    delta_indices = [i for i, name in enumerate(names) if name == "message.delta"]
    assert activated_index < min(delta_indices)  # 首个 delta 之前

    activated = next(data for name, data in events if name == "skill.activated")
    assert activated["skill_id"] == skill_id
    assert activated["scope"] == "user"
    assert activated["estimated_tokens"] > 0

    # 技能正文进入模型上下文（system prompt 中的 <skill> 包裹块）
    system_text = str(model.captured[0][0].content)
    assert "<skill" in system_text
    assert "三段式输出" in system_text

    # 停用后显式选择 → 422
    await client.put(f"/api/v1/skills/{skill_id}/enabled", json={"enabled": False}, headers=headers)
    rejected = await client.post(
        "/api/v1/chat/stream", json={"message": "x", "skills": [skill_id]}, headers=headers
    )
    assert rejected.status_code == 422


async def test_read_skill_file_tool(client: httpx.AsyncClient, monkeypatch) -> None:
    """read_skill_file 原生工具读取本轮激活技能的参考文件。"""
    from app.modules.chat import service as chat_service

    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    raw = _skill_zip("tool-skill", {"references/checklist.md": "# 清单\n- 第一项".encode()})
    imported = await client.post(
        "/api/v1/skills/import",
        files={"file": ("tool-skill.zip", io.BytesIO(raw), "application/zip")},
        headers=headers,
    )
    skill = imported.json()

    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {
                        "name": "read_skill_file",
                        "args": {"skill_id": skill["id"], "path": "references/checklist.md"},
                    }
                ]
            },
            {"text": "清单已读取"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    _, events = await collect_sse(
        client, headers=headers, payload={"message": "读取技能清单", "skills": [skill["id"]]}
    )
    names = [name for name, _ in events]
    assert "tool.start" in names and "tool.result" in names
    result = next(data for name, data in events if name == "tool.result")
    assert result["status"] == "ok"
    assert result["source"] == "skill"
    assert names[-1] == "message.done"
    assert model.captured[0][0].content  # 第二轮模型能看到工具结果
