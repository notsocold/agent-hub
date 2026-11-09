"""对话模型工厂。"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.agents.fake_model import FakeStreamingChatModel
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_chat_model(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    if settings.is_fake_llm:
        logger.warning("使用假流式模型（未配置 LLM_API_KEY 或 LLM_PROVIDER=fake）")
        return FakeStreamingChatModel(
            model_name=settings.llm_model, delay_ms=settings.fake_llm_delay_ms
        )
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_output_tokens,
        stream_usage=True,
        max_retries=1,
    )
