"""聊天链路的指标与 span 接入。"""

from __future__ import annotations

import httpx
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

    span_names = {span.name for span in exporter.get_finished_spans()}
    assert "chat.run" in span_names
    assert "tool.execute" in span_names
    assert "llm.chat" in span_names


async def test_stream_events_do_not_leak_metrics_labels(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await collect_sse(client, headers=headers, payload={"message": "hi"})
    text = metrics.render_metrics()[0].decode()
    # 用户 ID / run ID / 会话 ID 不得成为指标标签。
    assert registered["user"]["id"] not in text
