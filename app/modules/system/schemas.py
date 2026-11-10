"""系统信息 DTO。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ModelCapabilitiesOut(BaseModel):
    streaming: bool
    reasoning: bool
    tools: bool


class ModelInfoOut(BaseModel):
    id: str
    name: str
    description: str
    capabilities: ModelCapabilitiesOut


class ReasoningEffortOptionOut(BaseModel):
    id: Literal["low", "medium", "high"]
    name: str
    description: str


class SystemCapabilitiesOut(BaseModel):
    instructions: bool
    memories: bool
    skills: bool
    mcp: bool
    knowledge: bool
    attachments: bool
    workflows: bool
    approvals: bool
    # 前端导航门禁：M1.5 未实现智能体与任务页，显式返回 false 供页面置灰。
    agents: bool = Field(
        default=False,
        description="远程智能体能力是否启用；M2 前恒为 false。该键始终返回，前端按可选布尔消费",
    )
    tasks: bool = Field(
        default=False,
        description="A2A 任务能力是否启用；M2 前恒为 false。该键始终返回，前端按可选布尔消费",
    )


class SystemLimitsOut(BaseModel):
    skills_per_message: int
    mcp_servers_per_message: int
    mcp_tools_per_message: int
    knowledge_categories_per_message: int
    attachments_per_message: int
    attachment_max_bytes: int
    message_max_chars: int


class SystemA2aInfoOut(BaseModel):
    """A2A 接入信息。

    2026-11-10 需求变更：本服务只做 A2A 出站 Client，不提供 Agent Card 与
    JSON-RPC 入站端点。五个字段保留只为兼容前端契约：``enabled`` 恒 false，
    ``card_url`` / ``jsonrpc_url`` 恒为空字符串。
    """

    enabled: bool = Field(description="本服务是否提供 A2A 入站；恒为 false")
    card_url: str = Field(description="入站 Agent Card 地址；不提供入站时为空")
    jsonrpc_url: str = Field(description="入站 JSON-RPC 地址；不提供入站时为空")
    auth_type: str = Field(description="入站鉴权方式；不提供入站时恒为 none")
    card_security: str = Field(description="Agent Card 安全声明；不提供入站时恒为 none")


class EmbeddingInfoOut(BaseModel):
    model: str
    dim: int


class GenerationInfoOut(BaseModel):
    temperature: float
    timeout_seconds: int
    max_output_tokens: int


class SystemInfoOut(BaseModel):
    version: str
    build: str
    environment: str
    models: list[ModelInfoOut]
    default_model: str
    reasoning_efforts: list[ReasoningEffortOptionOut]
    default_reasoning_effort: Literal["low", "medium", "high"]
    embedding: EmbeddingInfoOut
    generation: GenerationInfoOut
    a2a: SystemA2aInfoOut
    capabilities: SystemCapabilitiesOut
    limits: SystemLimitsOut
