"""请求上下文中间件：生成/透传 X-Request-ID。"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.errors import request_id_ctx


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        # 同时写进 request.state：未处理异常会越过本中间件到达最外层
        # ServerErrorMiddleware，此时 contextvar 已随 finally 复位，错误处理器
        # 只能靠 state 拿到原始请求 ID（否则 500 响应会退化成 "unknown"）。
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response
