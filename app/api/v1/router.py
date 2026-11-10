"""``/api/v1`` 聚合路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.schemas import ErrorEnvelopeOut
from app.modules.agents.router import router as agents_router
from app.modules.approvals.router import router as approvals_router
from app.modules.attachments.router import router as attachments_router
from app.modules.auth.router import router as auth_router
from app.modules.chat.router import router as chat_router
from app.modules.conversations.router import router as conversations_router
from app.modules.instructions.router import router as instructions_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.mcp.router import router as mcp_router
from app.modules.memories.router import router as memories_router
from app.modules.skills.router import router as skills_router
from app.modules.system.router import router as system_router
from app.modules.tasks.router import router as tasks_router

# 所有业务端点共享的错误信封；集中声明，避免 OpenAPI 只有 2xx/422。
COMMON_ERROR_RESPONSES: dict[int | str, dict] = {
    400: {"model": ErrorEnvelopeOut, "description": "请求不合法（bad_request）"},
    401: {"model": ErrorEnvelopeOut, "description": "未认证、access token 过期或刷新令牌失效"},
    403: {
        "model": ErrorEnvelopeOut,
        "description": "无权限、账号停用，或能力未启用（capability_disabled / auth_flow_unavailable）",
    },
    404: {"model": ErrorEnvelopeOut, "description": "资源不存在（也用于隐藏他人资源）"},
    409: {
        "model": ErrorEnvelopeOut,
        "description": (
            "状态冲突：run_in_progress（details.run_id）、run_not_cancelable"
            "（details.status）、run_not_resumable、workflow_disabled、"
            "approval_already_decided、approval_expired"
        ),
    },
    413: {"model": ErrorEnvelopeOut, "description": "请求体过大（payload_too_large）"},
    422: {
        "model": ErrorEnvelopeOut,
        "description": "参数校验失败（validation_error）或模型不在白名单（model_not_allowed）",
    },
    429: {
        "model": ErrorEnvelopeOut,
        "description": "触发限流（rate_limited），details.retry_after_seconds 为建议重试秒数",
    },
    500: {"model": ErrorEnvelopeOut, "description": "服务器内部错误（internal_error）"},
    503: {
        "model": ErrorEnvelopeOut,
        "description": "依赖服务不可用（service_unavailable，如 Embedding 服务不可达）",
    },
}

api_router = APIRouter()
api_router.include_router(auth_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(conversations_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(chat_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(approvals_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(attachments_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(instructions_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(knowledge_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(memories_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(skills_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(mcp_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(agents_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(tasks_router, responses=COMMON_ERROR_RESPONSES)
api_router.include_router(system_router, responses=COMMON_ERROR_RESPONSES)
