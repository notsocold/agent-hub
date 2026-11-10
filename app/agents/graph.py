"""主智能体图（M1.5：模型 + 工具循环；M5：审批 gate）。

图状态只保存对话消息，会话历史由 checkpointer 按 ``thread_id`` 持久化；
系统提示词、工具目录与工具执行器由调用方（modules/chat）每轮装配后传入，
agents/ 不反向依赖 modules/。

M5 的审批放在独立的 ``approval_gate`` 节点：工具节点不会因 ``interrupt()``
恢复而重跑（实测 tools 只执行一次），gate 重跑时按 ``(run_id, tool_call_id)``
幂等复用审批记录。``halted`` 用于取消/清理时路由到 END。
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
# 审批 gate：返回 state 更新（可用 interrupt() 挂起）；由 modules/chat 提供。
ApprovalGate = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class GraphState(MessagesState):
    """消息 + 本轮是否被取消/清理（halt 路由到 END）。"""

    halted: bool


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
    approval_gate: ApprovalGate | None = None,
    max_tool_rounds: int = 8,
):
    """编译主图。

    ``tools`` 为空或 ``tool_executor`` 为 None 时退化为 M1 无工具图；
    有工具时 ``model → [approval_gate] → tools → model`` 循环。
    """
    bound_model = model.bind_tools(tools) if tools else model

    async def call_model(state: GraphState) -> dict:
        messages = [SystemMessage(content=system_prompt), *state["messages"]]
        response = await bound_model.ainvoke(messages)
        # 每次模型调用重置 halted，保证 halt 之后的新一轮不被误路由。
        return {"messages": [response], "halted": False}

    async def run_tools(state: GraphState) -> dict:
        if tool_executor is None:
            return {"messages": []}
        return await tool_executor(state)

    async def run_gate(state: GraphState) -> dict:
        if approval_gate is None:
            return {}
        return await approval_gate(state)

    def route_after_model(state: GraphState) -> str:
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return END
        if _tool_rounds(state["messages"]) >= max_tool_rounds:
            return END
        if tool_executor is None:
            return END
        return "approval_gate" if approval_gate is not None else "tools"

    def route_after_gate(state: GraphState) -> str:
        return END if state.get("halted") else "tools"

    graph = StateGraph(GraphState)
    graph.add_node("model", call_model)
    if approval_gate is not None:
        graph.add_node("approval_gate", run_gate)
    if tool_executor is not None:
        graph.add_node("tools", run_tools)
    graph.add_edge(START, "model")
    if approval_gate is not None:
        graph.add_conditional_edges(
            "model",
            route_after_model,
            {"approval_gate": "approval_gate", END: END},
        )
        graph.add_conditional_edges(
            "approval_gate",
            route_after_gate,
            {"tools": "tools", END: END},
        )
    else:
        graph.add_conditional_edges("model", route_after_model, {"tools": "tools", END: END})
    if tool_executor is not None:
        graph.add_edge("tools", "model")
    return graph.compile(checkpointer=checkpointer)
