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
    enabled: bool
    card_url: str
    jsonrpc_url: str
    auth_type: str
    card_security: str


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
