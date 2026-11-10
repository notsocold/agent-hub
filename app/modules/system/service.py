"""系统信息服务。"""

from __future__ import annotations

from app import __version__
from app.core.config import Settings, get_settings
from app.modules.system.schemas import (
    EmbeddingInfoOut,
    GenerationInfoOut,
    ModelCapabilitiesOut,
    ModelInfoOut,
    ReasoningEffortOptionOut,
    SystemA2aInfoOut,
    SystemCapabilitiesOut,
    SystemInfoOut,
    SystemLimitsOut,
)

_REASONING_EFFORTS = [
    ReasoningEffortOptionOut(id="low", name="标准", description="更快，适合日常"),
    ReasoningEffortOptionOut(id="medium", name="深度", description="平衡速度与推理"),
    ReasoningEffortOptionOut(id="high", name="最大", description="更深入，耗时更长"),
]


def build_system_info(settings: Settings | None = None) -> SystemInfoOut:
    settings = settings or get_settings()
    models = [
        ModelInfoOut(
            id=model,
            name=model,
            description="" if model != settings.llm_model else "默认对话模型",
            capabilities=ModelCapabilitiesOut(
                streaming=True,
                reasoning=settings.llm_reasoning_effort_supported,
                tools=True,
            ),
        )
        for model in settings.allowed_models
    ]
    return SystemInfoOut(
        version=__version__,
        build=settings.build_label,
        environment=settings.env,
        models=models,
        default_model=settings.llm_model,
        reasoning_efforts=(_REASONING_EFFORTS if settings.llm_reasoning_effort_supported else []),
        default_reasoning_effort="low",
        embedding=EmbeddingInfoOut(model=settings.embedding_model, dim=settings.embedding_dim),
        generation=GenerationInfoOut(
            temperature=settings.llm_temperature,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
        ),
        # 本服务只做 A2A 出站 Client（2026-11-10 需求变更）：不发布 Agent Card、
        # 不暴露 JSON-RPC 入站端点。五个字段保留只为不破坏前端契约，取值恒为“无入站”。
        a2a=SystemA2aInfoOut(
            enabled=False,
            card_url="",
            jsonrpc_url="",
            auth_type="none",
            card_security="none",
        ),
        capabilities=SystemCapabilitiesOut(
            instructions=settings.instructions_enabled,
            memories=settings.memories_enabled,
            skills=settings.skills_enabled,
            mcp=settings.mcp_enabled,
            knowledge=settings.knowledge_enabled,
            attachments=settings.attachments_enabled,
            workflows=False,
            approvals=settings.approvals_enabled,
            # M2 出站已实现：与入站 A2A_ENABLED 无关，始终为 true。
            agents=True,
            tasks=True,
        ),
        limits=SystemLimitsOut(
            skills_per_message=settings.skills_per_message,
            mcp_servers_per_message=settings.mcp_servers_per_message,
            mcp_tools_per_message=settings.mcp_tools_per_message,
            knowledge_categories_per_message=settings.knowledge_categories_per_message,
            attachments_per_message=settings.attachments_per_message,
            attachment_max_bytes=settings.attachment_max_bytes,
            message_max_chars=settings.message_max_chars,
        ),
    )
