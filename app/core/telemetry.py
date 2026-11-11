"""OpenTelemetry 初始化与 span 辅助。

设计口径（M6）：默认关闭；未配置/配置非法时退化为 no-op，绝不阻塞启动。
不使用全局 ``trace.set_tracer_provider``，而是持有模块级 provider，便于测试注入
``InMemorySpanExporter`` 且不污染其他用例。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode, format_span_id, format_trace_id

from app import __version__
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_provider: TracerProvider | None = None


def _valid_endpoint(endpoint: str) -> bool:
    return endpoint.startswith("http://") or endpoint.startswith("https://")


def _build_exporter(settings: Settings) -> SpanExporter:
    if settings.otel_exporter == "otlp":
        if not _valid_endpoint(settings.otel_exporter_otlp_traces_endpoint):
            raise ValueError("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT 必须是 http(s) URL")
        return OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_traces_endpoint,
            timeout=settings.otel_export_timeout_seconds,
        )
    return ConsoleSpanExporter()


def configure_tracing(settings: Settings | None = None) -> None:
    """按配置初始化 tracing；失败时只记 warning，服务以 no-op 继续运行。"""
    global _provider
    _provider = None
    settings = settings or get_settings()
    if not settings.otel_enabled or settings.otel_exporter == "none":
        return

    try:
        exporter = _build_exporter(settings)

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment": settings.env,
                "service.version": __version__,
                "service.instance.id": settings.app_build,
            }
        )
        provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        _provider = provider
        logger.info(
            "OpenTelemetry 已启用 exporter=%s service=%s ratio=%s",
            settings.otel_exporter,
            settings.otel_service_name,
            settings.otel_sample_ratio,
        )
    except Exception as exc:
        logger.warning("OpenTelemetry 初始化失败，降级为 no-op：%s", exc)
        _provider = None


def _set_provider_for_tests(provider: TracerProvider | None) -> None:
    """测试辅助：注入/清空 provider，不触碰全局 tracer provider。"""
    global _provider
    _provider = provider


def get_tracer(name: str = "agent_hub") -> trace.Tracer:
    if _provider is not None:
        return _provider.get_tracer(name)
    return trace.get_tracer(name)


@contextmanager
def start_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[trace.Span]:
    """开启一个当前 span；异常时记录 exception 并标记 ERROR。"""
    with get_tracer().start_as_current_span(
        name,
        kind=kind,
        attributes=dict(attributes or {}),
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise


def current_trace_id() -> str:
    span_context = trace.get_current_span().get_span_context()
    return format_trace_id(span_context.trace_id) if span_context.is_valid else "-"


def current_span_id() -> str:
    span_context = trace.get_current_span().get_span_context()
    return format_span_id(span_context.span_id) if span_context.is_valid else "-"


def extract_trace_context(headers: Mapping[str, str]) -> Context:
    """从 HTTP 请求头提取 W3C trace 上下文（traceparent/tracestate）。"""
    return extract(dict(headers))


async def shutdown_tracing(timeout: float = 5.0) -> None:
    global _provider
    provider = _provider
    _provider = None
    if provider is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(provider.shutdown), timeout=timeout)
    except Exception as exc:
        logger.warning("OpenTelemetry 关闭失败：%s", exc)


__all__ = [
    "configure_tracing",
    "current_span_id",
    "current_trace_id",
    "extract_trace_context",
    "get_tracer",
    "shutdown_tracing",
    "start_span",
]
