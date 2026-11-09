"""OpenAPI 公共响应模型。

错误信封在运行时是手写的 dict（``app/core/errors.py``），这里只为契约文档
建模，保证前端能从 ``docs/openapi.json`` 生成出 ``error.details`` 等字段，
而不是只看到 ``{}``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBodyOut(BaseModel):
    code: str = Field(
        description=(
            "稳定的机器可读错误码。M1 实际会出现的值包括：validation_error、"
            "unauthorized、forbidden、not_found、capability_disabled、"
            "run_in_progress、run_not_cancelable、run_not_resumable、"
            "workflow_disabled、model_not_allowed、approval_already_decided、"
            "approval_expired、rate_limited、internal_error。"
        )
    )
    message: str = Field(description="面向用户的中文提示，前端可直接展示")
    request_id: str = Field(
        description="请求 ID，与响应头 X-Request-ID 一致；500 响应也会回填真实值"
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description=(
            "结构化补充信息，可能不存在。已知字段：run_in_progress.details.run_id、"
            "run_not_cancelable.details.status、rate_limited.details.retry_after_seconds"
        ),
    )


class ErrorEnvelopeOut(BaseModel):
    error: ErrorBodyOut
