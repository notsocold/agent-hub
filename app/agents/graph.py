"""主智能体图（M1.5：模型 + 工具循环）。

图状态只保存对话消息，会话历史由 checkpointer 按 ``thread_id`` 持久化；
系统提示词、工具目录与工具执行器由调用方（modules/chat）每轮装配后传入，
agents/ 不反向依赖 modules/。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from app.agents.prompts import BASE_SYSTEM_PROMPT

# 工具执行器：state -> {"messages": [ToolMessage, ...]}；由 modules/chat 提供。
ToolExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _tool_rounds(messages: list[BaseMessage]) -> int:
    return sum(
        1
        for message in messages
        if isinstance(message, AIMessage) and bool(message.tool_calls)
    )


def build_graph(
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,
    *,
    system_prompt: str = BASE_SYSTEM_PROMPT,
    tools: list[Any] | None = None,
    tool_executor: ToolExecutor | None = None,
    max_tool_rounds: int = 8,
):
    """编译主图。

    ``tools`` 为空或 ``tool_executor`` 为 None 时退化为 M1 无工具图；
    模型带 tool_calls 时进入工具节点，执行后回到模型继续生成。
    """
    bound_model = model.bind_tools(tools) if tools else model

    async def call_model(state: MessagesState) -> dict:
        messages = [SystemMessage(content=system_prompt), *state["messages"]]
        response = await bound_model.ainvoke(messages)
        return {"messages": [response]}

    async def run_tools(state: MessagesState) -> dict:
        if tool_executor is None:
            return {"messages": []}
        return await tool_executor(state)

    def route_after_model(state: MessagesState) -> str:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return END
        if _tool_rounds(state["messages"]) >= max_tool_rounds:
            return END
        if tool_executor is None:
            return END
        return "tools"

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    if tool_executor is not None:
        graph.add_node("tools", run_tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model",
        route_after_model,
        {"tools": "tools", END: END},
    )
    if tool_executor is not None:
        graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)
