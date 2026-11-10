"""图 approval_gate / halt_gate 路由的单元测试（InMemorySaver，不依赖数据库）。

复现 spec §3 的实测结论：审批必须放在 gate 节点，恢复不会重跑 tools；
同一 gate 只调用一次 interrupt；halt gate 清理挂起后新输入可继续。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

from app.agents.graph import build_graph
from app.modules.chat.approvals import build_halt_gate
from tests.helpers import make_scripted_model


def _tools_recorder(executed: list[str]):
    async def run_tools(state: dict[str, Any]) -> dict[str, Any]:
        last = state["messages"][-1]
        results = []
        for call in last.tool_calls:
            executed.append(call["id"])
            results.append(ToolMessage(content=f"ok:{call['id']}", tool_call_id=call["id"]))
        return {"messages": results}

    return run_tools


def _single_call_model() -> Any:
    return make_scripted_model(
        [
            {"tool_calls": [{"id": "c1", "name": "danger", "args": {}}]},
            {"text": "done"},
        ]
    )


async def test_gate_interrupts_and_tools_execute_once() -> None:
    executed: list[str] = []

    async def gate(state: dict[str, Any]) -> dict[str, Any]:
        interrupt({"approvals": ["c1"]})
        return {}

    graph = build_graph(
        _single_call_model(),
        InMemorySaver(),
        tool_executor=_tools_recorder(executed),
        approval_gate=gate,
    )
    config = {"configurable": {"thread_id": "g1"}}
    async for _ in graph.astream({"messages": [HumanMessage(content="hi")]}, config, stream_mode="messages"):
        pass
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("approval_gate",)
    assert executed == []

    async for _ in graph.astream(Command(resume={"decision": "approve"}), config, stream_mode="messages"):
        pass
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()
    assert executed == ["c1"]
    assert any(isinstance(m, ToolMessage) for m in snapshot.values["messages"])


async def test_two_approvals_single_interrupt() -> None:
    executed: list[str] = []
    interrupts: list[Any] = []

    async def gate(state: dict[str, Any]) -> dict[str, Any]:
        last = state["messages"][-1]
        value = interrupt({"approvals": [call["id"] for call in last.tool_calls]})
        interrupts.append(value)
        return {}

    model = make_scripted_model(
        [
            {
                "tool_calls": [
                    {"id": "c1", "name": "danger", "args": {}},
                    {"id": "c2", "name": "danger", "args": {}},
                ]
            },
            {"text": "done"},
        ]
    )
    graph = build_graph(model, InMemorySaver(), tool_executor=_tools_recorder(executed), approval_gate=gate)
    config = {"configurable": {"thread_id": "g2"}}
    async for _ in graph.astream({"messages": [HumanMessage(content="hi")]}, config, stream_mode="messages"):
        pass
    assert executed == []
    async for _ in graph.astream(Command(resume={"decision": "approve"}), config, stream_mode="messages"):
        pass
    assert executed == ["c1", "c2"]
    assert len(interrupts) == 1


async def test_halt_gate_clears_suspended_thread() -> None:
    executed: list[str] = []

    async def gate(state: dict[str, Any]) -> dict[str, Any]:
        interrupt({"approvals": ["c1"]})
        return {}

    model = _single_call_model()
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "g3"}}
    graph = build_graph(model, saver, tool_executor=_tools_recorder(executed), approval_gate=gate)
    async for _ in graph.astream({"messages": [HumanMessage(content="hi")]}, config, stream_mode="messages"):
        pass
    assert (await graph.aget_state(config)).next == ("approval_gate",)

    halt_graph = build_graph(
        model, saver, tool_executor=_tools_recorder(executed), approval_gate=build_halt_gate()
    )
    async for _ in halt_graph.astream(
        Command(resume={"decision": "cancel"}), config, stream_mode="messages"
    ):
        pass
    snapshot = await halt_graph.aget_state(config)
    assert snapshot.next == ()
    assert snapshot.values.get("halted") is True
    assert executed == []
    tool_messages = [m for m in snapshot.values["messages"] if isinstance(m, ToolMessage)]
    assert tool_messages and "取消" in tool_messages[-1].content

    # 清理后新输入能正常走到 END
    async for _ in halt_graph.astream(
        {"messages": [HumanMessage(content="new message")]}, config, stream_mode="messages"
    ):
        pass
    final = await halt_graph.aget_state(config)
    assert final.next == ()
    assert final.values["messages"][-1].content == "done"
