"""OpenTelemetry 初始化与 span 辅助。"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import telemetry
from app.core.config import Settings


def test_disabled_is_noop() -> None:
    telemetry._set_provider_for_tests(None)
    telemetry.configure_tracing(Settings(otel_enabled=False, _env_file=None))
    with telemetry.start_span("x") as span:
        span.set_attribute("a", 1)
    assert telemetry.current_trace_id() == "-"
    assert telemetry.current_span_id() == "-"


def test_in_memory_span_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._set_provider_for_tests(provider)
    try:
        with telemetry.start_span("chat.run", attributes={"app.run_id": "r1"}) as span:
            span.set_attribute("app.status", "ok")
            assert telemetry.current_trace_id() != "-"
        spans = exporter.get_finished_spans()
        assert spans[0].name == "chat.run"
        assert spans[0].attributes["app.run_id"] == "r1"
        assert spans[0].attributes["app.status"] == "ok"
        assert telemetry.current_trace_id() == "-"
    finally:
        telemetry._set_provider_for_tests(None)


def test_invalid_exporter_degrades_to_noop(monkeypatch) -> None:
    telemetry._set_provider_for_tests(None)
    settings = Settings(
        otel_enabled=True,
        otel_exporter="otlp",
        otel_exporter_otlp_traces_endpoint="not-a-url",
        _env_file=None,
    )
    telemetry.configure_tracing(settings)
    # 构造 exporter 失败不应抛错；应退化为 no-op。
    with telemetry.start_span("x") as span:
        span.set_attribute("a", 1)
    assert telemetry.current_trace_id() == "-"
