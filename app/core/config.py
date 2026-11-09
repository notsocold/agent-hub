"""应用配置。

所有配置项只从这里读取，业务代码禁止直接访问环境变量。
测试通过 ``get_settings.cache_clear()`` 后重新获取，保证配置可注入。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# 开发环境缺少 JWT_SECRET 时使用的兜底值，生产环境会拒绝启动。
_DEV_JWT_SECRET = "agent-hub-dev-insecure-secret-change-me"


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
    vector_top_k: int = 6
    vector_score_threshold: float = 0.30

    # ------------------------------------------------------------------ A2A（M2/M3 使用）
    a2a_enabled: bool = False
    a2a_public_url: str = "http://localhost:8000"
    a2a_agent_path: str = "/a2a"
    a2a_client_timeout_seconds: int = 60
    a2a_allowed_hosts: str = ""

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
    rate_limit_enabled: bool = True

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

    def validate_runtime(self) -> None:
        """启动期的安全校验；生产环境不提供任何兜底密钥。"""
        if not self.database_url:
            raise RuntimeError(
                "必须配置 DATABASE_URL，指向本机已有的 PostgreSQL（参考 .env.example）"
            )
        if self.env == "prod" and len(self.jwt_secret) < 32:
            raise RuntimeError("生产环境必须配置至少 32 字节的 JWT_SECRET")
        if not self.jwt_secret:
            logger.warning("JWT_SECRET 未配置，开发环境使用内置兜底密钥；生产环境禁止这样运行")

    @property
    def resolved_jwt_secret(self) -> str:
        return self.jwt_secret or _DEV_JWT_SECRET

    @property
    def build_label(self) -> str:
        return self.app_build or "dev"


@lru_cache
def get_settings() -> Settings:
    """进程级单例；测试修改环境变量后需要调用 ``cache_clear()``。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试辅助：清空配置缓存。"""
    get_settings.cache_clear()


__all__ = ["Settings", "get_settings", "reset_settings_cache"]
