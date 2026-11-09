"""系统信息 DTO。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


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
    # 前端导航门禁：M1 未实现智能体与任务页，显式返回 false 供页面置灰。
    agents: bool = False
    tasks: bool = False


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
