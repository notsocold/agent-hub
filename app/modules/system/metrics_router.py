"""Prometheus 指标端点。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.core.config import get_settings
from app.core.errors import UnauthorizedError
from app.core.metrics import render_metrics

router = APIRouter(tags=["系统"])


async def metrics_endpoint(request: Request) -> Response:
    settings = get_settings()
    if settings.metrics_token:
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {settings.metrics_token}"
        if not hmac.compare_digest(header, expected):
            raise UnauthorizedError("指标端点需要认证")
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


__all__ = ["metrics_endpoint", "router"]
