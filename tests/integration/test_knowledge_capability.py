"""M4 Task 11：知识库能力开关与系统提示集成测试。

覆盖三件事：
1. ``system/info.capabilities.knowledge`` 跟随 ``KNOWLEDGE_ENABLED``；
2. 聊天请求的 ``knowledge_categories``：关闭时 403 ``capability_disabled``，
   开启时正常开流；数量上限读 ``KNOWLEDGE_CATEGORIES_PER_MESSAGE``（422）；
3. 系统提示词仅在「能力开启 + 当前用户可见至少一篇 ready 文档」时注入
   ``KNOWLEDGE_SECTION``。

提示词用例走真实 ``capabilities.assemble``（真实 DB 查询可见性），
不直接把手写标志塞给 ``assemble_system_prompt``，避免测不到门禁条件。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.messages import ToolMessage

from app.agents.prompts import KNOWLEDGE_SECTION, assemble_system_prompt
from app.core.config import reset_settings_cache
from app.core.security import create_access_token
from app.db.models.user import User
from app.modules.chat import capabilities as capabilities_module
from app.modules.chat import service as chat_service
from tests.conftest import auth_headers, collect_sse
from tests.helpers import make_scripted_model

INFO_URL = "/api/v1/system/info"
STREAM_URL = "/api/v1/chat/stream"


@pytest.fixture
def auth(user: User) -> dict[str, str]:
    """与 conftest ``admin_auth`` 同口径：直接签发 access token。"""
    return auth_headers(create_access_token(user.id).token)


async def _assemble_prompt(db: Any, user: User) -> str:
    """以最小选择集走真实装配；知识库可用性由 assemble 自己去查。"""
    assembled = await capabilities_module.assemble(
        db,
        user,
        attachment_ids=[],
        skill_ids=[],
        mcp_server_ids=[],
        mcp_tool_ids=[],
    )
    return assembled.system_prompt


# --------------------------------------------------------------- system/info


async def test_system_info_reports_knowledge_true(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get(INFO_URL, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"]["knowledge"] is True


async def test_system_info_reports_knowledge_false_when_disabled(
    client: httpx.AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    reset_settings_cache()
    try:
        response = await client.get(INFO_URL, headers=auth)
    finally:
        reset_settings_cache()
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"]["knowledge"] is False


# ------------------------------------------------------- knowledge_categories

async def test_disabled_knowledge_categories_rejected_with_403(
    client: httpx.AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    reset_settings_cache()
    try:
        response = await client.post(
            STREAM_URL,
            json={"message": "退货要保留吊牌吗？", "knowledge_categories": ["售后"]},
            headers=auth,
        )
    finally:
        reset_settings_cache()
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "capability_disabled"
    # 旧实现固定抛「知识库检索将在 M4 提供」；开关关闭时应说明能力未启用。
    assert "知识库" in error["message"]
    assert "M4" not in error["message"]


async def test_enabled_knowledge_categories_stream_starts(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """开启时 200 开流并跑到正常终态，而不是 403。"""
    _, events = await collect_sse(
        client,
        headers=auth,
        payload={"message": "退货要保留吊牌吗？", "knowledge_categories": ["售后"]},
    )
    names = [name for name, _ in events]
    assert "error" not in names, events
    assert "message.done" in names


async def test_disabled_knowledge_tool_cannot_retrieve_or_cite(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    seeded_docs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭能力后模型强行调用 search_knowledge 也不得检索、不得产生引用。

    使用真实 ready 文档（``seeded_docs``），确保「没有引用」只可能来自执行期门禁，
    而不是库为空；脚本化模型证明模型确实发起了工具调用。请求本身不带
    ``knowledge_categories``，因此不经过请求层 403，测的正是执行期兜底。
    """
    model = make_scripted_model(
        [
            {"tool_calls": [{"name": "search_knowledge", "args": {"query": "退货政策"}}]},
            {"text": "知识库能力未启用，无法检索。"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    reset_settings_cache()
    try:
        _, events = await collect_sse(
            client, headers=auth, payload={"message": "检索退货政策"}
        )
    finally:
        reset_settings_cache()

    names = [name for name, _ in events]
    assert "error" not in names, events
    assert "message.done" in names

    tool_result = next(data for name, data in events if name == "tool.result")
    assert tool_result["tool"] == "search_knowledge"
    assert tool_result["status"] == "ok"
    assert tool_result["summary"] == "能力未启用"
    assert not tool_result.get("citations")

    done = next(data for name, data in events if name == "message.done")
    assert done["citations"] == []

    tool_message = next(item for item in model.captured[-1] if isinstance(item, ToolMessage))
    assert "知识库能力未启用" in tool_message.content


async def test_over_limit_knowledge_categories_returns_422(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """默认上限 5：6 个分类在请求校验阶段 422。"""
    response = await client.post(
        STREAM_URL,
        json={"message": "hi", "knowledge_categories": [f"c{i}" for i in range(6)]},
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_over_limit_configured_lower_value_returns_422(
    client: httpx.AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限调低到 2 时，3 个分类同样 422，证明读的是配置而不是写死的 5。"""
    monkeypatch.setenv("KNOWLEDGE_CATEGORIES_PER_MESSAGE", "2")
    reset_settings_cache()
    try:
        response = await client.post(
            STREAM_URL,
            json={"message": "hi", "knowledge_categories": ["a", "b", "c"]},
            headers=auth,
        )
    finally:
        reset_settings_cache()
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_configured_limit_is_honoured(
    client: httpx.AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限读 settings：配置 8 时 6 个分类应当放行（默认 5 会误拒）。"""
    monkeypatch.setenv("KNOWLEDGE_CATEGORIES_PER_MESSAGE", "8")
    reset_settings_cache()
    try:
        _, events = await collect_sse(
            client,
            headers=auth,
            payload={"message": "hi", "knowledge_categories": [f"c{i}" for i in range(6)]},
        )
    finally:
        reset_settings_cache()
    names = [name for name, _ in events]
    assert "error" not in names, events
    assert "message.done" in names


# ------------------------------------------------------------- 系统提示词装配


def test_assemble_system_prompt_knowledge_flag() -> None:
    """纯函数层：显式标志决定是否插入整段规则。"""
    assert KNOWLEDGE_SECTION in assemble_system_prompt(knowledge_available=True)
    assert "## 知识库使用规则" not in assemble_system_prompt()


async def test_prompt_includes_knowledge_rules_when_ready_doc_visible(
    db: Any, user: User, seeded_docs: dict[str, Any]
) -> None:
    prompt = await _assemble_prompt(db, user)
    assert KNOWLEDGE_SECTION in prompt
    # 规则必须出现在「工具使用规则」之前、基础规则之后，保持稳定分层。
    assert prompt.index("基本规则") < prompt.index("## 知识库使用规则")
    assert prompt.index("## 知识库使用规则") < prompt.index("## 工具使用规则")


async def test_prompt_omits_knowledge_rules_when_kb_empty(db: Any, user: User) -> None:
    prompt = await _assemble_prompt(db, user)
    assert "## 知识库使用规则" not in prompt


async def test_prompt_omits_knowledge_rules_when_disabled(
    db: Any,
    user: User,
    seeded_docs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWLEDGE_ENABLED", "false")
    reset_settings_cache()
    try:
        prompt = await _assemble_prompt(db, user)
    finally:
        reset_settings_cache()
    assert "## 知识库使用规则" not in prompt
