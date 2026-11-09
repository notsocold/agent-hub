"""开发/测试用的假流式模型。

没有 LLM_API_KEY 时使用，保证前端联调不被外部模型阻塞；
它始终声明自己是假模型，避免被误当成真实回答。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class FakeStreamingChatModel(BaseChatModel):
    """按固定节奏逐段吐字的假模型，支持取消与用量统计。

    不真正执行工具调用；``bind_tools`` 返回自身，保证无工具环境下的图可用。
    """

    model_name: str = "fake-chat"
    delay_ms: int = 15
    chunk_size: int = 3

    @property
    def _llm_type(self) -> str:
        return "agent-hub-fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(
        self,
        tools: list[Any],
        **kwargs: Any,
    ) -> FakeStreamingChatModel:
        """假模型不支持工具调用：返回自身，忽略工具定义。"""
        return self

    def _reply_text(self, messages: list[BaseMessage]) -> str:
        last_human = next(
            (item for item in reversed(messages) if isinstance(item, HumanMessage)), None
        )
        question = str(last_human.content)[:60] if last_human else ""
        return (
            f"收到你的消息：「{question}」。\n\n"
            "这是开发环境的假模型回复，用于验证流式输出、取消与历史记录链路；"
            "配置 LLM_API_KEY 后会自动切换到真实模型。"
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = self._reply_text(messages)
        message = AIMessage(
            content=text,
            usage_metadata={
                "input_tokens": max(1, len(str(messages)) // 4),
                "output_tokens": max(1, len(text) // 2),
                "total_tokens": max(1, len(str(messages)) // 4 + len(text) // 2),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        text = self._reply_text(messages)
        pieces = [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]
        delay = max(0, self.delay_ms) / 1000
        for index, piece in enumerate(pieces):
            if delay:
                await asyncio.sleep(delay)
            final = index == len(pieces) - 1
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=piece,
                    usage_metadata=(
                        {
                            "input_tokens": max(1, len(str(messages)) // 4),
                            "output_tokens": max(1, len(text) // 2),
                            "total_tokens": max(1, len(str(messages)) // 4 + len(text) // 2),
                        }
                        if final
                        else None
                    ),
                )
            )
            if run_manager is not None:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk
