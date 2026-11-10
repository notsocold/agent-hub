"""M4 聊天集成测试：search_knowledge 工具、引用事件与引用落库。

假模型 ``FakeStreamingChatModel`` 不会发起工具调用，所以本文件只替换
``app.modules.chat.service.build_chat_model`` 这一个接缝，换成 ``ScriptedToolModel``
（tests/helpers.py）；HTTP/SSE、图循环、ToolRunner、终态与落库全部真实执行。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.core.security import create_access_token
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.chat import service as chat_service
from app.modules.chat.bus import bus
from app.modules.chat.events import RunEventPublisher
from app.modules.chat.tools import ToolRunner
from tests.conftest import auth_headers, collect_sse
from tests.helpers import make_scripted_model

# 前端冻结的 Citation 形状（spec §4.2）：字段必须精确一致。
CITATION_KEYS = {
    "index",
    "document_id",
    "chunk_id",
    "title",
    "category",
    "score",
    "updated_at",
}


@pytest.fixture
def auth(user: User) -> dict[str, str]:
    """与 conftest 的 ``admin_auth`` 同口径：为夹具用户直接签发 access token。"""
    return auth_headers(create_access_token(user.id).token)


@pytest.fixture
def seeded_kb(seeded_docs: dict[str, Any]) -> dict[str, Any]:
    """任务私有命名（Ruling R6）：知识库数据复用 conftest 的 ``seeded_docs``。"""
    return seeded_docs


def _search_model(queries: list[str], *, answer: str = "根据知识库回答 [1]。"):
    """脚本化模型：第一轮发起一次或多次 search_knowledge，第二轮给出最终文本。"""
    steps: list[dict[str, Any]] = [
        {
            "tool_calls": [
                {"name": "search_knowledge", "args": {"query": query}} for query in queries
            ]
        },
        {"text": answer},
    ]
    return make_scripted_model(steps)


def _tool_message(model: Any) -> ToolMessage:
    """取最后一轮模型输入里的 ToolMessage：即工具真正返回给模型的内容。"""
    message = next(item for item in model.captured[-1] if isinstance(item, ToolMessage))
    return message


async def test_tool_call_emits_citations(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_kb: dict[str, Any], monkeypatch
) -> None:
    """工具调用后 tool.result 与 message.done 都带引用，且 index 从 1 开始。"""
    model = _search_model(["退货要吊牌吗"])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "退货要吊牌吗？"})

    tool_result = next(data for name, data in events if name == "tool.result")
    assert tool_result["status"] == "ok"
    assert tool_result["citations"]
    assert tool_result["citations"][0]["index"] == 1
    assert set(tool_result["citations"][0]) == CITATION_KEYS

    done = next(data for name, data in events if name == "message.done")
    assert done["citations"]
    assert [item["index"] for item in done["citations"]] == list(
        range(1, len(done["citations"]) + 1)
    )
    assert set(done["citations"][0]) == CITATION_KEYS

    # 工具文本格式：[n] heading（filename / category / score）+ 换行正文。
    content = _tool_message(model).content
    assert content.startswith("[1] ")
    assert "（return-policy.md / 售后 / " in content
    assert "\n\n[2] " in content


async def test_citations_persisted(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_kb: dict[str, Any], monkeypatch
) -> None:
    """刷新后仍可见：GET /conversations/{id}/messages 的助手消息 citations 非空。"""
    model = _search_model(["退款时效"])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "退款多久到账？"})
    done = next(data for name, data in events if name == "message.done")

    response = await client.get(
        f"/api/v1/conversations/{done['conversation_id']}/messages", headers=auth
    )
    assert response.status_code == 200
    assistant = next(item for item in response.json()["items"] if item["role"] == "assistant")
    assert assistant["citations"]
    assert set(assistant["citations"][0]) == CITATION_KEYS
    assert [item["index"] for item in assistant["citations"]] == [
        item["index"] for item in done["citations"]
    ]
    assert [item["chunk_id"] for item in assistant["citations"]] == [
        item["chunk_id"] for item in done["citations"]
    ]


async def test_citation_indexes_contiguous_and_unique(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_kb: dict[str, Any], monkeypatch
) -> None:
    """两次检索（假向量下命中集合完全重合）去重：chunk_id 唯一、index 连续、按分数降序。"""
    model = _search_model(["退货 吊牌", "退款 时效"])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "退货与退款政策？"})

    results = [data for name, data in events if name == "tool.result"]
    assert len(results) == 2
    assert all(result["citations"] for result in results)

    done = next(data for name, data in events if name == "message.done")
    citations = done["citations"]
    chunk_ids = [item["chunk_id"] for item in citations]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert [item["index"] for item in citations] == list(range(1, len(citations) + 1))
    scores = [item["score"] for item in citations]
    assert scores == sorted(scores, reverse=True)
    # 第二次 tool.result 是整轮累计视图，与 message.done 一致。
    assert chunk_ids == [item["chunk_id"] for item in results[-1]["citations"]]


async def test_search_failure_keeps_turn_alive(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_kb: dict[str, Any], monkeypatch
) -> None:
    """检索抛错时工具返回中文错误串，整轮仍然成功收尾（无 500、流不中断）。"""

    async def broken_search(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("向量检索不可用")

    monkeypatch.setattr("app.modules.knowledge.service.search", broken_search)
    model = _search_model(["退货政策"])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "退货政策？"})

    names = [name for name, _ in events]
    assert "error" not in names
    assert names[-1] == "message.done"
    done = events[-1][1]
    assert done["finish_reason"] == "stop"
    assert done["citations"] == []

    result = next(data for name, data in events if name == "tool.result")
    assert result["status"] == "ok"
    assert "检索失败" in result["summary"]
    # 错误作为 ToolMessage 返回模型，而不是打断运行。
    assert "检索失败" in _tool_message(model).content


async def test_empty_knowledge_base_returns_not_found(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    fake_embedding: Any,
    monkeypatch,
) -> None:
    """库内无文档：工具返回中文「未找到」串，整轮正常完成且无引用。"""
    model = _search_model(["退货政策"])
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "退货政策？"})

    result = next(data for name, data in events if name == "tool.result")
    assert result["status"] == "ok"
    assert "未找到" in result["summary"]
    assert "citations" not in result
    assert _tool_message(model).content == "知识库中未找到相关内容"

    done = next(data for name, data in events if name == "message.done")
    assert done["finish_reason"] == "stop"
    assert done["citations"] == []


async def test_tool_exception_does_not_break_turn(
    client: httpx.AsyncClient, auth: dict[str, str], monkeypatch
) -> None:
    """任何工具抛错只落 tool.result(status=error)，整轮继续收尾。"""
    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {
                        "name": "read_skill_file",
                        "args": {"skill_id": "not-a-uuid", "path": "x.md"},
                    }
                ]
            },
            {"text": "没有可读的技能文件。"},
        ]
    )
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "读技能文件"})

    names = [name for name, _ in events]
    assert "error" not in names
    assert names[-1] == "message.done"
    result = next(data for name, data in events if name == "tool.result")
    assert result["status"] == "error"
    assert "citations" not in result


class _FailOnSecondRoundModel(BaseChatModel):
    """第一轮发起检索、第二轮抛错：验证失败终态仍把引用写进助手消息。"""

    model_name: str = "fail-after-tool"
    captured: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "agent-hub-fail-after-tool"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _FailOnSecondRoundModel:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.captured.append(list(messages))
        if not any(isinstance(item, ToolMessage) for item in messages):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_knowledge",
                        "args": {"query": "退货"},
                        "id": "call_kb_1",
                    }
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        raise RuntimeError("模型第二次调用失败")


async def test_failed_run_still_persists_citations(
    client: httpx.AsyncClient, auth: dict[str, str], seeded_kb: dict[str, Any], monkeypatch
) -> None:
    """_finalize_failed 只发 error，但必须把已收集的引用落库。"""
    model = _FailOnSecondRoundModel()
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)

    _, events = await collect_sse(client, headers=auth, payload={"message": "退货政策？"})
    names = [name for name, _ in events]
    assert "error" in names
    assert "message.done" not in names

    conversation_id = next(
        data["conversation_id"] for name, data in events if name == "run.started"
    )
    response = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=auth)
    assistant = next(item for item in response.json()["items"] if item["role"] == "assistant")
    assert assistant["status"] == "failed"
    assert assistant["citations"]
    assert assistant["citations"][0]["index"] == 1


async def test_tool_default_category_filter_and_explicit_override(
    user: User, seeded_kb: dict[str, Any]
) -> None:
    """分类优先级的补充覆盖（工具层）：默认取本轮 knowledge_categories，显式参数优先。

    请求级 ``knowledge_categories`` 已由 Task 11 放开（默认开启），但这里仍只直接
    驱动工具，避免依赖提示词/HTTP 装配；上面的用例已经证明工具在真实聊天链路里工作。
    """
    from app.modules.chat.tools import build_search_knowledge_tool

    runner = ToolRunner(
        run_id=uuid.uuid4(),
        publisher=RunEventPublisher(uuid.uuid4(), get_session_factory(), bus),
        user_id=user.id,
        user_role=user.role,
        mcp_tools={},
        active_skill_ids=set(),
        citations=[],
        knowledge_categories=["售后"],
    )
    tool = build_search_knowledge_tool(runner)

    await tool.ainvoke({"query": "退货"})
    assert runner.citations
    assert {item["category"] for item in runner.citations} == {"售后"}

    # 工具显式传参优先于请求的默认分类。
    runner.citations[:] = []
    await tool.ainvoke({"query": "发货", "categories": ["物流"]})
    assert runner.citations
    assert {item["category"] for item in runner.citations} == {"物流"}
