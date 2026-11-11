"""聊天链路的指标与 span 接入。"""

from __future__ import annotations

import uuid

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import metrics, telemetry
from app.modules.chat import service as chat_service
from tests.conftest import auth_headers, collect_sse, register_user
from tests.helpers import make_scripted_model


async def test_chat_metrics_and_spans(client: httpx.AsyncClient, monkeypatch) -> None:
    metrics.reset_metrics()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._set_provider_for_tests(provider)
    try:
        monkeypatch.setattr(
            chat_service,
            "build_chat_model",
            lambda settings: make_scripted_model(
                [
                    {
                        "tool_calls": [
                            {
                                "name": "save_memory",
                                "args": {"title": "偏好", "content": "喜欢简洁回答"},
                            }
                        ]
                    },
                    {"text": "记住了"},
                ]
            ),
        )
        registered = await register_user(client)
        headers = auth_headers(registered["access_token"])
        await collect_sse(client, headers=headers, payload={"message": "记住我的偏好"})
    finally:
        telemetry._set_provider_for_tests(None)

    text = metrics.render_metrics()[0].decode()
    assert 'agent_hub_chat_runs_total{status="succeeded"} 1.0' in text
    assert 'agent_hub_sse_events_total{event="message.delta"}' in text
    assert 'agent_hub_tool_invocations_total{source="native"' in text
    assert 'tool="save_memory"' in text
    assert "agent_hub_llm_tokens_total" in text

    spans = exporter.get_finished_spans()
    span_names = {span.name for span in spans}
    assert "chat.run" in span_names
    assert "tool.execute" in span_names
    assert "llm.chat" in span_names
    chat_span = next(span for span in spans if span.name == "chat.run")
    tool_span = next(span for span in spans if span.name == "tool.execute")
    assert tool_span.parent is not None
    assert tool_span.context.trace_id == chat_span.context.trace_id


async def test_stream_events_do_not_leak_metrics_labels(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await collect_sse(client, headers=headers, payload={"message": "hi"})
    text = metrics.render_metrics()[0].decode()
    # 用户 ID / run ID / 会话 ID 不得成为指标标签。
    assert registered["user"]["id"] not in text


class _SplitSecretModel(BaseChatModel):
    """把密钥拆成两个 chunk 的假流式模型，用于验证 SSE 实时打码。"""

    model_name: str = "split-secret"

    @property
    def _llm_type(self) -> str:
        return "split-secret"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        for piece in ("前缀 sk-abcdefghij", "klmnopqrstuvwxyz 后缀"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


async def test_split_secret_redacted_in_sse_and_storage(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    from sqlalchemy import select

    from app.db.models.conversation import Message
    from app.db.session import get_session_factory

    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: _SplitSecretModel())
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    run_id, events = await collect_sse(client, headers=headers, payload={"message": "输出密钥"})
    deltas = "".join(
        data.get("delta", "") for name, data in events if name == "message.delta"
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in deltas
    assert "***" in deltas
    async with get_session_factory()() as session:
        assistant = await session.scalar(
            select(Message).where(Message.run_id == uuid.UUID(run_id), Message.role == "assistant")
        )
        assert assistant is not None
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in assistant.content
        assert "***" in assistant.content
