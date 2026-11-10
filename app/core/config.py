"""应用配置。

所有配置项只从这里读取，业务代码禁止直接访问环境变量。
测试通过 ``get_settings.cache_clear()`` 后重新获取，保证配置可注入。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# 开发环境缺少 JWT_SECRET 时使用的兜底值，生产环境会拒绝启动。
_DEV_JWT_SECRET = "agent-hub-dev-insecure-secret-change-me"

# 占位/过短的 Embedding 密钥视为不可用：开发环境自动回退到假向量，避免请求必然失败。
_PLACEHOLDER_KEY = re.compile(r"^sk-(x+|your.*|test.*|placeholder.*)$", re.IGNORECASE)


def is_placeholder_api_key(key: str) -> bool:
    """判断密钥是否只是占位符（空、过短或典型示例值）。"""
    return len(key.strip()) < 20 or bool(_PLACEHOLDER_KEY.match(key.strip()))


class Settings(BaseSettings):
    """环境变量映射；字段名与 .env.example 保持一致（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ 应用
    app_name: str = "agent-hub"
    env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    api_prefix: str = "/api/v1"
    app_build: str = "dev"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ------------------------------------------------------------------ 数据库
    # 必须指向使用方本机已有的 PostgreSQL；不提供默认地址，避免误连 docker/旧实例。
    database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False
    db_pool_pre_ping: bool = True

    # LangGraph checkpoint：生产用 postgres，测试/无库环境用 memory
    checkpoint_backend: Literal["postgres", "memory"] = "postgres"
    checkpoint_database_url: str = ""
    checkpoint_schema: str = "langgraph"

    # ------------------------------------------------------------------ 模型
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_models: str = ""  # 逗号分隔的模型白名单，为空时仅 llm_model
    llm_temperature: float = 0.2
    llm_timeout_seconds: int = 60
    llm_max_output_tokens: int = 4096
    llm_reasoning_effort_supported: bool = False
    fake_llm_delay_ms: int = 15

    # ------------------------------------------------------------------ Embedding / 检索（M4 使用，先保留配置）
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    # auto：有可用密钥走 OpenAI 兼容接口，否则回退假向量；fake/openai 强制指定
    embedding_provider: Literal["auto", "fake", "openai"] = "auto"
    embedding_timeout_seconds: int = 30
    embedding_batch_size: int = 32
    vector_top_k: int = 6
    vector_score_threshold: float = 0.30

    # ------------------------------------------------------------------ 知识库 / 存储（M4）
    knowledge_enabled: bool = True
    knowledge_dir: Path = Path("data/knowledge")
    knowledge_max_upload_bytes: int = 10 * 1024 * 1024
    knowledge_chunk_size: int = 800
    knowledge_chunk_overlap: int = 80
    knowledge_max_chunks_per_document: int = 5000
    knowledge_max_pdf_pages: int = 500
    # local：本地磁盘；s3：任意 S3 兼容对象存储（endpoint_url 可指向 MinIO 等）
    storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "knowledge/"

    # ------------------------------------------------------------------ A2A（仅出站）
    # 2026-11-10 需求变更：本服务只做 A2A Client，不提供入站 Server。
    # A2A_ENABLED / A2A_PUBLIC_URL / A2A_AGENT_PATH 仅为历史部署配置兼容保留，
    # 不再参与任何运行逻辑，也不再生成 Agent Card；新部署无需设置。
    a2a_enabled: bool = False
    a2a_public_url: str = "http://localhost:8000"
    a2a_agent_path: str = "/a2a"
    a2a_client_timeout_seconds: int = 60
    a2a_allowed_hosts: str = ""
    # None 表示跟随环境：dev/test 允许回环（本地假 Agent 联调），prod 默认拒绝。
    a2a_allow_loopback: bool | None = None
    a2a_card_timeout_seconds: int = 10
    a2a_max_skills: int = 50
    a2a_skill_description_max_chars: int = 2000
    a2a_input_timeout_seconds: int = 30 * 60
    a2a_max_response_bytes: int = 1024 * 1024

    # ------------------------------------------------------------------ 能力总开关（M1.5）
    instructions_enabled: bool = True
    memories_enabled: bool = True
    skills_enabled: bool = True
    mcp_enabled: bool = True
    attachments_enabled: bool = True
    approvals_enabled: bool = True

    # ------------------------------------------------------------------ MCP（M1.5）
    mcp_user_stdio_enabled: bool = False
    mcp_allowed_hosts: str = ""
    mcp_stdio_allowed_commands: str = ""
    mcp_client_timeout_seconds: int = 30
    mcp_max_servers_per_user: int = 20
    mcp_max_tools_per_server: int = 50
    mcp_max_auto_tools: int = 60
    mcp_max_response_bytes: int = 262144
    mcp_tool_cache_ttl_seconds: int = 86400
    max_tool_rounds: int = 8
    capability_secret_key: str = ""

    # ------------------------------------------------------------------ 技能（M1.5）
    skill_max_upload_bytes: int = 5 * 1024 * 1024
    skill_max_files: int = 50
    skill_max_file_bytes: int = 1024 * 1024
    skill_max_skill_md_bytes: int = 200 * 1024
    skill_max_body_tokens: int = 8000
    skill_file_read_max_bytes: int = 32768
    skill_catalog_max_items: int = 100

    # ------------------------------------------------------------------ 记忆（M1.5）
    memory_top_k: int = 5
    memory_max_tokens: int = 1200
    memory_max_item_chars: int = 1000
    memory_max_title_chars: int = 200
    memory_pinned_max: int = 10
    memory_max_saves_per_run: int = 5

    # ------------------------------------------------------------------ 附件（M1.5）
    attachment_allowed_mime_types: str = (
        "text/plain,text/markdown,text/csv,text/html,application/json,application/pdf,"
        "application/zip,application/msword,application/vnd.ms-excel,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "image/png,image/jpeg,image/gif,image/webp,audio/mpeg,audio/wav,"
        "video/mp4,application/x-yaml,application/xml"
    )
    attachment_text_max_chars: int = 12000
    attachment_retention_hours: int = 24 * 30

    # ------------------------------------------------------------------ 提示词预算（M1.5）
    prompt_budget_tokens: int = 16000

    # ------------------------------------------------------------------ 认证
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7

    # ------------------------------------------------------------------ 业务限制
    instruction_max_chars: int = 4000
    message_max_chars: int = 8000
    skills_per_message: int = 3
    mcp_servers_per_message: int = 5
    mcp_tools_per_message: int = 10
    knowledge_categories_per_message: int = 5
    attachments_per_message: int = 5
    attachment_max_bytes: int = 20 * 1024 * 1024
    approval_timeout_seconds: int = 30 * 60
    hitl_stream_timeout_seconds: int = 15 * 60

    # ------------------------------------------------------------------ 限流
    # 默认值即生产建议值；本地联调可通过环境变量放宽（例如注册 100 次/小时）。
    rate_limit_enabled: bool = True
    rate_limit_register_max: int = 5
    rate_limit_register_window_seconds: int = 3600
    rate_limit_login_max: int = 10
    rate_limit_login_window_seconds: int = 300

    # ------------------------------------------------------------------ 路径
    data_dir: Path = Path("data")
    attachment_dir: Path = Path("data/attachments")

    @field_validator("cors_origins")
    @classmethod
    def _strip_origins(cls, value: str) -> str:
        return value.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 列表转成数组；``*`` 原样返回。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def allowed_models(self) -> list[str]:
        """模型白名单：``LLM_MODELS`` 优先，缺省时只允许 ``LLM_MODEL``。"""
        models = [item.strip() for item in self.llm_models.split(",") if item.strip()]
        if models:
            return models
        return [self.llm_model]

    @property
    def checkpoint_dsn(self) -> str:
        """LangGraph 使用 psycopg 原生连接串，需要去掉 SQLAlchemy 驱动后缀。"""
        raw = self.checkpoint_database_url or self.database_url
        raw = raw.replace("postgresql+psycopg://", "postgresql://")
        raw = raw.replace("postgresql+asyncpg://", "postgresql://")
        separator = "&" if "?" in raw else "?"
        return f"{raw}{separator}options=-csearch_path%3D{self.checkpoint_schema}"

    @property
    def is_fake_llm(self) -> bool:
        """没有密钥的开发/测试环境自动使用假模型，避免启动即不可用。"""
        if self.llm_provider.lower() == "fake":
            return True
        return not self.llm_api_key and self.env in {"dev", "test"}

    @property
    def embedding_key_usable(self) -> bool:
        """Embedding 密钥是否可用；占位符或过短时由上层回退到假向量。"""
        return not is_placeholder_api_key(self.embedding_api_key)

    def validate_runtime(self) -> None:
        """启动期的安全校验；生产环境不提供任何兜底密钥。"""
        if not self.database_url:
            raise RuntimeError(
                "必须配置 DATABASE_URL，指向本机已有的 PostgreSQL（参考 .env.example）"
            )
        if self.env == "prod" and len(self.jwt_secret) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字节的 JWT_SECRET")
        if (
            self.env == "prod"
            and self.knowledge_enabled
            and not self.embedding_key_usable
            and self.embedding_provider != "fake"
        ):
            raise RuntimeError(
                "生产环境已启用知识库，但未配置可用的 EMBEDDING_API_KEY；"
                "请配置真实密钥，或显式设置 EMBEDDING_PROVIDER=fake 使用假向量"
                "（假向量分数不可解释，仅用于无外部依赖的部署）"
            )
        if not self.jwt_secret:
            logger.warning("JWT_SECRET 未配置，开发环境使用内置兜底密钥；生产环境禁止这样运行")

    @property
    def resolved_jwt_secret(self) -> str:
        return self.jwt_secret or _DEV_JWT_SECRET

    @property
    def build_label(self) -> str:
        return self.app_build or "dev"

    @property
    def allowed_mime_type_list(self) -> list[str]:
        return [
            item.strip().lower()
            for item in self.attachment_allowed_mime_types.split(",")
            if item.strip()
        ]

    @property
    def mcp_host_allowlist(self) -> list[str]:
        """MCP SSRF 主机白名单；未配置时回退到 A2A 白名单。"""
        raw = self.mcp_allowed_hosts.strip() or self.a2a_allowed_hosts.strip()
        return [item.strip().lower() for item in raw.split(",") if item.strip()]

    @property
    def mcp_stdio_command_allowlist(self) -> list[str]:
        return [
            item.strip() for item in self.mcp_stdio_allowed_commands.split(",") if item.strip()
        ]

    @property
    def a2a_allow_loopback_resolved(self) -> bool:
        """loopback 放行：未显式配置时 dev/test 允许（本地假 Agent 联调），prod 拒绝。"""
        if self.a2a_allow_loopback is None:
            return self.env != "prod"
        return self.a2a_allow_loopback

    @property
    def a2a_host_allowlist(self) -> list[str]:
        """A2A SSRF 主机白名单：主机名、IP 或 CIDR。"""
        return [item.strip().lower() for item in self.a2a_allowed_hosts.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程级单例；测试修改环境变量后需要调用 ``cache_clear()``。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试辅助：清空配置缓存。"""
    get_settings.cache_clear()


__all__ = ["Settings", "get_settings", "is_placeholder_api_key", "reset_settings_cache"]
