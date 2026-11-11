"""数据库连接异常映射为 503。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.errors import register_exception_handlers


def test_operational_error_maps_to_503() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise OperationalError("SELECT 1", {}, Exception("down"))

    with TestClient(app) as client:
        response = client.get("/boom")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "database_unavailable"
    assert body["error"]["request_id"]
