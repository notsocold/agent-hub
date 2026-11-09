"""测试辅助：脚本化工具调用模型与 MCP mock 服务器。"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedToolModel(BaseChatModel):
    """按脚本逐轮返回的假模型：step 为 tool_calls 消息或最终文本。

    ``steps`` 依次消费：``{"tool_calls": [{name, args, id}]}`` 或 ``{"text": "..."}``；
    超出后重复最后一步。测试用它验证工具节点循环与 HITL。
    """

    steps: list[dict[str, Any]]
    _index: int = 0
    captured: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "agent-hub-scripted"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> ScriptedToolModel:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.captured.append(list(messages))
        step = self.steps[min(self._index, len(self.steps) - 1)]
        self._index += 1
        tool_calls: list[dict[str, Any]] = []
        if step.get("tool_calls"):
            for call in step["tool_calls"]:
                tool_calls.append(
                    {
                        "name": call["name"],
                        "args": call.get("args") or {},
                        "id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    }
                )
        message = AIMessage(
            content=step.get("text") or "",
            tool_calls=tool_calls,
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def make_scripted_model(steps: list[dict[str, Any]]) -> ScriptedToolModel:
    return ScriptedToolModel(steps=steps)


def make_mcp_transport(tools: list[dict[str, Any]]) -> httpx.MockTransport:
    """构造 JSON-RPC MCP mock 服务器：initialize / tools/list / tools/call。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("content-type", "").startswith("application/json")
        try:
            payload = json.loads(request.content)
        except json.JSONDecodeError:
            return httpx.Response(400)
        method = payload.get("method")
        response_id = payload.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            params = payload.get("params") or {}
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": f"ok:{params.get('name')}:{json.dumps(params.get('arguments'), ensure_ascii=False)}",
                    }
                ],
                "isError": False,
            }
        elif method == "notifications/initialized":
            return httpx.Response(202)
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": response_id, "error": {"code": -32601, "message": "unknown"}})
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": response_id, "result": result},
        )

    return httpx.MockTransport(handler)


MOCK_TOOL_READ = {
    "name": "search",
    "description": "搜索资料",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "关键词"}},
        "required": ["query"],
    },
    "annotations": {"readOnlyHint": True},
}

MOCK_TOOL_WRITE = {
    "name": "create_issue",
    "description": "创建工单（写操作）",
    "inputSchema": {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "标题"}},
        "required": ["title"],
    },
    "annotations": {"destructiveHint": False, "readOnlyHint": False},
}
