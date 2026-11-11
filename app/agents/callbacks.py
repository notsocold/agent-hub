"""LangChain 回调：LLM span 与 token/时延指标。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.core.metrics import record_llm_call
from app.core.telemetry import get_tracer


def _usage_from_response(response: LLMResult) -> dict[str, int]:
    """从 LLMResult 中尽量提取 token 用量；兼容 OpenAI 与 fake 模型。"""
    candidates: list[Any] = []
    if isinstance(response.llm_output, dict):
        candidates.append(response.llm_output.get("token_usage"))
        candidates.append(response.llm_output.get("usage"))
    for generation_list in response.generations:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            if message is not None:
                candidates.append(getattr(message, "usage_metadata", None))
                candidates.append(getattr(message, "response_metadata", None))

    input_tokens = output_tokens = reasoning_tokens = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        input_tokens = int(
            candidate.get("input_tokens")
            or candidate.get("prompt_tokens")
            or candidate.get("input_token_count")
            or 0
        )
        output_tokens = int(
            candidate.get("output_tokens")
            or candidate.get("completion_tokens")
            or candidate.get("output_token_count")
            or 0
        )
        reasoning_tokens = int(
            candidate.get("reasoning_tokens")
            or (candidate.get("output_token_details") or {}).get("reasoning")
            or 0
        )
        if input_tokens or output_tokens or reasoning_tokens:
            break
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


class ObservabilityCallbackHandler(BaseCallbackHandler):
    """为每次模型调用开一个 ``llm.chat`` span，并记录指标。"""

    def __init__(self, model: str) -> None:
        self.model = model
        self._spans: dict[str, Any] = {}
        self._started: dict[str, float] = {}

    def _key(self, run_id: uuid.UUID | str | None) -> str:
        return str(run_id)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[Any],
        *,
        run_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> None:
        key = self._key(run_id)
        span = get_tracer().start_span(
            "llm.chat",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.system": "langchain",
                "gen_ai.request.model": self.model,
                "app.stream": True,
            },
        )
        self._spans[key] = span
        self._started[key] = time.monotonic()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> None:
        key = self._key(run_id)
        span = self._spans.pop(key, None)
        started = self._started.pop(key, None)
        usage = _usage_from_response(response)
        duration = max(0.0, time.monotonic() - started) if started is not None else 0.0
        if span is not None:
            span.set_attribute("gen_ai.usage.input_tokens", usage["input_tokens"])
            span.set_attribute("gen_ai.usage.output_tokens", usage["output_tokens"])
            span.set_attribute("gen_ai.usage.reasoning_tokens", usage["reasoning_tokens"])
            span.set_status(Status(StatusCode.OK))
            span.end()
        record_llm_call(
            model=self.model,
            status="ok",
            duration_seconds=duration,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID | str | None = None,
        **kwargs: Any,
    ) -> None:
        key = self._key(run_id)
        span = self._spans.pop(key, None)
        started = self._started.pop(key, None)
        duration = max(0.0, time.monotonic() - started) if started is not None else 0.0
        if span is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR))
            span.end()
        record_llm_call(
            model=self.model,
            status="error",
            duration_seconds=duration,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
        )


__all__ = ["ObservabilityCallbackHandler"]
