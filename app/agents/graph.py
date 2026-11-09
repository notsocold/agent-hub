"""主智能体图（M1 无工具版）。"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, MessagesState, StateGraph

from app.agents.prompts import BASE_SYSTEM_PROMPT


def build_graph(model: BaseChatModel, checkpointer: BaseCheckpointSaver):
    """编译主图。

    图状态只保存对话消息，会话历史由 checkpointer 按 ``thread_id`` 持久化；
    工具、检索与审批节点在 M2/M4/M5 按需加入。
    """

    async def call_model(state: MessagesState) -> dict:
        messages = [SystemMessage(content=BASE_SYSTEM_PROMPT), *state["messages"]]
        response = await model.ainvoke(messages)
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_edge(START, "model")
    return graph.compile(checkpointer=checkpointer)
