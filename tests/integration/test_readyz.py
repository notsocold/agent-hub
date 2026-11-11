"""健康检查与就绪检查。"""

from __future__ import annotations

import httpx

from app.main import app


async def test_readyz_components(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["draining"] is False
    names = {item["name"] for item in body["components"]}
    assert {"database", "migrations", "checkpoint", "storage"} <= names


async def test_healthz_is_liveness_only(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


async def test_readyz_draining_503(client: httpx.AsyncClient) -> None:
    app.state.draining = True
    try:
        response = await client.get("/readyz")
    finally:
        app.state.draining = False
    assert response.status_code == 503
    assert response.json()["draining"] is True


async def test_readyz_database_failure_503(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    from app.modules.system import health

    async def _boom(timeout: float):
        return {"name": "database", "status": "error", "detail": "down"}

    monkeypatch.setattr(health, "_check_database", _boom)
    response = await client.get("/readyz")
    assert response.status_code == 503
    database = next(item for item in response.json()["components"] if item["name"] == "database")
    assert database["status"] == "error"


async def test_readyz_migrations_enabled_reports_missing(
    client: httpx.AsyncClient, monkeypatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "readiness_check_migrations", True)
    response = await client.get("/readyz")
    assert response.status_code == 503
    migrations = next(
        item for item in response.json()["components"] if item["name"] == "migrations"
    )
    assert migrations["status"] == "error"
