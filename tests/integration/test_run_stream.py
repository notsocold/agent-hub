"""SSE 续流端点 ``GET /runs/{run_id}/stream`` 的集成测试。

``httpx.ASGITransport`` 会缓冲整段 SSE，因此只对「立即结束」的流走 HTTP；
实时跟随与心跳直接消费服务层 ``stream_run_events`` 生成器，事件经真实
``RunEventPublisher``/进程内 bus 驱动。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.chat import service as chat_service
from app.modules.chat.bus import bus
from app.modules.chat.events import RunEventPublisher
from tests.conftest import auth_headers, register_user

_RUN_STREAM = "/api/v1/runs/{run_id}/stream"


def _parse_sse(body: str) -> list[dict]:
    """把 SSE 响应体解析为帧列表；``: ping`` 注释帧记为 ``event=": ping"``。"""
    frames: list[dict] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line]
        if not lines:
            continue
        if all(line.startswith(":") for line in lines):
            frames.append({"id": None, "event": ": ping", "data": None})
            continue
        frame: dict = {"id": None, "event": None, "data": None}
        for line in lines:
            field, _, value = line.partition(":")
            value = value.strip()
            if field == "id":
                frame["id"] = int(value)
            elif field == "event":
                frame["event"] = value
            elif field == "data":
                frame["data"] = json.loads(value)
        assert frame["event"] is not None, f"无法解析的 SSE 帧：{block!r}"
        frames.append(frame)
    return frames


async def _seed_run(
    user_id: str | uuid.UUID,
    *,
    status: str = "succeeded",
    events: list[str] | None = None,
) -> tuple[uuid.UUID, list[int]]:
    """直接写库造 conversation + run + 已落库事件，避免真实执行图。

    ``events`` 按顺序占用 ``seq=1..N``，``payload._event`` 保存原始 SSE 事件名；
    未提供 ``_event`` 的事件在生成器里会被跳过，因此测试用的事件名都非空。
    """
    names = events or []
    async with get_session_factory()() as session:
        conversation = Conversation(user_id=uuid.UUID(str(user_id)), title="续流测试")
        session.add(conversation)
        await session.flush()
        run = Run(conversation_id=conversation.id, status=status, model="fake")
        session.add(run)
        await session.flush()
        ts = datetime.now(UTC)
        rows = [
            RunEvent(
                run_id=run.id,
                seq=seq,
                type="message",
                status="ok",
                title="",
                subtitle="",
                payload={"run_id": str(run.id), "seq": seq, "_event": name},
                ts=ts,
            )
            for seq, name in enumerate(names, start=1)
        ]
        session.add_all(rows)
        await session.commit()
        return run.id, list(range(1, len(names) + 1))


# ---------------------------------------------------------------- HTTP 契约测试


async def test_stream_replays_persisted_events_then_closes(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    names = ["run.started", "message.created", "message.delta", "message.done"]
    run_id, seqs = await _seed_run(registered["user"]["id"], events=names)

    # 不传 after_seq：默认 0，从第一条已落库事件开始回放。
    response = await client.get(_RUN_STREAM.format(run_id=run_id), headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Run-ID"] == str(run_id)
    assert response.headers["X-Request-ID"]
    frames = _parse_sse(response.text)
    assert [frame["event"] for frame in frames] == names
    assert [frame["id"] for frame in frames] == seqs
    assert [frame["data"]["seq"] for frame in frames] == seqs
    assert all(frame["data"]["run_id"] == str(run_id) for frame in frames)
    # 终态事件后生成器立即返回，响应体到此为止。
    assert frames[-1]["event"] == "message.done"


async def test_stream_after_seq_skips_earlier_events(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    names = ["run.started", "message.created", "message.delta", "message.done"]
    run_id, seqs = await _seed_run(registered["user"]["id"], events=names)

    response = await client.get(
        _RUN_STREAM.format(run_id=run_id), params={"after_seq": seqs[1]}, headers=headers
    )

    assert response.status_code == 200, response.text
    frames = _parse_sse(response.text)
    assert [frame["id"] for frame in frames] == seqs[2:]
    assert [frame["event"] for frame in frames] == names[2:]


async def test_terminal_run_closes_immediately_when_replay_is_empty(
    client: httpx.AsyncClient,
) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    names = ["run.started", "message.created", "message.done"]
    run_id, seqs = await _seed_run(registered["user"]["id"], events=names)

    # after_seq 已经越过终态事件：回放为空，run 已终态 ⇒ 不应挂起等待实时事件。
    async with asyncio.timeout(2):
        response = await client.get(
            _RUN_STREAM.format(run_id=run_id), params={"after_seq": seqs[-1]}, headers=headers
        )

    assert response.status_code == 200
    assert response.headers["X-Run-ID"] == str(run_id)
    assert _parse_sse(response.text) == []


async def test_stream_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get(_RUN_STREAM.format(run_id=uuid.uuid4()))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_stream_rejects_invalid_token(client: httpx.AsyncClient) -> None:
    response = await client.get(
        _RUN_STREAM.format(run_id=uuid.uuid4()),
        headers={"Authorization": "Bearer not-a-valid-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_stream_hides_missing_and_foreign_runs(client: httpx.AsyncClient) -> None:
    owner = await register_user(client)
    owner_headers = auth_headers(owner["access_token"])
    run_id, _ = await _seed_run(owner["user"]["id"], events=["run.started", "message.done"])
    other = await register_user(client)
    other_headers = auth_headers(other["access_token"])

    foreign = await client.get(_RUN_STREAM.format(run_id=run_id), headers=other_headers)
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "not_found"
    # 路由不存在时也是 404，用业务文案区分「所有权校验」与「路由缺失」。
    assert foreign.json()["error"]["message"] == "运行不存在"

    missing = await client.get(_RUN_STREAM.format(run_id=uuid.uuid4()), headers=owner_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert missing.json()["error"]["message"] == "运行不存在"


async def test_stream_hides_foreign_run_before_replay_cap(client: httpx.AsyncClient) -> None:
    """所有权校验先于回放上限：他人 run 即使回放 >1000 条也必须是 404，不是 400。"""
    owner = await register_user(client)
    names = [*(["message.delta"] * 1000), "message.done"]
    run_id, _ = await _seed_run(owner["user"]["id"], events=names)
    other = await register_user(client)

    response = await client.get(
        _RUN_STREAM.format(run_id=run_id),
        params={"after_seq": 0},
        headers=auth_headers(other["access_token"]),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"] == "运行不存在"


async def test_stream_rejects_negative_after_seq(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])

    response = await client.get(
        _RUN_STREAM.format(run_id=uuid.uuid4()), params={"after_seq": -1}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_stream_rejects_after_seq_beyond_int32(client: httpx.AsyncClient) -> None:
    """run_events.seq 是 int4：超出 2**31-1 的 after_seq 在参数层就 422，不落到 SQL。"""
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    run_id, _ = await _seed_run(registered["user"]["id"], events=["run.started", "message.done"])

    response = await client.get(
        _RUN_STREAM.format(run_id=run_id), params={"after_seq": 2**31}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_replay_over_cap_returns_400_and_boundary_is_allowed(
    client: httpx.AsyncClient,
) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    # 1001 条：恰好在回放上限之外；最后一条是终态事件。
    names = [*(["message.delta"] * 1000), "message.done"]
    run_id, seqs = await _seed_run(registered["user"]["id"], events=names)

    over = await client.get(
        _RUN_STREAM.format(run_id=run_id), params={"after_seq": 0}, headers=headers
    )
    assert over.status_code == 400
    error = over.json()["error"]
    assert error["code"] == "replay_too_large"
    assert "1000" in error["message"]
    assert "分页" in error["message"]
    assert "after_seq" in error["message"]

    # seq > 1 恰好 1000 条，不超上限：正常回放并在终态事件处关闭。
    boundary = await client.get(
        _RUN_STREAM.format(run_id=run_id), params={"after_seq": seqs[0]}, headers=headers
    )
    assert boundary.status_code == 200, boundary.text
    frames = _parse_sse(boundary.text)
    assert [frame["id"] for frame in frames] == seqs[1:]
    assert frames[-1]["event"] == "message.done"


# ------------------------------------------------------------- 服务层实时测试


async def test_generator_caps_replay_defensively(user: User) -> None:
    """路由层 400 之外的兜底：生成器超过 max_replay 时只发前 N 条并关闭，不抛错。"""
    names = ["run.started", "message.delta", "message.delta", "message.done"]
    run_id, seqs = await _seed_run(user.id, events=names)

    frames: list[dict] = []
    async with asyncio.timeout(5):
        async for frame in chat_service.stream_run_events(run_id, after_seq=0, max_replay=2):
            frames.extend(_parse_sse(frame))

    assert [frame["id"] for frame in frames] == seqs[:2]
    assert [frame["event"] for frame in frames] == names[:2]


async def test_legacy_stream_default_replays_without_truncation(user: User) -> None:
    """旧 ``POST /chat/stream`` 语义：不传新参数时 1001 条事件全部回放，不截断。"""
    names = [*(["message.delta"] * 1000), "message.done"]
    run_id, seqs = await _seed_run(user.id, events=names)

    frames: list[dict] = []
    async with asyncio.timeout(10):
        async for frame in chat_service.stream_run_events(run_id, after_seq=0):
            frames.extend(_parse_sse(frame))

    assert len(frames) == 1001
    assert [frame["id"] for frame in frames] == seqs
    assert frames[-1]["event"] == "message.done"


async def test_legacy_stream_terminal_run_keeps_connection(user: User) -> None:
    """旧语义：已终态 run 且回放为空时保持连接，不做终态早关。"""
    names = ["run.started", "message.done"]
    run_id, seqs = await _seed_run(user.id, events=names)
    frames: list[dict] = []

    async def consume() -> None:
        async for frame in chat_service.stream_run_events(run_id, after_seq=seqs[-1]):
            frames.extend(_parse_sse(frame))

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.2)
        assert not task.done(), "旧语义下已终态 run 不应回放后立即关流"
        assert frames == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_interrupted_stream_stays_open_then_follows_live_events(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """interrupted run：回放后不关闭，审批恢复的 run.resumed/message.done 实时到达后结束。"""
    # 缩短心跳周期，验证等待实时事件期间会发送 `: ping` 且不影响后续事件。
    monkeypatch.setattr(chat_service, "_SSE_HEARTBEAT_SECONDS", 0.05)
    names = ["run.started", "run.interrupted"]
    run_id, seqs = await _seed_run(user.id, status="interrupted", events=names)
    frames: list[dict] = []

    async def consume() -> None:
        async for frame in chat_service.stream_run_events(run_id, after_seq=0):
            frames.extend(_parse_sse(frame))

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(5):
            while len([frame for frame in frames if frame["event"] != ": ping"]) < 2:
                await asyncio.sleep(0.01)
        assert not task.done(), "interrupted run 回放后不应关闭流"
        # 缩短心跳后，等待实时事件期间应实际收到 `: ping`，且连接保持打开。
        async with asyncio.timeout(5):
            while not any(frame["event"] == ": ping" for frame in frames):
                await asyncio.sleep(0.01)
        assert not task.done()

        publisher = RunEventPublisher(run_id, get_session_factory(), bus, start_seq=seqs[-1])
        await publisher.emit(
            "run.resumed",
            {"run_id": str(run_id), "status": "running"},
            event_type="run",
            title="继续运行",
        )
        await publisher.emit(
            "message.done",
            {"run_id": str(run_id), "finish_reason": "stop"},
            event_type="message",
            title="助手回复",
        )
        async with asyncio.timeout(5):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    live = [frame for frame in frames if frame["event"] != ": ping"]
    assert [frame["event"] for frame in live] == [*names, "run.resumed", "message.done"]
    assert [frame["id"] for frame in live] == [seqs[0], seqs[1], seqs[-1] + 1, seqs[-1] + 2]
    assert live[-1]["data"]["seq"] == seqs[-1] + 2
