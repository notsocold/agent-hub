"""统一错误信封与领域异常。

HTTP 边界只做一件事：把 ``DomainError`` 转成
``{"error": {"code", "message", "request_id"}}``，避免各模块自己拼响应体。
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# 每个请求一个 ID，错误响应、日志与响应头共用。
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    return request_id_ctx.get() or "unknown"


def request_id_from(request: Request) -> str:
    """读取当前请求 ID。

    ``contextvar`` 在正常请求链路中最快，但未处理异常会越过
    ``RequestContextMiddleware``（其 ``finally`` 已复位 contextvar）后才由最外层
    ``ServerErrorMiddleware`` 处理，此时只能从 ``request.state`` 取。
    """
    value = getattr(request.state, "request_id", "")
    return value or current_request_id()


class DomainError(Exception):
    """可预期的业务异常，必须由 HTTP 层统一转换。"""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}

    def to_envelope(self, request_id: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id or current_request_id(),
        }
        # details 是向后兼容的扩展字段：前端契约只读 code/message/request_id，
        # 需要携带 run_id 等结构化信息时放在 details 里，避免破坏信封形状。
        if self.details:
            error["details"] = self.details
        return {"error": error}


class BadRequestError(DomainError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(DomainError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(DomainError):
    status_code = 403
    code = "forbidden"


class CapabilityDisabledError(DomainError):
    status_code = 403
    code = "capability_disabled"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class PayloadTooLargeError(DomainError):
    status_code = 413
    code = "payload_too_large"


class ValidationFailedError(DomainError):
    status_code = 422
    code = "validation_error"


class RateLimitedError(DomainError):
    status_code = 429
    code = "rate_limited"


class ServiceUnavailableError(DomainError):
    status_code = 503
    code = "service_unavailable"


class RunInProgressError(ConflictError):
    code = "run_in_progress"

    def __init__(self, run_id: str) -> None:
        super().__init__(
            "该会话已有运行中的请求，请等待完成或先停止当前运行",
            details={"run_id": run_id},
        )


class RunNotCancelableError(ConflictError):
    code = "run_not_cancelable"

    def __init__(self, status: str) -> None:
        super().__init__("当前运行已结束，无法取消", details={"status": status})


class ApprovalAlreadyDecidedError(ConflictError):
    code = "approval_already_decided"


class ApprovalExpiredError(ConflictError):
    code = "approval_expired"


def _envelope(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id or current_request_id(),
    }
    if details:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


def _validation_message(exc: RequestValidationError) -> str:
    """把 FastAPI 的字段错误压缩成一句可读的中文提示。"""
    errors = exc.errors()
    if not errors:
        return "请求参数不合法"
    first = errors[0]
    location = ".".join(str(item) for item in first.get("loc", ()) if item != "body")
    reason = first.get("msg", "参数不合法")
    return f"请求参数不合法：{location} {reason}".strip()


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理；所有 4xx/5xx 都返回统一信封。"""

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        request_id = request_id_from(request)
        if exc.status_code >= 500:
            logger.exception("业务异常：%s", exc.message, extra={"request_id": request_id})
        else:
            logger.info(
                "业务异常：%s code=%s", exc.message, exc.code, extra={"request_id": request_id}
            )
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_envelope(request_id=request_id)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _envelope(
            422,
            "validation_error",
            _validation_message(exc),
            request_id=request_id_from(request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code_map = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            422: "validation_error",
            429: "rate_limited",
            503: "service_unavailable",
        }
        code = code_map.get(exc.status_code, "http_error")
        message = str(exc.detail) if exc.detail else "请求失败"
        return _envelope(exc.status_code, code, message, request_id=request_id_from(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # ServerErrorMiddleware 位于最外层，RequestContextMiddleware 无法再给这个
        # 响应补 X-Request-ID；这里显式回填，保证前端上报的 ID 与后端日志一致。
        request_id = request_id_from(request)
        if request_id == "unknown":
            request_id = uuid.uuid4().hex
        logger.exception("未处理异常：%s", exc, extra={"request_id": request_id})
        response = _envelope(
            500,
            "internal_error",
            "服务器内部错误，请稍后重试",
            request_id=request_id,
        )
        response.headers["X-Request-ID"] = request_id
        return response
