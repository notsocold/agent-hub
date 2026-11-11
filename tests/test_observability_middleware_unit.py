"""HTTP/SSE 观测中间件。"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.core import metrics
from app.core.middleware import RequestContextMiddleware
from app.core.observability import ObservabilityMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics_path="/metrics")
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sse")
    async def sse() -> StreamingResponse:
        async def gen():
            yield "data: one\n\n"
            yield "data: two\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


async def test_http_metric_and_request_id() -> None:
    metrics.reset_metrics()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/ping", headers={"X-Request-ID": "req-9"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-9"
    text = metrics.render_metrics()[0].decode()
    assert 'route="/ping"' in text
    assert 'status="200"' in text
    assert "agent_hub_http_request_duration_seconds" in text


async def test_sse_connection_gauge_returns_to_zero() -> None:
    metrics.reset_metrics()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        async with client.stream("GET", "/sse") as response:
            assert response.status_code == 200
            body = "".join([line async for line in response.aiter_lines()])
    assert "one" in body and "two" in body
    text = metrics.render_metrics()[0].decode()
    assert "agent_hub_sse_connections_active 0.0" in text
    # SSE 不计入 HTTP 时长直方图（由 run 指标覆盖）。
    assert 'route="/sse"' in text
    assert "agent_hub_http_requests_total{" in text
