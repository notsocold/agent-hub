"""M9 T6 集成测试：执行语义——workflow.step 事件与强制审批。

覆盖任务书测试清单：

- 全链路 3 步模板（knowledge_search → a2a → approval）：``workflow.step``
  恰 2 条、``step_id`` 序列 ``["s1", "s2"]``、每条紧跟对应 ``tool.result``
  且 tool 名一致、payload 键集精确（信封字段之外恰 5 键）、``status``
  复用工具执行状态；
- 强制审批兜底：FakeA2aAgent 声明 ``requires_approval=False``（原生策略
  不审批），workflow 的 approval 步骤强制审批仍生效——出现
  ``run.interrupted`` → approve → run succeeded、``fake.call_count == 1``；
- M7 回放契约：事件落库后 ``GET /api/v1/runs/{id}/events`` 中两条
  ``workflow.step`` 的 ``event`` 字段正确（``event_type="workflow"``）；
- 拒绝路径：reject 后 ``workflow.step`` 的 ``status == "rejected"``，
  run 正常收尾（rejected 工具结果不拖垮整轮）。

模式同 spike ``test_three_step_workflow_end_to_end`` / T5
``test_workflow_chat.py``：``seeded_docs`` + 服务层直启（monkeypatch
``build_chat_model`` + ``make_scripted_model``）+ admin 建远程 agent。
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db.models.chat import Run
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.a2a.client import register_test_transport
from app.modules.chat import service as chat_service
from app.modules.chat.schemas import ChatStreamRequest
from app.modules.chat.service import start_chat
from tests.conftest import auth_headers, collect_run_events_live, register_user
from tests.fixtures.fake_a2a_agent import FakeA2aAgent
from tests.helpers import make_scripted_model

AGENT_URL = "http://a2a.test"

# 三步模板：检索 → 委派 → 审批。白名单并集 = {search_knowledge, send_message}，
# 强制审批集合 = {send_message}；approval 步骤不提供工具、不产生 workflow.step，
# 故工具步骤命中恰为 s1（search_knowledge）与 s2（send_message）两条事件。
WORKFLOW_STEPS: list[dict[str, Any]] = [
    {"id": "s1", "type": "knowledge_search", "title": "检索政策", "instruction": "先检索知识库"},
    {"id": "s2", "type": "a2a", "title": "委派远程", "instruction": "委派远程智能体核对"},
    {"id": "s3", "type": "approval", "title": "人工确认", "config": {"tools": ["send_message"]}},
]

# workflow.step payload 恰为 5 键；run_id/ts/seq 信封由 RunEventPublisher 统一附加。
STEP_PAYLOAD_KEYS = {"step_id", "step_type", "step_title", "tool", "status"}
ENVELOPE_KEYS = {"run_id", "ts", "seq"}


async def _load_user(registered: dict) -> User:
    """在独立 session 里按注册响应取回用户行（对象不跨 session 使用）。"""
    async with get_session_factory()() as session:
        user = await session.scalar(
            select(User).where(User.id == uuid.UUID(registered["user"]["id"]))
        )
        assert user is not None
        return user


async def _register_fake_agent(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> FakeA2aAgent:
    """注册 FakeA2aAgent 传输并创建 RemoteAgent 行（send_message 工具目录需要）。

    slug 固定 ``fake``：脚本模型的工具调用参数 ``agent="fake"`` 按 slug 匹配
    ``a2a_agent_map``；``requires_approval=False``——原生策略不审批，
    ``run.interrupted`` 一旦出现即证明 workflow 强制审批兜底生效。
    """
    fake = FakeA2aAgent(url=AGENT_URL, requires_approval=False)
    register_test_transport(AGENT_URL, httpx.ASGITransport(app=fake.app))
    created = await client.post(
        "/api/v1/agents", json={"url": AGENT_URL, "slug": "fake"}, headers=admin_headers
    )
    assert created.status_code == 201, created.text
    return fake


async def _create_workflow(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    """通过 API 创建本人三步模板并断言 201，返回响应 JSON。"""
    response = await client.post(
        "/api/v1/workflows", json={"name": "退货处理流程", "steps": WORKFLOW_STEPS}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _start_workflow_chat(
    monkeypatch: pytest.MonkeyPatch, registered: dict, model: Any, *, workflow_id: str
) -> str:
    """服务层直启一轮工作流对话，返回 run_id（后台任务继续执行）。"""
    monkeypatch.setattr(chat_service, "build_chat_model", lambda settings: model)
    user = await _load_user(registered)
    async with get_session_factory()() as session:
        result = await start_chat(
            session,
            user,
            ChatStreamRequest(message="按流程处理退货", workflow_id=uuid.UUID(workflow_id)),
        )
    return str(result.run_id)


def _scripted_model() -> Any:
    """脚本模型：第一轮同轮调用两个工具（检索 + 委派），第二轮收尾文本。

    send_message 命中 workflow 强制审批集合：审批挂起轮工具未执行
    （gate interrupt），恢复后才有 tool.result + workflow.step。
    """
    return make_scripted_model(
        [
            {
                "tool_calls": [
                    {"name": "search_knowledge", "args": {"query": "退货需要保留吊牌吗"}},
                    {
                        "name": "send_message",
                        "args": {"agent": "fake", "task": "核对 9 月退款工单"},
                    },
                ]
            },
            {"text": "流程完成：已检索政策并委派远程核对。"},
        ]
    )


async def test_workflow_step_events_and_force_approval(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    admin_auth: dict[str, str],
    seeded_docs: dict[str, Any],
) -> None:
    """approve 路径：事件序列、payload 键集、强制审批生效与 M7 回放契约。"""
    fake = await _register_fake_agent(client, admin_auth)
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    workflow = await _create_workflow(client, headers)

    model = _scripted_model()
    run_id = await _start_workflow_chat(monkeypatch, registered, model, workflow_id=workflow["id"])

    approval_id: str | None = None

    async def on_event(name: str, data: dict) -> None:
        nonlocal approval_id
        if name != "run.interrupted":
            return
        approval_id = data["approvals"][0]
        decided = await client.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text

    events = await collect_run_events_live(
        run_id, on_event=on_event, stop_on={"message.done"}, timeout=30
    )
    names = [name for name, _ in events]

    # 强制审批生效：agent 原生不要求审批仍出现 run.interrupted；approve 后远程恰被调用一次。
    assert "run.interrupted" in names
    assert approval_id
    assert fake.call_count == 1

    # workflow.step 恰 2 条，step_id 序列 s1→s2，每条紧跟对应 tool.result 且 tool 名一致。
    steps = [(index, data) for index, (name, data) in enumerate(events) if name == "workflow.step"]
    assert len(steps) == 2
    assert [data["step_id"] for _, data in steps] == ["s1", "s2"]
    for index, data in steps:
        prev_name, prev_data = events[index - 1]
        assert prev_name == "tool.result"
        assert prev_data["tool"] == data["tool"]

    # payload 键集精确：信封之外恰为 5 键；status 复用工具执行状态（本轮均 ok）。
    for _, data in steps:
        assert set(data) - ENVELOPE_KEYS == STEP_PAYLOAD_KEYS
        assert data["status"] == "ok"
    assert steps[0][1]["step_type"] == "knowledge_search"
    assert steps[0][1]["step_title"] == "检索政策"
    assert steps[0][1]["tool"] == "search_knowledge"
    assert steps[1][1]["step_type"] == "a2a"
    assert steps[1][1]["step_title"] == "委派远程"
    assert steps[1][1]["tool"] == "send_message"

    # run 正常收尾且终态 succeeded。
    assert names[-1] == "message.done"
    async with get_session_factory()() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        assert run is not None
        assert run.status == "succeeded"

    # M7 回放契约：event_type="workflow" 落库，event 字段还原为 workflow.step，
    # payload 与实时帧一致（仅剔除内部 _event）。
    replayed = await client.get(
        f"/api/v1/runs/{run_id}/events", params={"limit": 500}, headers=headers
    )
    assert replayed.status_code == 200, replayed.text
    items = replayed.json()["items"]
    replay_steps = [item for item in items if item["event"] == "workflow.step"]
    assert len(replay_steps) == 2
    for item, (_, live) in zip(replay_steps, steps, strict=True):
        assert item["type"] == "workflow"
        assert item["payload"] == live
    assert replay_steps[0]["title"] == "工作流步骤 检索政策"
    assert replay_steps[1]["title"] == "工作流步骤 委派远程"


async def test_reject_marks_workflow_step_rejected(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    admin_auth: dict[str, str],
    seeded_docs: dict[str, Any],
) -> None:
    """reject 路径：send_message 被拒后 workflow.step(s2) status=rejected，run 正常收尾。"""
    fake = await _register_fake_agent(client, admin_auth)
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    workflow = await _create_workflow(client, headers)

    model = _scripted_model()
    run_id = await _start_workflow_chat(monkeypatch, registered, model, workflow_id=workflow["id"])

    async def on_event(name: str, data: dict) -> None:
        if name != "run.interrupted":
            return
        decided = await client.post(
            f"/api/v1/approvals/{data['approvals'][0]}/decision",
            json={"decision": "reject"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text

    events = await collect_run_events_live(
        run_id, on_event=on_event, stop_on={"message.done"}, timeout=30
    )
    names = [name for name, _ in events]
    assert "run.interrupted" in names
    # 拒绝后 send_message 不再真正调用远程。
    assert fake.call_count == 0

    # s1 照常执行 ok；s2 因审批被拒标记 rejected。
    steps = [data for name, data in events if name == "workflow.step"]
    assert [data["step_id"] for data in steps] == ["s1", "s2"]
    assert steps[0]["status"] == "ok"
    assert steps[1]["status"] == "rejected"

    # run 正常收尾：rejected 只作为工具结果返回模型，不拖垮整轮。
    assert names[-1] == "message.done"
    async with get_session_factory()() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        assert run is not None
        assert run.status == "succeeded"
