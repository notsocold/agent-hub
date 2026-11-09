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
    base_url = settings.a2a_public_url.rstrip("/")
    agent_path = "/" + settings.a2a_agent_path.strip("/")
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
        a2a=SystemA2aInfoOut(
            enabled=settings.a2a_enabled,
            card_url=f"{base_url}{agent_path}/.well-known/agent-card.json",
            jsonrpc_url=f"{base_url}{agent_path}",
            auth_type="none",
            card_security="none",
        ),
        capabilities=SystemCapabilitiesOut(
            instructions=False,
            memories=False,
            skills=False,
            mcp=False,
            knowledge=False,
            attachments=False,
            workflows=False,
            approvals=True,
            agents=False,
            tasks=False,
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
