"""管理台平台默认指令（M8 T3）：读取、保存、恢复、覆盖计数与审计。

覆盖契约：
- ``GET/PUT /api/v1/admin/settings/instruction`` 同形，返回平台默认文本与
  ``is_default``/``max_chars``/``user_override_count``/``updated_by(_email)``/
  ``updated_at``；不返回 ``effective_preview``（R5）；
- ``PUT`` 显式 null（或 strip 后空串）恢复内置默认并删除 ``system_settings`` 行；
- 超长 422 ``instruction_too_long``、多余字段/缺字段 422 ``validation_error``；
- 审计 ``instruction.update`` 只记长度/指纹，不落正文；
- 非管理员 403；能力总开关关闭不阻断管理端配置（R1），运行态仍 403。
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.core.audit import content_fingerprint
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models.audit import AuditLog
from app.db.models.system import SystemSetting
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.instructions.schemas import DEFAULT_INSTRUCTION, SYSTEM_KEY_INSTRUCTION
from tests.conftest import auth_headers, register_user

URL = "/api/v1/admin/settings/instruction"

_FIELDS = {
    "content",
    "is_default",
    "max_chars",
    "user_override_count",
    "updated_by",
    "updated_by_email",
    "updated_at",
}


async def _system_setting_row() -> SystemSetting | None:
    """用独立会话读库，避免测试会话的 identity map 缓存旧行。"""
    async with get_session_factory()() as session:
        return await session.get(SystemSetting, SYSTEM_KEY_INSTRUCTION)


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


async def test_get_platform_instruction_default(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """无 ``system_settings`` 行：返回内置默认且元信息为空。"""
    response = await client.get(URL, headers=admin_auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == _FIELDS
    assert "effective_preview" not in body
    assert body["content"] == DEFAULT_INSTRUCTION
    assert body["is_default"] is True
    assert body["max_chars"] == get_settings().instruction_max_chars
    assert body["user_override_count"] == 0
    assert body["updated_by"] is None
    assert body["updated_by_email"] is None
    assert body["updated_at"] is None


async def test_get_platform_instruction_reads_legacy_value_shapes(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """兼容历史裸字符串；形态非法（dict 无 content）按内置默认处理。"""
    async with get_session_factory()() as session:
        session.add(
            SystemSetting(
                key=SYSTEM_KEY_INSTRUCTION, value="历史裸字符串：先结论后依据"
            )
        )
        await session.commit()

    legacy = await client.get(URL, headers=admin_auth)
    assert legacy.status_code == 200, legacy.text
    legacy_body = legacy.json()
    assert legacy_body["content"] == "历史裸字符串：先结论后依据"
    assert legacy_body["is_default"] is False
    assert legacy_body["updated_at"] is not None

    async with get_session_factory()() as session:
        row = await session.get(SystemSetting, SYSTEM_KEY_INSTRUCTION)
        assert row is not None
        row.value = {"unexpected": "shape"}
        await session.commit()

    invalid = await client.get(URL, headers=admin_auth)
    assert invalid.status_code == 200, invalid.text
    invalid_body = invalid.json()
    assert invalid_body["content"] == DEFAULT_INSTRUCTION
    assert invalid_body["is_default"] is True
    assert invalid_body["updated_at"] is None


async def test_put_updates_default_and_user_effective(
    client: httpx.AsyncClient,
    admin: User,
    admin_auth: dict[str, str],
) -> None:
    """保存平台默认：响应元信息完整，普通用户 ``default_value`` 实时跟随。"""
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])
    content = "平台默认：先结论后依据"

    response = await client.put(URL, json={"content": content}, headers=admin_auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == _FIELDS
    assert body["content"] == content
    assert body["is_default"] is False
    assert body["updated_by"] == str(admin.id)
    assert body["updated_by_email"] == admin.email
    assert body["updated_at"] is not None
    assert body["max_chars"] == get_settings().instruction_max_chars

    row = await _system_setting_row()
    assert row is not None
    assert row.value == {"content": content}
    assert row.updated_by == admin.id
    assert row.updated_at is not None

    effective = await client.get("/api/v1/instructions", headers=user_headers)
    assert effective.status_code == 200, effective.text
    assert effective.json()["default_value"] == content
    assert effective.json()["effective"] == content
    assert effective.json()["source"] == "system_default"


async def test_put_null_restores_builtin(
    client: httpx.AsyncClient,
    admin_auth: dict[str, str],
) -> None:
    """显式 null 与 strip 后空串都恢复内置默认并删除 ``system_settings`` 行。"""
    registered = await register_user(client)
    user_headers = auth_headers(registered["access_token"])

    saved = await client.put(
        URL, json={"content": "平台默认：结构化输出"}, headers=admin_auth
    )
    assert saved.status_code == 200, saved.text
    assert (await _system_setting_row()) is not None

    restored = await client.put(URL, json={"content": None}, headers=admin_auth)
    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["content"] == DEFAULT_INSTRUCTION
    assert restored_body["is_default"] is True
    assert restored_body["updated_by"] is None
    assert restored_body["updated_by_email"] is None
    assert restored_body["updated_at"] is None
    assert (await _system_setting_row()) is None

    effective = await client.get("/api/v1/instructions", headers=user_headers)
    assert effective.status_code == 200, effective.text
    assert effective.json()["default_value"] == DEFAULT_INSTRUCTION

    # 无行时再恢复是 no-op，仍返回内置默认。
    noop = await client.put(URL, json={"content": None}, headers=admin_auth)
    assert noop.status_code == 200, noop.text
    assert noop.json()["is_default"] is True

    # 空白字符串 strip 后等价于 null。
    await client.put(URL, json={"content": "临时默认"}, headers=admin_auth)
    blank = await client.put(URL, json={"content": "   "}, headers=admin_auth)
    assert blank.status_code == 200, blank.text
    assert blank.json()["content"] == DEFAULT_INSTRUCTION
    assert blank.json()["is_default"] is True
    assert (await _system_setting_row()) is None

    # 恢复默认的审计只标记 scope/restored_default，不落正文/长度/指纹。
    details = await _audit_details("instruction.update")
    assert {
        "scope": "platform",
        "restored_default": True,
    } in details


async def test_user_override_count(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """只统计 ``user_settings.instructions IS NOT NULL`` 的用户。"""
    first = await register_user(client)
    second = await register_user(client)
    await client.put(
        "/api/v1/instructions",
        json={"content": "个人覆盖：要点式回答"},
        headers=auth_headers(first["access_token"]),
    )
    # 第二人写入 null 会留下 instructions 为 NULL 的 user_settings 行，不计入。
    await client.put(
        "/api/v1/instructions",
        json={"content": None},
        headers=auth_headers(second["access_token"]),
    )

    response = await client.get(URL, headers=admin_auth)
    assert response.status_code == 200, response.text
    assert response.json()["user_override_count"] == 1


async def test_too_long_and_extra_field(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    too_long = await client.put(
        URL, json={"content": "字" * 4001}, headers=admin_auth
    )
    assert too_long.status_code == 422, too_long.text
    error = too_long.json()["error"]
    assert error["code"] == "instruction_too_long"
    assert "4000" in error["message"]
    assert (await _system_setting_row()) is None

    extra = await client.put(
        URL, json={"content": "x", "extra": 1}, headers=admin_auth
    )
    assert extra.status_code == 422, extra.text
    assert extra.json()["error"]["code"] == "validation_error"

    missing = await client.put(URL, json={}, headers=admin_auth)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "validation_error"


async def test_audit_has_no_content(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """审计只落字段名/长度/指纹，响应文本不含指令正文。"""
    content = "审计专用：正文不得进入审计明细"
    saved = await client.put(URL, json={"content": content}, headers=admin_auth)
    assert saved.status_code == 200, saved.text

    audit = await client.get(
        "/api/v1/admin/audit-logs",
        params={"action": "instruction.update", "limit": 100},
        headers=admin_auth,
    )
    assert audit.status_code == 200, audit.text
    assert content not in audit.text
    items = audit.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["action"] == "instruction.update"
    assert item["resource_type"] == "system_setting"
    assert item["resource_id"] == SYSTEM_KEY_INSTRUCTION
    assert item["detail"] == {
        "scope": "platform",
        "length": len(content),
        "fingerprint": content_fingerprint(content),
    }
    assert "content" not in item["detail"]
    assert isinstance(item["detail"]["fingerprint"], str)

    details = await _audit_details("instruction.update")
    assert len(details) == 1
    assert details[0]["scope"] == "platform"


async def test_non_admin_forbidden_and_unauthorized(
    client: httpx.AsyncClient, user: User
) -> None:
    headers = auth_headers(create_access_token(user.id).token)
    cases = [
        ("GET", URL, None),
        ("PUT", URL, {"content": "x"}),
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
    """R1：指令能力关闭时管理端配置面仍可读写，运行态接口仍 403。"""
    monkeypatch.setattr(get_settings(), "instructions_enabled", False)
    content = "能力关闭仍可配置的平台默认"

    fetched = await client.get(URL, headers=admin_auth)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["is_default"] is True

    updated = await client.put(URL, json={"content": content}, headers=admin_auth)
    assert updated.status_code == 200, updated.text
    assert updated.json()["content"] == content
    assert updated.json()["is_default"] is False

    runtime = await client.get("/api/v1/instructions", headers=admin_auth)
    assert runtime.status_code == 403, runtime.text
    assert runtime.json()["error"]["code"] == "capability_disabled"


async def test_restore_default_without_row_does_not_audit(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """无覆盖行时恢复默认是 no-op：返回内置默认且不写审计。"""
    response = await client.put(URL, json={"content": None}, headers=admin_auth)
    assert response.status_code == 200, response.text
    assert response.json()["is_default"] is True
    assert response.json()["updated_at"] is None
    assert await _audit_details("instruction.update") == []


async def test_second_put_on_existing_row_bumps_updated_at(
    client: httpx.AsyncClient, admin_auth: dict[str, str]
) -> None:
    """已有行第二次 PUT 必须严格刷新 updated_at（SystemSetting 无 onupdate）。"""
    first = await client.put(
        URL, json={"content": "第一次平台默认"}, headers=admin_auth
    )
    second = await client.put(
        URL, json={"content": "第二次平台默认"}, headers=admin_auth
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_at = datetime.fromisoformat(first.json()["updated_at"])
    second_at = datetime.fromisoformat(second.json()["updated_at"])
    assert second_at > first_at
    assert second.json()["updated_by"] is not None

