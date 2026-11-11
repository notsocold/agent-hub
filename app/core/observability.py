"""HTTP 请求的 trace、指标与 SSE 连接计数。

纯 ASGI 中间件：不缓冲响应体，SSE 可以边生成边下发；``send`` 包装只观察
``http.response.start`` / ``http.response.body``，不改变消息内容。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry.trace import SpanKind, Status, StatusCode, use_span

from app.core.errors import request_id_ctx
from app.core.metrics import (
    record_http_request,
    record_sse_connection,
)
from app.core.telemetry import (
    current_trace_id,
    extract_trace_context,
    get_tracer,
)

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


def _headers_to_map(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1") for key, value in headers
    }


def _route_of(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class ObservabilityMiddleware:
    """为每个 HTTP 请求开 span、记指标，并维护 SSE 活跃连接数。"""

    def __init__(self, app: Callable[..., Awaitable[None]], *, metrics_path: str = "/metrics"):
        self.app = app
        self.metrics_path = metrics_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        headers = _headers_to_map(scope.get("headers") or [])
        start = time.monotonic()
        status_code = 500
        sse_counted = False
        response_started = False

        span = get_tracer().start_span(
            "http.request",
            kind=SpanKind.SERVER,
            context=extract_trace_context(headers),
            attributes={
                "http.request.method": method,
                "url.path": path,
            },
        )

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, sse_counted, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                response_headers = list(message.get("headers") or [])
                response_map = _headers_to_map(response_headers)
                trace_id = current_trace_id()
                if trace_id != "-":
                    response_headers = [
                        (key, value)
                        for key, value in response_headers
                        if key.lower() != b"x-trace-id"
                    ]
                    response_headers.append((b"x-trace-id", trace_id.encode("latin-1")))
                    message["headers"] = response_headers
                    response_map["x-trace-id"] = trace_id

                content_type = response_map.get("content-type", "")
                is_sse = content_type.startswith("text/event-stream")
                route = _route_of(scope)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                span.set_attribute(
                    "app.request_id",
                    response_map.get("x-request-id") or request_id_ctx.get() or "-",
                )
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))

                if path != self.metrics_path:
                    record_http_request(
                        method,
                        route,
                        status_code,
                        time.monotonic() - start,
                        record_duration=not is_sse,
                    )
                if is_sse:
                    record_sse_connection(1)
                    sse_counted = True

            elif message["type"] == "http.response.body":
                if not message.get("more_body", False) and sse_counted:
                    record_sse_connection(-1)
                    sse_counted = False
            await send(message)

        try:
            with use_span(span, end_on_exit=False):
                await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            if sse_counted:
                record_sse_connection(-1)
            if not response_started:
                span.set_attribute("http.response.status_code", 500)
            span.end()


__all__ = ["ObservabilityMiddleware"]
