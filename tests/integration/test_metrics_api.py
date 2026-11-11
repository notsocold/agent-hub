"""`/metrics` 端点契约。"""

from __future__ import annotations

import httpx


async def test_metrics_endpoint_exposes_build_info(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "agent_hub_build_info" in response.text
    assert "agent_hub_http_requests_total" in response.text


async def test_metrics_token_required(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "metrics_token", "secret-token")
    unauthorized = await client.get("/metrics")
    assert unauthorized.status_code == 401
    authorized = await client.get(
        "/metrics", headers={"Authorization": "Bearer secret-token"}
    )
    assert authorized.status_code == 200
